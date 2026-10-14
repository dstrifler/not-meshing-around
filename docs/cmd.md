# Command discovery plugin

Send `cmd` to list the primary command names available to you. The plugin queries
core's active command registry on each request; it does not scan plugin files,
import other plugins, or maintain a separate command list. It includes `cmd`
itself, omits aliases and descriptions, and lists entries alphabetically.
Independently registered help commands such as `ping?` are primary commands and
remain listed. Disabled, rejected, and command-free plugins add no entries.

Core applies the request's normal policy: admin-only commands appear for admins,
DM-only commands appear for DM requests, and blocked senders receive no reply.
The reply retains the original audience: DMs receive a private response, while
channel requests receive their response on that channel.

Every packet starts with `CMD | Help`, after any core packet-number prefix. A
single packet has no prefix, for example:

```text
CMD | Help
cmd
ping
```

Larger lists repeat the header on every packet:

```text
[1/2] CMD | Help
ack
cmd
```

```text
[2/2] CMD | Help
ping
test
```

The plugin keeps whole command names on their own lines. It counts the UTF-8
bytes of the header, newlines, and entries, and queries core's numbering budget
for the planned packet count. It repacks when numbering gains digits, including
the transitions to 10 and 100 packets. It returns one preplanned response; core
adds numbering, validates all packets before sending, and retains its normal
delivery serialization, pacing, audience, and timeout behavior. Literal command
names containing `@` are not converted into node mentions.

`plugins.d/cmd.toml` enables the plugin by default; it has no plugin-specific
settings. Packet size comes from `core.message_bytes`. If the header, numbering,
and one complete command cannot fit, the request records a clear plugin error
and sends nothing. Entries are never truncated into unusable command fragments.
There is no automatic registry cache or persistent pagination state to go stale.
