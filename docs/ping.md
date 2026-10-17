# Ping plugin

Migrated from `handle_ping` in the original mesh/pong bots, their command maps,
and `handleMultiPing` in the original system module. It uses only `botcore.api`;
no original feature modules or installation changes are required.

| Command | Behavior |
| --- | --- |
| `ping`, `pinging` | Reply with “🏓 Pong.” and available link diagnostics. |
| `pong` | Reply with “Ping.” |
| `ack` | Reply with “Acknowledged.” |
| `test`, `testing` | Reply with “Testing, one, two, three.” |
| `cq`, `cqcq`, `cqcqcq` | Acknowledge CQ and identify the local node when known. |
| `🔔` | Reply with “I can hear you.” |
| `ping?`, `test?`, `ack?`, etc. | Display plugin help; `ping help` also works. |
| `ping N`, `ack N`, `cq N` | Schedule exactly N diagnostic replies after confirmation. |
| `test N` | Schedule N increasing, byte-sized buffer-test payloads. |
| `ping stop` | Cancel your sequence on this radio/channel; other command families accept `stop` too. |
| `ping #Field test` | Label the response; counted requests can also include a label. |
| `ping @ABCD` | Label the reply and emit `ping.mention_requested` for future integration. |

Core normalizes known node names, short names, and node numbers to `@!xxxxxxxx`
before ping receives them and before responses are sent. Unknown or ambiguous
tags remain unchanged. See [node mentions](plugins.md#node-mentions).
The mention remains a label; it does not change the reply's recipient.

## Configuration

`plugins.d/ping.toml` uses core's shared command routing:

```toml
[routing]
listen_mode = "both"
# channels = [0, 2]  # Omit to accept all channels; [] accepts none.
reply_mode = "dm"
```

Set `listen_mode` to `channels`, `dm`, or `both`. DM acceptance is independent
of the channel list. Ping replies privately by default; use `reply_mode = "origin"`
to answer channel requests in their originating channel. DM requests always
remain private. Immediate replies, sequence confirmations, help/errors, and
delayed sequence messages all follow the same core routing. See
[command routing](plugins.md#command-routing). Restart after configuration changes.

Existing `[settings].reply_privately` values are supported as a compatibility
fallback (`true` means `dm`, `false` means `origin`). An explicit
`[routing].reply_mode` overrides that old value; use the new table for updates.

The `[settings]` table supplies feature settings:

- `reply`: configured ping response; default `🏓 Pong.`
- `show_noise_floor`: default false; include the bot's available local noise floor
  for confirmed direct RF requests only.
- `interval_seconds`: default 20, permitted range 1–3600 seconds.
- `max_count`: default 50, permitted range 2–50 replies.
- `max_sessions`: default 2, permitted range 1–8 concurrent sequences.
- `allow_channel_sequences`: default false; repeated requests must arrive by DM.
- `buffer_bytes`: default 160, permitted range 32–200 bytes, capped by the core
  message byte limit so a buffer-test payload stays in one transmission.

Only valid booleans, finite timing values, and bounded integers are accepted.
Counts outside 1–`max_count` are rejected explicitly. The legacy spoken-test
shorthand `123` and `1234` produces one reply. Duplicate sequences are rejected
until the earlier sequence completes or is cancelled. Limits apply across all
users; a sender can cancel only their sequence on the originating interface and
channel. Restart and shutdown discard active sequences rather than resuming
radio traffic unexpectedly. Accepted request counts are stored in plugin state.

The core scheduler checks due sequences once per second. The first reply follows
the configured interval; later replies wait at least that interval after the
previous delivery. Core pacing and congestion can delay replies. `stop` prevents
future replies but cannot recall a packet already being transmitted. Delivery
failure removes the affected sequence and is reported in core health/logs.

Diagnostics use the received request's normalized transport metadata:

| Request route | Example ping response |
| --- | --- |
| Confirmed direct RF (zero hops, no MQTT/gateway indication) | `🏓 Pong. Direct RF. SNR: 7.5 dB. RSSI: -85 dBm.` |
| Relayed | `🏓 Pong. Hops: 2.` |
| MQTT reported | `🏓 Pong. Hops: 2. MQTT.` |
| MQTT reported, hops unavailable | `🏓 Pong. Hops: unknown. MQTT.` |
| Gateway reported | `🏓 Pong. Hops: 2. Gateway.` |
| Hops unavailable | `🏓 Pong. Hops: unknown.` |

SNR and RSSI on relayed packets describe the last RF link into the bot, so they
are omitted for indirect, MQTT, gateway, and unknown-hop requests. Missing or
non-finite direct-link readings are omitted; zero is valid. Optional noise floor
is a local bot measurement in dBm and follows the same direct-RF restriction.
Hop count comes only from valid `hopStart`/`hopLimit` fields; DM addressing does
not prove a direct RF link. MQTT is tagged when reported by normalized packet
metadata; absence of the tag does not establish an entirely RF route. Core cannot
reconstruct unreported MQTT segments. Console requests show unknown hops without
invented RF readings.

Automatic diagnostic replies use the original request's measurements and route;
they do not measure new RF conditions or round-trip time. Reply counts and
buffer-test byte sizes retain their existing output.

## Text conventions and migration decisions

System-generated prose uses sentence case and punctuation. Standard radio terms
retain their conventional forms: CQ, RF, MQTT, SNR, RSSI, dB, dBm, and B (bytes).
An occasional emoji is allowed; emoji clusters, misspellings, unexplained legacy
shorthand, and repeated exclamation marks are avoided. Long user labels are
shortened at word boundaries to 80 UTF-8 bytes and marked with an ellipsis.
An oversized single-word label becomes an ellipsis rather than a broken word.
Buffer-test payloads intentionally contain ASCII binary test data rather than
prose. Normal responses still pass through core chunking.

The old `ping @node` path called BBS storage and a joke provider directly. Those
features belong to future BBS/joke plugins, so ping emits an event with sender,
target, channel, and interface instead of importing them or pretending delivery
occurred. This event does not currently deliver a joke. Echo, MOTD, node lists,
system information, and automatic banning are separate features/policy and are
not adopted by ping. Excessive counts are rejected without modifying a ban list.

Physical Meshtastic radio operation remains unverified; tests exercise metadata
normalization, routing, formatting, sequence timing, byte sizes, cancellation,
limits, lifecycle cleanup, and delivery policy without hardware.
