"""Adapters keep Meshtastic and console details outside plugins."""

import asyncio
import logging
import math
import socket
import sys
import threading

from .api import Event, Message
from .workers import blocking_call, positive_seconds

log = logging.getLogger(__name__)


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
        node_name=node_name if isinstance(node_name, str) else None, noise_floor=number(noise_floor))


class ConsoleTransport:
    def __init__(self):
        self.status = "new"

    def health(self):
        return {"status": self.status, "last_error": None}

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

    def health(self):
        return {"status": self.status, "last_error": self.last_error}

    async def start(self, runtime):
        from pubsub import pub
        self.runtime, self.loop = runtime, asyncio.get_running_loop()
        self.status = "starting"
        kind = self.settings.get("type")
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
        log.error("Radio transport failed: %s", reason)

    def receive(self, packet, interface):
        if not self.accepting or interface is not self.interface:
            return
        # Meshtastic invokes callbacks from a worker thread.
        self.loop.call_soon_threadsafe(self._enqueue, packet)

    def _enqueue(self, packet):
        if not self.accepting:
            return
        if len(self.pending) >= 256:
            log.warning("Dropping radio packet: receive queue full")
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
                    return
                local_info = nodes.get(f"!{local_node:08x}", {})
                name = local_info.get("user", {}).get("shortName")
                noise = local_info.get("localStats", {}).get("noiseFloor")
                await self.runtime.dispatch(radio_message(packet, local_node, name, noise))
            else:
                self.runtime.enqueue_event(Event("radio.packet", packet))
        except Exception:
            log.exception("Unable to process radio packet")

    async def send(self, text, destination, channel, interface):
        if interface != "primary":
            raise ValueError("unknown radio interface")
        if self.status != "ready":
            raise ConnectionError(self.last_error or "radio is not ready")
        kwargs = {"channelIndex": channel}
        if destination is not None:
            kwargs["destinationId"] = destination
        try:
            await blocking_call(self.interface.sendText, text, **kwargs)
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
