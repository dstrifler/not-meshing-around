# Logging

Management configures Python's standard logging once. Core and plugins share one
readable, single-line text format, destinations, and level. No extra packages or
application log files are required. Configure `bot.toml` (normally
`/opt/meshbot/bot.toml`), then restart `meshbot.service`:

```toml
[logging]
level = "INFO"
message_content = false
output = "journald"          # "journald", "rsyslog", or "both"
rsyslog_host = ""            # Hostname or IP, required for rsyslog/both
rsyslog_protocol = "tcp"     # "tcp" or "udp"
rsyslog_port = 514
```

These defaults also apply to existing installations without a logging table.
Unknown options, unsupported levels/methods/protocols, non-boolean content
settings, and ports outside 1-65535 reject startup before plugins or the database
are opened. Remote fields are validated even if journald is selected. Use a bare
IPv6 address such as `::1` without brackets or a port; the port has its own field.
Hostnames must not include credentials, URL schemes, or whitespace.

## Destinations

- `journald`: writes to stderr. The installed systemd service captures it under
  `meshbot.service`, with identifier `meshbot`. Outside systemd, logs appear on
  the terminal. Journald controls persistence, storage limits, and rotation;
  the bot does not modify host journal settings.
  Native journal streams receive severity prefixes consumed by systemd, so
  `journalctl -u meshbot.service -p warning` filters correctly. Terminal output
  remains ordinary text.
- `rsyslog`: sends directly to the configured syslog receiver, which can be
  rsyslog or another compatible server. The receiver must already accept the
  chosen protocol/port; merely installing rsyslog may not enable a listener.
  An unavailable receiver does not prevent bot startup.
- `both`: writes to the journal/terminal and sends the same text to the receiver.
  If the host also forwards these journal records to that server, choose one
  forwarding path to avoid additional duplicates.

For example, select `output = "both"`, `rsyslog_host = "192.0.2.10"`, and
`rsyslog_port = 5514` to retain the local journal and send to a remote receiver.
Wire records use the RFC5424 envelope, user facility, and application name
`meshbot`. TCP uses RFC6587 octet-counted framing; UDP uses one datagram per
record. The transport is plain TCP/UDP, without TLS. The readable message inside
the envelope matches the journal entry. The receiver owns any file storage,
rotation, or forwarding. The installer does not install/reconfigure rsyslog.

DNS, connections, and writes run in a separate daemon thread. Producers use a
bounded 1024-record queue and never wait for remote network I/O. Socket operations
have a 0.5-second timeout; DNS can outlast that timeout but cannot block the bot
or extend its logging shutdown wait. The worker backs off five seconds after
a failure. Records during backoff or queue overflow are dropped without replay.
Shutdown allows at most one second to drain logs; remaining records can be lost.
This is not a durable audit queue: UDP has no delivery confirmation, and a TCP
write does not establish that the receiver persisted the record.

Remote failure, overflow, recovery, and drop summaries appear locally on stderr,
including with `output = "rsyslog"`. Repeated failure/overflow notices are limited
to one per 30 seconds. Recovery and final summaries include aggregate counters.
These logger-health diagnostics remain visible independently of the selected
application destination/level. A separate daemon worker writes these notices
through a bounded 16-record queue, so stalled local output cannot block remote
delivery, application producers, or the one-second logging shutdown deadline.
Notices are best effort and can be lost if that queue fills or shutdown expires.
Invalid startup configuration is also reported
locally if the requested destination cannot be initialized.

## Levels and records

The configured level is the minimum severity emitted:

| Level | Typical records |
| --- | --- |
| INFO | Startup/API and plugin versions, effective routing, radio/local node readiness, incoming text metadata, command start/completion and duration, response submissions, observed ACKs, shutdown/final states. |
| WARNING | Plugin rejection, queue overflow, firmware NAKs and missing-key guidance. |
| ERROR | Plugin/storage/transport failures, send timeouts, cleanup/startup failures. |
| DEBUG | Ignored-message reasons, routing/policy rejection, non-text packet metadata, packet planning, job/event dispatch. |
| CRITICAL | Fatal bot exit following startup, runtime, or cleanup failure. |

Each line has a UTC timestamp, severity, component, stable event name, and
readable `key=value` fields. Relevant fields include node IDs, interface, channel,
plugin, byte counts, and packet indexes. For example:

```text
2026-10-17T08:00:00.000+00:00 INFO botcore.runtime message_received trace=abcd1234ef56 rx_packet=17 sender=!1234abcd channel=2 interface=primary audience=dm bytes=4 encryption=unknown
2026-10-17T08:00:00.001+00:00 INFO botcore.runtime command_started trace=abcd1234ef56 rx_packet=17 sender=!1234abcd channel=2 interface=primary audience=dm plugin=ping command=ping
```

A `trace` follows the request through execution, plugin API logs, splitting, and
delayed replies. Wire IDs are recorded separately as `rx_packet`, `tx_packet`,
and `ack_for`. Outgoing IDs retain request/plugin context in memory, capped at
1024 entries and 120 seconds. Early/expired/unmatched routing replies still log
their packet ID without claiming a matching request.

`response_submitted` means the SDK accepted the send call, not recipient delivery.
`radio_ack` and `radio_nak` report routing packets provided by firmware; logging
does not change ACK requests/retries or invent failures for missing ACKs.
Incoming encryption is `pki` or `non_pki` only when an explicit flag is available,
otherwise `unknown`. `non_pki` does not distinguish channel encryption from
cleartext. Outgoing logs say `firmware_decides`: SDK submission precedes on-air
encryption. DM status alone never establishes encryption. Repeated NAK/queue
warnings report suppressed counts instead of flooding the journal. NAKs are
limited separately by failure reason, so a missing-public-key failure remains
visible immediately after a different failure. At most 128 recent reason
limiters are retained; evicted reasons start a new suppression window. A full
event queue drops the incoming radio event with the core's limited warning;
it does not also generate a generic packet-processing error for each drop.

## Contents, secrets, and plugin API

`message_content = false` omits incoming bodies, arguments, and outgoing text,
including at DEBUG. Recognized primary command names still appear. Unknown chat
is metadata, without quoting its first word as a command. Explicitly setting
`message_content = true` includes received text and each formatted response,
including DMs. Control characters/newlines are escaped to prevent fake log lines.
Remote records over 65,000 UTF-8 bytes are truncated with a marker; normal mesh
messages are much smaller.

Core never dumps configuration, state values, raw packets, or key material.
Sensitive field names are omitted. Recognizable `password=...`, `psk=...`, and
similar assignments are redacted even with content on. Arbitrary plugin/SDK
exception messages can contain text or credentials, so logs retain error types
and stack frame locations without source lines or arbitrary exception text.
Known core validation/timeout reasons remain readable. Raw Meshtastic/PubSub logs
are suppressed in favor of normalized core diagnostics, so DEBUG does not enable
SDK packet/key dumps.

Plugins use the API logger rather than configuring handlers or destinations:

```python
context.services.logger.info("lookup_started", item_count=3)
context.services.logger.debug("lookup_input", content=context.arguments)
try:
    await perform_lookup()
except Exception as error:
    context.services.logger.error("lookup_failed", error=error)
    raise
```

`services.logger` has `debug`, `info`, `warning`, and `error`, accepting an event
name and keyword metadata. Plugin identity and current request correlation are
automatic. Jobs/lifecycle hooks also have loggers, without implicit payloads.
Uncaught failures are logged by core. Authors must put message text in `content=`;
never interpolate it or secrets into event names/metadata. Pattern redaction
cannot identify every secret in arbitrary prose. The interactive console's
displayed bot replies remain console output, separate from log content controls.

## Reading and exporting the journal

```sh
sudo journalctl -u meshbot.service -f
sudo journalctl -u meshbot.service -b -n 100 --no-pager
sudo journalctl -u meshbot.service --since "1 hour ago" --no-pager > meshbot.log
```

Journal files are binary. Persistent storage normally lives in `/var/log/journal`;
runtime-only storage normally lives in `/run/log/journal`. Reboot persistence
depends on host configuration. `--check` reports startup/plugin/shutdown results
using the same text logs on stderr, with its exit status indicating success or
failure. It no longer prints JSON health snapshots to stdout.
