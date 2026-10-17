"""The public plugin contract. No radio implementation belongs here."""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class Message:
    text: str
    sender: int
    channel: int = 0
    interface: str = "primary"
    direct: bool = False
    snr: float | None = None
    rssi: float | None = None
    hops: int | None = None
    via_mqtt: bool = False
    node_name: str | None = None
    noise_floor: float | None = None
    transport: str | None = None
    original_text: str | None = None  # Received text before core mention normalization.
    packet_id: int | None = None
    pki_encrypted: bool | None = None  # Reported by firmware, not inferred from DM status.
    trace_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True)
class Routing:
    """Plugin command defaults; operator [routing] entries override these."""

    listen_mode: str = "both"
    channels: tuple[int, ...] = tuple(range(8))
    reply_mode: str = "origin"

    def __post_init__(self):
        if self.listen_mode not in ("channels", "dm", "both"):
            raise ValueError('routing.listen_mode must be "channels", "dm", or "both"')
        if (not isinstance(self.channels, tuple) or
                any(type(channel) is not int or not 0 <= channel <= 7 for channel in self.channels)):
            raise ValueError("routing.channels must contain integer channel indexes from 0 to 7")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("routing.channels must not contain duplicates")
        if self.reply_mode not in ("origin", "dm"):
            raise ValueError('routing.reply_mode must be "origin" or "dm"')

    @classmethod
    def from_config(cls, config, default=None):
        if not isinstance(config, dict):
            raise ValueError("routing must be a TOML table")
        if set(config) - {"listen_mode", "channels", "reply_mode"}:
            raise ValueError("unknown routing setting")
        default = default if default is not None else cls()
        if not isinstance(default, cls):
            raise TypeError("Registration.routing must be Routing")
        channels = default.channels
        if "channels" in config:
            if not isinstance(config["channels"], list):
                raise ValueError("routing.channels must be an array")
            channels = tuple(config["channels"])
        return cls(config.get("listen_mode", default.listen_mode), channels,
                   config.get("reply_mode", default.reply_mode))

    def accepts(self, message: Message) -> bool:
        if message.direct:
            return self.listen_mode in ("dm", "both")
        return self.listen_mode in ("channels", "both") and message.channel in self.channels


@dataclass(frozen=True)
class Response:
    text: str | tuple[str, ...]  # A tuple supplies preplanned packet bodies.
    destination: str = "reply"  # Core routing owns solicited replies; unsolicited sends use channel.
    channel: int | None = None
    interface: str | None = None
    normalize_mentions: bool = True


@dataclass(frozen=True)
class Event:
    name: str
    data: Mapping[str, Any] = field(default_factory=dict)


class EventQueueFull(RuntimeError):
    """The core cannot accept another event; callers may retry or drop it."""


class Services(Protocol):
    @property
    def logger(self) -> Any: ...  # PluginLogger from botcore.observability.
    @property
    def message_bytes(self) -> int: ...
    def command_names(self, message: Message) -> tuple[str, ...]: ...
    def packet_payload_bytes(self, packet_count: int) -> int: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    async def emit(self, event: Event) -> None: ...
    async def send(self, response: Response) -> None: ...
    async def reply(self, response: Response, message: Message) -> None: ...


@dataclass(frozen=True)
class Context:
    message: Message
    command: str
    arguments: str
    services: Services


Handler = Callable[[Context], Awaitable[Response | list[Response] | None]]
EventHandler = Callable[[Event, Services], Awaitable[None]]
JobHandler = Callable[[Services], Awaitable[None]]
Hook = Callable[[Services], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    text: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    help: str = ""
    admin_only: bool = False
    direct_only: bool = False


@dataclass(frozen=True)
class Subscription:
    event: str
    handler: EventHandler


@dataclass(frozen=True)
class Job:
    name: str
    interval: float
    handler: JobHandler


@dataclass(frozen=True)
class Registration:
    commands: tuple[Command, ...] = ()
    events: tuple[Subscription, ...] = ()
    jobs: tuple[Job, ...] = ()
    start: Hook | None = None
    stop: Hook | None = None
    routing: Routing = Routing()
