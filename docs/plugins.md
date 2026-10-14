# Plugin runtime

The new runtime has one entry point, `mesh_bot.py`. Its features come entirely from
paired files in `plugins.d`; a ping-only installation enables only `ping.toml`.
The original implementation is preserved under `integration/` for future plugin
ports. It is not a supported runnable bot or a dependency of the new runtime.

Requires Python 3.11+ (`tomllib`). From the repository root:

```sh
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python mesh_bot.py --check
printf 'ping\n' | venv/bin/python mesh_bot.py
venv/bin/python -m unittest discover -s tests -v
```

`bot.toml` defaults to console transport: each input line is treated as a DM
from node 1, and responses print to stdout. EOF shuts down the controller.
`bash launch.sh` also starts the new runtime. Use `--config /path/to/bot.toml`
for local settings; relative plugin and database paths resolve against that
configuration file, not the shell working directory.

For live operation, configure `[radio]` with `type = "tcp"`, `hostname`, and
optional `port` (4403 by default), or `type = "serial"` and `device`. Only one
radio named `primary` is currently supported. Live radio operation requires a
reachable node and has not been validated with physical hardware.
The radio adapter accepts command text addressed to the local node or the broadcast
address only; text for other nodes, missing recipients, and local echoes is ignored.
Radio settings are validated before plugin loading and database creation, including
with `--check`. TCP requires a nonempty `hostname`; `port`, when supplied, must be
an integer from 1 to 65535. Serial `device`, when supplied, must be nonempty text;
omitting it retains SDK device autodetection. These checks do not import the SDK
or open hardware, and do not test whether the configured node is reachable.

## Layers

- Controller: configuration, plugin adoption, startup report, signals, shutdown.
- Core: transport adapters, command dispatcher, policy, event queue, interval
  scheduler, UTF-8 byte chunking, serialized delivery, namespaced SQLite state.
- Plugins: feature logic, command registrations, event subscriptions, jobs,
  configuration validation, and lifecycle hooks.

The core has no feature command list. Routing is case insensitive and matches
whole command text followed by a space or end of message. Longer commands win:
`bbs read` takes priority over `bbs`. Duplicate commands or aliases reject the
later plugin; loading order is alphabetical. Unknown commands receive no reply.

## Add a plugin

Create `plugins.d/example.toml`:

```toml
[plugin]
id = "example"
version = "1.0.0"
api_version = 1
enabled = true

[settings]
greeting = "Hello"
```

Create `plugins.d/example.py`:

```python
from botcore.api import Command, Registration, Response

def register(settings):
    greeting = settings.get("greeting", "Hello")
    if not isinstance(greeting, str):
        raise ValueError("greeting must be text")

    async def hello(context):
        return Response(f"{greeting} {context.arguments}".rstrip())

    return Registration(commands=(
        Command("hello", hello, aliases=("hi",), help="Say hello"),
    ))
```

No core edits are required. Plugin filenames use lowercase letters, digits,
and underscores, beginning with a letter. Identity must match the filename.
Missing pairs, malformed TOML, incompatible API versions, import errors,
registration conflicts, and failed initialization appear as rejected plugins in
the startup report. Disabled plugins are not imported. Both files must exist.
Plugins implement and validate their own `[settings]`; the core validates the
manifest and registration contract. `--check` runs initialization and shutdown
hooks, so those hooks should avoid irreversible actions.

## Contract

Import public types from `botcore.api`. `register(settings)` is synchronous and
returns a `Registration`. It should only validate settings and construct handlers.
The `commands`, `events`, and `jobs` fields, and each command's `aliases`, must be
tuples. Lists, generators, and other containers are rejected before initialization;
invalid registrations never receive partial command registrations.
Handlers and lifecycle hooks are asynchronous; blocking I/O belongs in
`asyncio.to_thread`, or an asynchronous client.

- Commands receive `Context`: normalized command name, arguments, original
  `Message` (sender, channel, interface, DM status), and plugin services.
  Messages also provide optional normalized SNR, RSSI, noise floor, hop count,
  MQTT status, and local node name for diagnostics.
  Return a `Response`, a list of responses, or `None`.
- `Response(text)` replies to the incoming audience. `destination="private"`
  replies to the sender. `destination="channel"` targets a channel.
- `Subscription("event.name", handler)` receives `(Event, services)`.
  Services can emit custom events. Non-text radio packets currently emit
  `radio.packet` with the decoded packet dictionary; additional normalized
  radio events can be added in the transport adapter.
- `Job("name", interval_seconds, handler)` receives services. It first runs
  after the interval, then waits another interval after each execution.
  This first version supports intervals, not calendar/cron schedules.
  Management enables jobs only after the transport is ready; loading and
  `--check` never run jobs. The interval begins when jobs are enabled.
- `start(services)` and `stop(services)` own resource setup and cleanup. Startup
  has a 10-second timeout and cleanup runs even when startup fails. Commands
  and jobs are activated only after successful startup. Job tasks are cancelled
  before shutdown hooks. Hooks must make cleanup safe after partial startup.
  Stop hooks run concurrently after all job cancellation completes, so they must
  own their resources independently and must not depend on another plugin's
  teardown order. Each hook has its own timeout, capped at 10 seconds and further
  limited by `core.shutdown_timeout`; every active plugin gets an attempt.

Services provide `get(key, default)` and `set(key, JSON_serializable_value)` in
plugin-scoped persistent SQLite state, `emit(Event(...))`, and `send(Response(...))`.
The event queue holds at most 256 waiting events. Emission never waits for queue
space: it raises `botcore.api.EventQueueFull` immediately when full. Callers can
catch that exception and drop or retry an event later. An uncaught overflow is
reported as a handler error; other subscribers continue. This avoids a subscriber
waiting on the same queue that the dispatcher must consume.
For scheduled replies to a received request, use `reply(Response(...), original_message)`;
the core retains its audience and applies delivery policy again, including
blocked-sender checks. `services.message_bytes` exposes the core's UTF-8 byte limit.
Emission and sending require an active plugin, so startup/shutdown hooks should
focus on resource management. Jobs and event handlers have no implicit reply
recipient: sending requires `destination="channel"` and an explicit channel.
Alternatively, retain the originating message and use `reply` for solicited
delayed responses. See [ping.md](ping.md) for the first feature migration.

Core policy applies blocked node IDs and command `admin_only`/`direct_only`
flags. Unsolicited broadcasts, DM-to-channel responses, channel changes on
public replies, and interface changes require the plugin ID in
`policy.broadcast_plugins`. Direct replies to received public channel commands
are allowed. Channel indices must be 0–7.
`policy.admins` and `policy.blocked` must be arrays of integer node IDs from
0 to 0xffffffff. TOML hexadecimal integers such as `0x1234abcd` are accepted;
quoted IDs, booleans, and out-of-range values reject startup. The broadcast
permission list must be an array of valid plugin ID strings. Policy validation
runs before plugin initialization or database creation, including with `--check`.

Delivery prefers word boundaries without dropping or reordering characters, respecting
`core.message_bytes` as a UTF-8 byte limit. Oversized single words are split
only when necessary to fit the byte limit. A delivery lock prevents chunks
from different responses interleaving; `core.send_delay` paces transmissions.
Every multipart response starts each packet with `[n/x] ` (one-based packet
number and final packet count). Prefix bytes count toward the same byte limit,
including when counts gain digits. Responses that fit in one packet are unchanged
and have no prefix. Prefixes do not replace or truncate any original content.
If the byte limit cannot accommodate a prefix and payload character, delivery
fails explicitly rather than sending an oversized packet. Plugins return plain
responses; the core owns numbering and split locations. Plugin-provided split
hints are reserved for future work.
Transport failures are reported, without automatic retries or durable outbox
semantics in this version.
`core.send_timeout` (10 seconds by default) bounds waiting for the delivery lock
and each individual transport send. Normal multipart pacing is not included in
the per-packet timeout. A timed-out response releases the lock and records an
error; packets already sent cannot be retracted. A cancelled or failed radio
send marks the transport failed, preventing subsequent SDK sends from
overlapping a still-running worker thread.

Management reports transport readiness at startup and transport/plugin health
at shutdown. Meshtastic connection-loss notifications propagate from the SDK
thread to the core's event loop, fail the transport monitor, and trigger orderly
shutdown with a nonzero exit. The TCP adapter also intercepts the SDK's internal
reconnect hook, which can run after socket EOF or a write error without publishing
connection loss. It stops the reader and reports failure instead of reconnecting
while health still reports ready. Restart the process to establish a fresh radio
connection. Shutdown stops new work and ingress, cancels and awaits all jobs
and inbound handlers before running plugin stop hooks, then closes transport.
Plugin storage remains available during stop hooks. Partially initialized radio
interfaces and event subscriptions are cleaned up even if startup fails.
SIGINT and SIGTERM are handled throughout plugin initialization, radio connection,
and cleanup. `core.startup_timeout` (30 seconds) bounds initialization, while
`core.shutdown_timeout` (15 seconds) bounds each cleanup stage independently.
Later cleanup stages still run if an earlier one times out. Radio connection and
cleanup also have their own `radio.connect_timeout` (20 seconds) and
`radio.close_timeout` (5 seconds) deadlines.
Runtime job cancellation and concurrent stop hooks have separate cleanup budgets.
A failed, timed-out, or cancelled stop hook leaves its plugin marked `failed`,
records the error, and allows other hooks to finish with storage still available.
This includes a hook awaiting a background task it has cancelled. Storage is closed and
transport cleanup is attempted before aggregated cleanup errors reach management. Failed
cleanup makes normal shutdown and `--check` exit nonzero; failures in multiple
cleanup stages are included in the error report.
Errors releasing resources after failed or cancelled initialization are also
retained for management, even when that plugin never became active.

Blocking SDK calls run in daemon workers rather than the default executor.
Cleanup interrupts native socket/serial I/O and stops SDK heartbeat timers before
calling the SDK close method. A constructor or connection that finishes after
shutdown releases its own late resources. Python cannot forcibly stop a stuck
thread; after a deadline the bot reports failure and can exit without waiting for
that worker. No replacement radio sends are allowed after a send failure.

Exceptions in command, event, or job handlers are logged and counted in
`Runtime.health()` with the last error. They do not stop other plugins. Command,
event, and job handlers have a 30-second timeout and run in individual tasks.
A handler that cancels itself, raises `CancelledError`, or awaits a cancelled
child task records a `plugin handler cancelled` error. Command dispatch continues,
remaining event subscribers still receive the event, and jobs run on later intervals.
Management can still cancel the core task and its handler during shutdown, even
if the handler suppresses cancellation or raises another exception while cleaning up.
Cleanup exceptions are still logged and counted before management's cancellation
propagates. Plugins run as trusted Python code inside one process: this API is a
software boundary, not a security sandbox.
Infinite synchronous code, process termination, or malicious code cannot be
contained by these exception handlers. Plugin-owned tasks must be cancelled in
their shutdown hook; prefer registered jobs for core-managed background work.

Configuration is read at startup; restart to adopt changes. Original feature
code is migration reference under `integration/`; plugins must not import it.
