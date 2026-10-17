"""Routing, policy, delivery, events, jobs, and plugin-scoped state."""

import asyncio
import inspect
import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field, replace

from .api import Command, Context, Event, EventQueueFull, Job, Message, Registration, Response, Routing, Subscription
from .mentions import MentionResolver, payload_units
from .workers import positive_seconds

log = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    """Cleanup finished with failures that management must report."""

    def __init__(self, failures):
        self.failures = dict(failures)
        details = "; ".join(f"{name}: {str(error) or type(error).__name__}"
                            for name, error in sorted(self.failures.items()))
        super().__init__(f"cleanup failed: {details}")


async def _invoke_plugin(awaitable, timeout, cancelled_message):
    """Isolate plugin cancellation while preserving new caller cancellations."""
    caller = asyncio.current_task()
    cancelling = caller.cancelling()
    task = asyncio.create_task(awaitable)
    try:
        result = await asyncio.wait_for(task, timeout)
    except asyncio.CancelledError as error:
        if caller.cancelling() > cancelling:
            raise
        raise RuntimeError(cancelled_message) from error
    except Exception as error:
        if caller.cancelling() > cancelling:
            raise asyncio.CancelledError() from error
        raise
    if caller.cancelling() > cancelling:
        raise asyncio.CancelledError()
    return result


def _payload_chunks(text: str, limit: int) -> list[str]:
    """Prefer word boundaries while preserving the original payload."""
    result, current, size = [], [], 0
    for unit in payload_units(text):
        width = len(unit.encode("utf-8"))
        if width > limit:
            raise ValueError("message byte limit is too small for split tags and payload")
        while size + width > limit:
            boundary = next((i + 1 for i in range(len(current) - 1, -1, -1)
                             if current[i].isspace()), len(current))
            result.append("".join(current[:boundary]))
            current = current[boundary:]
            size = sum(len(item.encode("utf-8")) for item in current)
        current.append(unit)
        size += width
    if current:
        result.append("".join(current))
    return result


def packet_prefix(index, total):
    return f"[{index}/{total}] " if total > 1 else ""


def packet_payload_bytes(limit, packet_count):
    """Reserve the widest numbering prefix for a planned response."""
    if type(packet_count) is not int or packet_count < 1:
        raise ValueError("packet count must be a positive integer")
    return limit - len(packet_prefix(packet_count, packet_count).encode("utf-8"))


def chunks(text: str, limit: int) -> list[str]:
    """Number multipart messages; include prefix bytes in every packet's budget."""
    if type(limit) is not int or limit < 4:
        raise ValueError("message byte limit must be at least 4")
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]
    total = 2
    while True:
        # Reserve the widest prefix, then recalculate if the count grows.
        budget = packet_payload_bytes(limit, total)
        if budget < 1:
            raise ValueError("message byte limit is too small for split tags and payload")
        parts = _payload_chunks(text, budget)
        if len(parts) <= total:
            total = len(parts)
            return [packet_prefix(index, total) + part for index, part in enumerate(parts, 1)]
        total = len(parts)


@dataclass
class Policy:
    admins: frozenset[int] = frozenset()
    blocked: frozenset[int] = frozenset()
    broadcast_plugins: frozenset[str] = frozenset()

    def __post_init__(self):
        for name in ("admins", "blocked"):
            if any(type(node) is not int or not 0 <= node <= 0xffffffff
                   for node in getattr(self, name)):
                raise ValueError(f"policy.{name} requires integer node IDs from 0 to 0xffffffff")
        if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
               for name in self.broadcast_plugins):
            raise ValueError("policy.broadcast_plugins requires valid plugin IDs")

    @classmethod
    def from_config(cls, config):
        if not isinstance(config, dict):
            raise ValueError("policy must be a TOML table")
        values = []
        for name in ("admins", "blocked", "broadcast_plugins"):
            entries = config.get(name, [])
            if not isinstance(entries, list):
                raise ValueError(f"policy.{name} must be an array")
            # Validate before making a set: bools compare equal to integer IDs,
            # and malformed entries may be unhashable.
            if name == "broadcast_plugins":
                if any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", item)
                       for item in entries):
                    raise ValueError("policy.broadcast_plugins requires valid plugin IDs")
            elif any(type(item) is not int or not 0 <= item <= 0xffffffff for item in entries):
                raise ValueError(f"policy.{name} requires integer node IDs from 0 to 0xffffffff")
            values.append(frozenset(entries))
        return cls(*values)

    def allows(self, message: Message, command: Command) -> bool:
        return (message.sender not in self.blocked
                and (not command.admin_only or message.sender in self.admins)
                and (not command.direct_only or message.direct))


@dataclass
class PluginState:
    registration: Registration
    status: str = "starting"
    errors: int = 0
    last_error: str | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    cleanup_error: Exception | None = None


class PluginServices:
    def __init__(self, runtime, plugin):
        self._runtime, self._plugin = runtime, plugin

    @property
    def message_bytes(self):
        return self._runtime.byte_limit

    def command_names(self, message):
        """Snapshot active primary names allowed for this request, without aliases."""
        return self._runtime.command_names(message)

    def packet_payload_bytes(self, packet_count):
        return packet_payload_bytes(self.message_bytes, packet_count)

    async def reply(self, response, message):
        """Delayed replies retain their original audience and delivery policy."""
        if self._runtime.plugins[self._plugin].status != "active":
            raise RuntimeError("plugin is not active")
        if message.sender in self._runtime.policy.blocked:
            raise PermissionError("sender is blocked")
        await self._runtime.deliver(self._plugin, response, message)

    def get(self, key, default=None):
        row = self._runtime.db.execute(
            "SELECT value FROM state WHERE plugin=? AND key=?",
            (self._plugin, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self._runtime.db:
            self._runtime.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?, ?)",
                                     (self._plugin, key, json.dumps(value)))

    async def emit(self, event):
        if self._runtime.plugins[self._plugin].status != "active":
            raise RuntimeError("plugin is not active")
        self._runtime.enqueue_event(event)

    async def send(self, response):
        if self._runtime.plugins[self._plugin].status != "active":
            raise RuntimeError("plugin is not active")
        await self._runtime.deliver(self._plugin, response)


class Runtime:
    def __init__(self, transport, database, *, policy=None, byte_limit=160, send_delay=0.7,
                 send_timeout=10):
        chunks("", byte_limit)
        if not math.isfinite(send_delay) or send_delay < 0:
            raise ValueError("send delay must be finite and nonnegative")
        if type(send_timeout) not in (int, float) or not math.isfinite(send_timeout) or send_timeout <= 0:
            raise ValueError("send timeout must be finite and positive")
        self.transport, self.policy = transport, policy or Policy()
        self.byte_limit, self.send_delay = byte_limit, send_delay
        self.send_timeout = send_timeout
        self._running = self._closing = self._closed = False
        self.db = sqlite3.connect(database)
        self.db.execute("CREATE TABLE IF NOT EXISTS state "
                        "(plugin TEXT, key TEXT, value TEXT, PRIMARY KEY(plugin,key))")
        self.plugins, self.commands = {}, {}
        self.events = asyncio.Queue(maxsize=256)
        self.delivery_lock = asyncio.Lock()

    def health(self):
        return {name: {"status": state.status, "errors": state.errors,
                       "last_error": state.last_error}
                for name, state in self.plugins.items()}

    def command_names(self, message):
        if self._closing:
            return ()
        return tuple(sorted({command.text for alias, (name, command) in self.commands.items()
                             if alias == command.text and self.plugins[name].status == "active"
                             and self.plugins[name].registration.routing.accepts(message)
                             and self.policy.allows(message, command)}))

    def _mention_resolver(self, interface):
        snapshot = getattr(self.transport, "node_snapshot", None)
        nodes = snapshot(interface) if snapshot else ()
        return MentionResolver(nodes)

    def normalize_mentions(self, text, interface="primary"):
        return self._mention_resolver(interface).normalize(text) if "@" in text else text

    def enqueue_event(self, event):
        """Never wait on the queue whose consumer may be the calling handler."""
        if self._closing:
            raise RuntimeError("runtime is stopping")
        if not isinstance(event, Event) or not isinstance(event.name, str) or not event.name:
            raise ValueError("emit requires a named Event")
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull as error:
            raise EventQueueFull("event queue is full") from error

    def validate(self, registration):
        if not isinstance(registration, Registration):
            raise TypeError("register must return Registration")
        if not isinstance(registration.routing, Routing):
            raise TypeError("Registration.routing must be Routing")
        for field_name in ("commands", "events", "jobs"):
            if not isinstance(getattr(registration, field_name), tuple):
                raise TypeError(f"Registration.{field_name} must be a tuple")
        names = set()
        for command in registration.commands:
            if not isinstance(command, Command) or not inspect.iscoroutinefunction(command.handler):
                raise ValueError("commands require async handlers")
            if not isinstance(command.aliases, tuple):
                raise TypeError("Command.aliases must be a tuple")
            for name in (command.text, *command.aliases):
                if not isinstance(name, str) or not name.strip() or name != " ".join(name.lower().split()):
                    raise ValueError("command names must be normalized lowercase text")
                if name in self.commands or name in names:
                    raise ValueError(f"command conflict: {name}")
                names.add(name)
        job_names = set()
        for job in registration.jobs:
            if not isinstance(job, Job):
                raise TypeError("jobs must contain Job objects")
            if not job.name or job.name in job_names or not math.isfinite(job.interval) or job.interval <= 0:
                raise ValueError("jobs need unique names and positive finite intervals")
            job_names.add(job.name)
            if not inspect.iscoroutinefunction(job.handler):
                raise ValueError("jobs require async handlers")
        for subscription in registration.events:
            if not isinstance(subscription, Subscription):
                raise TypeError("events must contain Subscription objects")
            if not subscription.event or not inspect.iscoroutinefunction(subscription.handler):
                raise ValueError("subscriptions require named events and async handlers")
        for hook in (registration.start, registration.stop):
            if hook is not None and not inspect.iscoroutinefunction(hook):
                raise ValueError("lifecycle hooks must be async")

    async def activate(self, name, registration):
        if self._closing:
            raise RuntimeError("runtime is stopping")
        if name in self.plugins:
            raise ValueError(f"duplicate plugin: {name}")
        self.validate(registration)
        state = self.plugins[name] = PluginState(registration)
        services = PluginServices(self, name)
        try:
            if registration.start:
                await _invoke_plugin(registration.start(services), 10, "plugin startup cancelled")
            state.status = "active"
            for command in registration.commands:
                for alias in (command.text, *command.aliases):
                    self.commands[alias] = (name, command)
            if self._running:
                self._start_jobs(name, state)
        except BaseException as original:
            state.status = "failed"
            if isinstance(original, asyncio.CancelledError) and isinstance(original.__cause__, Exception):
                self.failed(name, original.__cause__)
            if registration.stop:
                try:
                    # Rollback can begin with a previously delivered management
                    # cancellation. Only a new cancellation should interrupt it.
                    await _invoke_plugin(registration.stop(services), 10, "startup cleanup cancelled")
                except asyncio.CancelledError as cancelled:
                    error = (cancelled.__cause__ if isinstance(cancelled.__cause__, Exception)
                             else RuntimeError("startup cleanup cancelled"))
                    state.cleanup_error = error
                    self.failed(name, error)
                    raise
                except Exception as error:
                    if isinstance(error, TimeoutError):
                        error = TimeoutError("startup cleanup timed out")
                    state.cleanup_error = error
                    self.failed(name, error)
            raise

    def start(self):
        """Management enables jobs only after transport startup succeeds."""
        if self._closing:
            raise RuntimeError("runtime is stopping")
        if self._running:
            return
        self._running = True
        for name, state in self.plugins.items():
            if state.status == "active":
                self._start_jobs(name, state)

    def _start_jobs(self, name, state):
        services = PluginServices(self, name)
        for job in state.registration.jobs:
            state.tasks.append(asyncio.create_task(self._job(name, job, services)))

    def quiesce(self):
        """Stop new work and request cancellation of all jobs before cleanup."""
        if self._closing:
            return
        self._closing = True
        self._running = False
        for state in self.plugins.values():
            for task in state.tasks:
                task.cancel()

    def failed(self, name, error):
        state = self.plugins[name]
        state.errors += 1
        state.last_error = str(error)
        log.exception("Plugin %s handler failed", name)

    async def _invoke_handler(self, name, handler, *args):
        """Contain plugin cancellation while preserving cancellation of core work."""
        try:
            return await _invoke_plugin(handler(*args), 30, "plugin handler cancelled")
        except asyncio.CancelledError as error:
            if isinstance(error.__cause__, Exception):
                self.failed(name, error.__cause__)
            raise

    async def dispatch(self, message):
        if self._closing:
            return False
        text = message.text.strip()
        # Longest command wins; matching is case insensitive and boundary aware.
        for alias in sorted(self.commands, key=len, reverse=True):
            if text.lower() != alias and not text.lower().startswith(alias + " "):
                continue
            name, command = self.commands[alias]
            if (self.plugins[name].status != "active" or
                    not self.plugins[name].registration.routing.accepts(message) or
                    not self.policy.allows(message, command)):
                return False
            try:
                # Route by the original command spelling. Normalize message and
                # arguments from one identity snapshot so aliases containing @
                # remain valid and a radio update cannot change identities mid-call.
                resolver = self._mention_resolver(message.interface) if "@" in message.text else None
                normalized = replace(message,
                    text=resolver.normalize(message.text) if resolver else message.text,
                    original_text=message.original_text if message.original_text is not None else message.text)
                arguments = text[len(alias):].strip()
                reply = await self._invoke_handler(name, command.handler, Context(
                    normalized, command.text, resolver.normalize(arguments) if resolver else arguments,
                    PluginServices(self, name)))
                for response in reply if isinstance(reply, list) else ([] if reply is None else [reply]):
                    await self.deliver(name, response, message)
            except Exception as error:
                self.failed(name, error)
            return True
        return False

    async def deliver(self, plugin, response, message=None):
        if self._closing:
            raise RuntimeError("runtime is stopping")
        if not isinstance(response, Response) or not isinstance(response.text, (str, tuple)):
            raise TypeError("handlers must return Response objects")
        if type(response.normalize_mentions) is not bool:
            raise TypeError("normalize_mentions must be a boolean")
        if isinstance(response.text, tuple) and (not response.text or
                any(not isinstance(part, str) or not part for part in response.text)):
            raise ValueError("preplanned responses require nonempty text packet bodies")
        if response.destination not in ("reply", "private", "channel"):
            raise ValueError("invalid response destination")
        if message is None and response.destination != "channel":
            raise ValueError("unsolicited messages require an explicit channel")
        if message is not None:
            state = self.plugins.get(plugin)
            routing = state.registration.routing if state else Routing()
            if not routing.accepts(message):
                raise PermissionError("plugin routing does not accept this request")
            if message.sender in self.policy.blocked:
                raise PermissionError("sender is blocked")
            if ((message.direct and response.destination == "channel") or
                    (response.channel is not None and response.channel != message.channel) or
                    (response.interface is not None and response.interface != message.interface)):
                raise PermissionError("replies must retain the originating conversation")
            # Operator routing controls solicited delivery, including retained
            # messages passed to services.reply by jobs and event handlers.
            response = replace(response, destination=(
                "private" if message.direct or routing.reply_mode == "dm" else "reply"))
        channel = response.channel if response.channel is not None else (message.channel if message else None)
        interface = response.interface or (message.interface if message else "primary")
        destination = message.sender if message and (response.destination == "private" or
                       (response.destination == "reply" and message.direct)) else None
        # Unsolicited broadcasts still require explicit global permission.
        broadcast = destination is None and (message is None or message.direct or channel != message.channel)
        if (broadcast or (message and interface != message.interface)) and plugin not in self.policy.broadcast_plugins:
            raise PermissionError("plugin is not permitted to broadcast or change interface")
        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 7:
            raise ValueError("channel must be between 0 and 7")
        try:
            async with asyncio.timeout(self.send_timeout):
                await self.delivery_lock.acquire()
        except TimeoutError as error:
            raise TimeoutError("delivery queue wait timed out") from error
        try:
            if self._closing:
                raise RuntimeError("runtime is stopping")
            bodies = response.text if isinstance(response.text, tuple) else (response.text,)
            if response.normalize_mentions and any("@" in body for body in bodies):
                resolver = self._mention_resolver(interface)
                bodies = tuple(resolver.normalize(body) for body in bodies)
            if isinstance(response.text, tuple):
                total = len(bodies)
                parts = [packet_prefix(index, total) + body for index, body in enumerate(bodies, 1)]
                if any(len(part.encode("utf-8")) > self.byte_limit for part in parts):
                    raise ValueError("preplanned packet exceeds core.message_bytes after formatting")
            else:
                parts = chunks(bodies[0], self.byte_limit)
            for part in parts:
                try:
                    async with asyncio.timeout(self.send_timeout):
                        await self.transport.send(part, destination, channel, interface)
                except TimeoutError as error:
                    raise TimeoutError("transport send timed out") from error
                await asyncio.sleep(self.send_delay)
        finally:
            self.delivery_lock.release()

    async def event_loop(self):
        while True:
            event = await self.events.get()
            for name, state in self.plugins.items():
                if state.status != "active":
                    continue
                for subscription in state.registration.events:
                    if subscription.event == event.name:
                        try:
                            await self._invoke_handler(name, subscription.handler, event, PluginServices(self, name))
                        except Exception as error:
                            self.failed(name, error)

    async def _job(self, name, job, services):
        while True:
            await asyncio.sleep(job.interval)
            try:
                await self._invoke_handler(name, job.handler, services)
            except Exception as error:
                self.failed(name, error)

    async def close(self, *, timeout=10):
        if self._closed:
            return
        timeout = positive_seconds(timeout, "runtime cleanup timeout")
        self.quiesce()
        active = []
        tasks = []
        for name, state in self.plugins.items():
            if state.status == "active":
                active.append((name, state))
                state.status = "stopping"
            tasks.extend(state.tasks)
        failures = {name: state.cleanup_error for name, state in self.plugins.items()
                    if state.cleanup_error is not None}

        async def stop_plugin(name, state):
            try:
                if state.registration.stop:
                    async with asyncio.timeout(min(10, timeout)):
                        await state.registration.stop(PluginServices(self, name))
            except asyncio.CancelledError:
                error = RuntimeError("shutdown hook cancelled")
                failures[name] = error
                state.cleanup_error = error
                self.failed(name, error)
                state.status = "failed"
                raise
            except Exception as error:
                if isinstance(error, TimeoutError):
                    error = TimeoutError("shutdown hook timed out")
                failures[name] = error
                state.cleanup_error = error
                self.failed(name, error)
                state.status = "failed"
            else:
                state.status = "stopped"

        try:
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                failures["job cancellation"] = TimeoutError("job cancellation timed out")
            # Start every hook before awaiting any one plugin. Resource cleanup
            # is independent; jobs have stopped and namespaced storage is open.
            hooks = [asyncio.create_task(stop_plugin(name, state)) for name, state in reversed(active)]
            # A hook can propagate cancellation from its own child task. Wait
            # for all hooks before closing storage and reporting their failures.
            # Cancelling close() itself still cancels this gather and propagates.
            await asyncio.gather(*hooks, return_exceptions=True)
        finally:
            self.commands.clear()
            self.db.close()
            self._closed = True
        if failures:
            raise CleanupError(failures)
