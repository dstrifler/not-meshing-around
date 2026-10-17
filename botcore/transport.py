"""Adapters keep Meshtastic and console details outside plugins."""

import asyncio
import logging
import math
import re
import socket
import sys
import threading
from collections import OrderedDict
from time import monotonic

from .api import Event, Message
from .mentions import NodeIdentity, valid_number
from .observability import RateLimit, event as log_event, node_id
from .workers import blocking_call, positive_seconds

log = logging.getLogger(__name__)


def validate_radio(settings):
    """Validate configuration without importing the SDK or opening hardware."""
    if not isinstance(settings, dict):
        raise ValueError("radio must be a TOML table")
    settings = dict(settings)
    kind = settings.get("type", "console")
    if kind not in ("console", "tcp", "serial"):
        raise ValueError("radio.type must be console, tcp, or serial")
    settings["type"] = kind
    if kind == "tcp" or "hostname" in settings:
        hostname = settings.get("hostname")
        if not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("radio.hostname must be nonempty text for TCP")
        settings["hostname"] = hostname.strip()
    if "port" in settings:
        port = settings["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("radio.port must be an integer from 1 to 65535")
    if "device" in settings:
        device = settings["device"]
        if not isinstance(device, str) or not device.strip():
            raise ValueError("radio.device must be nonempty text when supplied")
    for name, default in (("connect_timeout", 20), ("close_timeout", 5)):
        settings[name] = positive_seconds(settings.get(name, default), f"radio.{name}")
    return settings


def number(value):
    """Accept genuine finite radio measurements, including zero."""
    return value if type(value) in (int, float) and math.isfinite(value) else None


def radio_message(packet, local_node, node_name=None, noise_floor=None):
    decoded = packet.get("decoded", {})
    payload = decoded.get("payload", b"")
    text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
    start, remaining = packet.get("hopStart"), packet.get("hopLimit")
    hops = start - remaining if (type(start) is int and type(remaining) is int
                                and 0 <= remaining <= start <= 7) else None
    mechanism = str(packet.get("transport_mechanism") or packet.get("transportMechanism")
                    or decoded.get("transport_mechanism") or decoded.get("transportMechanism") or "").lower()
    mqtt = packet.get("viaMqtt") is True or decoded.get("viaMqtt") is True or "mqtt" in mechanism
    transport = "mqtt" if mqtt else "gateway" if "udp" in mechanism else "rf" if "lora" in mechanism else None
    return Message(text, packet["from"], packet.get("channel", 0),
        direct=packet.get("to") == local_node, snr=number(packet.get("rxSnr")),
        rssi=number(packet.get("rxRssi")), hops=hops,
        via_mqtt=mqtt, transport=transport,
        node_name=node_name if isinstance(node_name, str) else None, noise_floor=number(noise_floor),
        packet_id=packet.get('id') if type(packet.get('id')) is int else None,
        pki_encrypted=packet.get('pkiEncrypted', packet.get('pki_encrypted'))
        if type(packet.get('pkiEncrypted', packet.get('pki_encrypted'))) is bool else None)


class ConsoleTransport:
    def __init__(self):
        self.status = "new"

    def health(self):
        return {"status": self.status, "last_error": None}

    def node_snapshot(self, interface="primary"):
        if interface != "primary":
            raise ValueError("unknown radio interface")
        return ()

    async def start(self, runtime):
        self.runtime = runtime
        self.status = "ready"

    async def send(self, text, destination, channel, interface):
        print(f"[{interface} channel={channel} to={destination or 'broadcast'}] {text}", flush=True)

    async def run(self):
        reader = asyncio.StreamReader()
        pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        try:
            while line := await reader.readline():
                await self.runtime.dispatch(Message(line.decode("utf-8").rstrip("\r\n"),
                                                    sender=1, direct=True))
        finally:
            pipe.close()

    async def stop_receiving(self):
        # Management cancels run(), which closes the input pipe.
        pass

    async def close(self):
        self.status = "stopped"


class MeshtasticTransport:
    def __init__(self, settings):
        settings = validate_radio(settings)
        if settings["type"] not in ("tcp", "serial"):
            raise ValueError("Meshtastic transport requires radio.type tcp or serial")
        self.settings = settings
        self.connect_timeout = positive_seconds(settings.get("connect_timeout", 20), "radio.connect_timeout")
        self.close_timeout = positive_seconds(settings.get("close_timeout", 5), "radio.close_timeout")
        self.interface = None
        self.accepting = False
        self.pending = set()
        self.status = "new"
        self.last_error = None
        self._failure = asyncio.Event()
        self._subscriptions = []
        self._connect_task = None
        self._stopping = False
        self._stop_worker = threading.Event()
        self._closed = False
        self._queue_warning = RateLimit()
        self._routing_warning = RateLimit()
        self._submissions = OrderedDict()

    def health(self):
        return {"status": self.status, "last_error": self.last_error}

    def node_snapshot(self, interface="primary"):
        """Copy identity data from the SDK's live node database, without I/O."""
        if interface != "primary":
            raise ValueError("unknown radio interface")
        nodes = getattr(self.interface, "nodes", None) or {}
        if not isinstance(nodes, dict):
            return ()
        result = []
        for key, value in list(nodes.items()):
            if not isinstance(value, dict):
                continue
            user = value.get("user")
            user = dict(user) if isinstance(user, dict) else {}
            number = value.get("num")
            keyed = int(key[1:], 16) if isinstance(key, str) and re.fullmatch(r"![0-9a-fA-F]{8}", key) else None
            if not valid_number(number):
                number = keyed
            elif keyed is not None and keyed != number:
                continue
            if valid_number(number):
                result.append(NodeIdentity(number, user.get("longName", ""), user.get("shortName", "")))
        return tuple(result)

    async def start(self, runtime):
        from pubsub import pub
        self.runtime, self.loop = runtime, asyncio.get_running_loop()
        self.status = "starting"
        kind = self.settings.get("type")
        log_event(log, logging.INFO, 'radio_connecting', transport=kind,
                  hostname=self.settings.get('hostname'), port=self.settings.get('port', 4403),
                  device=self.settings.get('device'))
        if kind == "tcp":
            from meshtastic.tcp_interface import TCPInterface
            factory = TCPInterface
            kwargs = {"hostname": self.settings["hostname"], "portNumber": self.settings.get("port", 4403)}
        elif kind == "serial":
            from meshtastic.serial_interface import SerialInterface
            factory, kwargs = SerialInterface, {"devPath": self.settings.get("device")}
        else:
            raise ValueError("radio type must be serial or tcp")
        try:
            for handler, topic in ((self.receive, "meshtastic.receive"),
                                   (self.connection_lost, "meshtastic.connection.lost")):
                pub.subscribe(handler, topic)
                self._subscriptions.append((handler, topic))
            # Retain the interface before connecting so failed handshakes can
            # always be cleaned up, including when startup is cancelled.
            self._connect_task = asyncio.create_task(blocking_call(self._connect, factory, kwargs))
            self._connect_task.add_done_callback(self._connection_finished)
            async with asyncio.timeout(self.connect_timeout):
                await asyncio.shield(self._connect_task)
            if self._failure.is_set():
                raise ConnectionError(self.last_error)
            self.status = "ready"
            self.accepting = True
            log_event(log, logging.INFO, 'radio_ready', transport=kind,
                      local_node=node_id(self.interface.myInfo.my_node_num))
        except Exception as error:
            self._fail("radio startup timed out" if isinstance(error, TimeoutError)
                       else f"radio startup failed: {error}")
            if isinstance(error, TimeoutError):
                raise TimeoutError("radio startup timed out") from error
            raise

    @staticmethod
    def _connection_finished(task):
        if not task.cancelled():
            task.exception()  # Retrieve late failures after the startup await was cancelled.

    def _connect(self, factory, kwargs):
        interface = factory(connectNow=False, timeout=self.connect_timeout, **kwargs)
        self.interface = interface
        if self.settings["type"] == "tcp":
            # TCP EOF/write errors can invoke this SDK hook without publishing
            # connection.lost. Fail the bot instead of silently reconnecting;
            # install before connect(), which starts the SDK reader thread.
            interface._reconnect = lambda: self._tcp_connection_lost(interface)
        try:
            if self._stop_worker.is_set():
                raise ConnectionError("radio startup stopped")
            interface.connect()
            if self._stop_worker.is_set():
                raise ConnectionError("radio startup stopped")
            if interface.waitForConfig() is False:
                raise ConnectionError("radio configuration was not received")
            if not interface.isConnected.is_set() or interface.myInfo is None:
                raise ConnectionError("radio did not become ready")
        finally:
            # A constructor or connect that returns after the cleanup deadline
            # still owns and releases the resources it eventually acquired.
            if self._stop_worker.is_set():
                try:
                    self._interrupt_io(interface)
                finally:
                    interface.close()
                    if self.interface is interface:
                        self.interface = None

    def _tcp_connection_lost(self, interface):
        interface._wantExit = True
        if not self._stopping:
            self.connection_lost(interface)
        # Stop the SDK reader on EOF and propagate failure to a writing worker.
        raise ConnectionError("radio connection lost")

    @staticmethod
    def _interrupt_io(interface):
        """Interrupt native I/O before the SDK attempts its own cleanup/join."""
        interface._wantExit = True
        timer = getattr(interface, "heartbeatTimer", None)
        if timer:
            timer.cancel()
        stream = getattr(interface, "socket", None)
        if stream:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
            interface.socket = None
        stream = getattr(interface, "stream", None)
        if stream:
            for name in ("cancel_read", "cancel_write"):
                cancel = getattr(stream, name, None)
                if cancel:
                    try:
                        cancel()
                    except OSError:
                        pass
            stream.close()
            interface.stream = None

    def connection_lost(self, interface):
        if interface is self.interface and self.status in ("starting", "ready"):
            self.loop.call_soon_threadsafe(self._fail, "radio connection lost")

    def _fail(self, reason):
        if self._failure.is_set():
            return
        self.accepting = False
        self.status, self.last_error = "failed", reason
        self._failure.set()
        # Preserve detailed health internally, but SDK exception text may
        # contain packet payloads; logs use a stable reason category.
        category = reason.split(':', 1)[0]
        log_event(log, logging.ERROR, 'radio_failed', reason=category)

    def receive(self, packet, interface):
        if not self.accepting or interface is not self.interface:
            return
        # Meshtastic invokes callbacks from a worker thread.
        self.loop.call_soon_threadsafe(self._enqueue, packet)

    def _enqueue(self, packet):
        if not self.accepting:
            return
        if len(self.pending) >= 256:
            suppressed = self._queue_warning.allow()
            if suppressed is not None:
                log_event(log, logging.WARNING, 'radio_queue_full', suppressed=suppressed)
            return
        task = asyncio.create_task(self._handle(packet))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _handle(self, packet):
        try:
            decoded = packet.get("decoded", {})
            if decoded.get("portnum") == "TEXT_MESSAGE_APP":
                nodes = getattr(self.interface, "nodes", {}) or {}
                local_node = self.interface.myInfo.my_node_num
                if (packet.get("to") not in (local_node, 0xffffffff)
                        or packet.get("from") == local_node):
                    log_event(log, logging.DEBUG, 'radio_text_ignored',
                              rx_packet=packet.get('id'), sender=node_id(packet.get('from')),
                              reason='local_echo' if packet.get('from') == local_node else 'other_recipient')
                    return
                local_info = nodes.get(f"!{local_node:08x}", {})
                name = local_info.get("user", {}).get("shortName")
                noise = local_info.get("localStats", {}).get("noiseFloor")
                await self.runtime.dispatch(radio_message(packet, local_node, name, noise))
            else:
                if decoded.get('portnum') == 'ROUTING_APP':
                    self._log_routing(packet)
                else:
                    log_event(log, logging.DEBUG, 'radio_packet_received',
                              port=decoded.get('portnum'), rx_packet=packet.get('id'),
                              sender=node_id(packet.get('from')))
                self.runtime.enqueue_event(Event("radio.packet", packet))
        except Exception as error:
            log_event(log, logging.ERROR, 'radio_packet_failed', error=error)

    def record_submission(self, packet_id, fields):
        now = monotonic()
        self._expire_submissions(now)
        self._submissions[packet_id] = (now, fields)
        self._submissions.move_to_end(packet_id)
        while len(self._submissions) > 1024:
            self._submissions.popitem(last=False)

    def _expire_submissions(self, now):
        while self._submissions and next(iter(self._submissions.values()))[0] < now - 120:
            self._submissions.popitem(last=False)

    def _log_routing(self, packet):
        decoded = packet.get('decoded', {})
        routing = decoded.get('routing', {})
        if not isinstance(routing, dict):
            return
        reason = routing.get('errorReason', routing.get('error_reason', 'NONE'))
        if not isinstance(reason, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', reason):
            reason = 'UNKNOWN'
        request_id = decoded.get('requestId', decoded.get('request_id'))
        self._expire_submissions(monotonic())
        submission = self._submissions.pop(request_id, None) if type(request_id) is int else None
        fields = dict(submission[1]) if submission else {}
        fields.update(ack_for=request_id if type(request_id) is int else None,
                      sender=node_id(packet.get('from')), reason=reason,
                      channel=packet.get('channel', 0))
        if reason == 'NONE':
            log_event(log, logging.INFO, 'radio_ack', **fields)
        else:
            suppressed = self._routing_warning.allow()
            if suppressed is not None:
                hints = {'PKI_SEND_FAIL_PUBLIC_KEY': 'destination_public_key_missing',
                         'PKI_UNKNOWN_PUBKEY': 'sender_public_key_missing',
                         'PKI_FAILED': 'check_key_material_and_firmware'}
                log_event(log, logging.WARNING, 'radio_nak', suppressed=suppressed,
                          guidance=hints.get(reason, 'inspect_radio_configuration'), **fields)

    async def send(self, text, destination, channel, interface):
        if interface != "primary":
            raise ValueError("unknown radio interface")
        if self.status != "ready":
            raise ConnectionError(self.last_error or "radio is not ready")
        kwargs = {"channelIndex": channel}
        if destination is not None:
            kwargs["destinationId"] = destination
        try:
            packet = await blocking_call(self.interface.sendText, text, **kwargs)
            packet_id = getattr(packet, 'id', None)
            return packet_id if type(packet_id) is int else None
        except asyncio.CancelledError:
            # Cancelling an await cannot retract a running SDK send. Prevent
            # subsequent sends from overlapping it and let management close
            # the connection to release any blocked I/O.
            if not self._stopping:
                self._fail("radio send cancelled or timed out")
            raise
        except Exception as error:
            self._fail(f"radio send failed: {error}")
            raise

    async def run(self):
        await self._failure.wait()
        raise ConnectionError(self.last_error)

    async def stop_receiving(self):
        self._stopping = True
        self.accepting = False
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self._closed:
            return
        self._stopping = True
        self._stop_worker.set()
        if self.status != "failed":
            self.status = "stopping"
        try:
            # Unsubscribe before any await, including cleanup that may time out.
            if self._subscriptions:
                from pubsub import pub
                for handler, topic in self._subscriptions:
                    pub.unsubscribe(handler, topic)
                self._subscriptions.clear()
            async with asyncio.timeout(self.close_timeout):
                await self.stop_receiving()
                if self.interface:
                    try:
                        await blocking_call(self._interrupt_io, self.interface)
                    except Exception:
                        log.warning("Native radio I/O interruption failed; attempting SDK cleanup", exc_info=True)
                if self._connect_task:
                    await asyncio.shield(asyncio.gather(self._connect_task, return_exceptions=True))
                if self.interface:
                    await blocking_call(self.interface.close)
        except TimeoutError as error:
            self._fail("radio cleanup timed out")
            raise TimeoutError("radio cleanup timed out") from error
        except Exception as error:
            self._fail(f"radio cleanup failed: {error}")
            raise
        finally:
            self._closed = True
            self.interface = None
        if self.status != "failed":
            self.status = "stopped"
        log_event(log, logging.INFO, 'radio_closed', status=self.status)
