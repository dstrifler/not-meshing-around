# Mesh Bot

A plugin-driven Meshtastic bot, rebuilding the architecture of meshing-around.
One controller starts the core services and adopts enabled plugins from
`plugins.d/`. The default installation enables command discovery and ping.

Each plugin's TOML file supports a `[routing]` table with `listen_mode`
(`"channels"`, `"dm"`, or `"both"`), allowed channel indexes, and `reply_mode`
(`"origin"` or `"dm"`). Core applies these settings to commands, discovery, and
delayed replies. See [command routing](docs/plugins.md#command-routing).

Operational logs use readable text with journald output by default. Optional
TCP/UDP syslog output and full message logging are configured in `bot.toml`;
message bodies stay off by default, including at DEBUG. See [logging](docs/logging.md).

## Quick start

Requires Python 3.11 or newer.

```sh
bash bootstrap.sh
venv/bin/python mesh_bot.py --check
printf 'ping\n' | venv/bin/python mesh_bot.py
```

The default transport is a local console: type `ping` to receive `🏓 Pong. Hops: unknown.`.
Type `cmd` to list available primary command names. Every reply packet includes
the `CMD | Help` header; core numbers multipart lists within the byte limit.
Run `bash launch.sh` for an interactive session. EOF, Ctrl+C, or SIGTERM shuts
down the controller. Edit `bot.toml` to use a Meshtastic TCP or serial node.
Use `--config /path/to/local.toml` for a separate configuration file.

## Install as a Linux service

On supported Debian/Ubuntu or Raspberry Pi OS releases with systemd and system Python 3.11+:

```sh
sudo bash install.sh
```

The installer asks for the installation directory (default `/opt/meshbot`), system
group/user (both default `meshbot`), and TCP or serial radio settings. It shows a
bot/dependency summary and asks for approval before changing the host. Execution
uses quiet stage progress and root-only logs under `/var/log/meshbot/`, installs
dependencies and the venv, and enables/starts `meshbot.service`. The checkout can
live anywhere. `--dry-run` previews without changes; `-u`/`--uninstall` removes the
service/runtime while preserving configuration, plugins, and data. Add `--purge`
to explicitly remove those too. See the [installation guide](docs/install.md)
for dependencies, previews, automation, updates, and uninstall details.

## Architecture

```text
mesh_bot.py       Primary entry point
botcore/          Controller, plugin API, and core services
plugins.d/        Paired feature logic (.py) and configuration (.toml)
bot.toml          Core, transport, and policy configuration
tests/            Tests of the new runtime
docs/             Plugin development documentation
integration/      Original implementation awaiting integration
data/             New runtime state (generated locally)
```

Core services handle command routing, policy, events, interval scheduling,
message formatting and delivery, and plugin-scoped storage. Plugins register
their own commands, event subscriptions, jobs, and lifecycle hooks without
changes to core code. See the [plugin development guide](docs/plugins.md).

## Validation

```sh
venv/bin/python -m unittest discover -s tests -v
```

Command discovery and ping are available. Physical radio operation has not yet been validated.
The original modules, assets, documentation, and deployment scripts are preserved
in [integration/](integration/README.md) as migration reference; they are not part
of the supported runtime. Use the original meshing-around project if you need
the original bot's feature set today.

## Containers

```sh
docker compose build
docker compose run --rm mesh-bot
```

The container uses the same `mesh_bot.py` entry point and console default.
Configuration and plugin files are built into the image; rebuild after editing
them. Compose persists only the new runtime's `data/` directory.

This project retains the original GPL license and acknowledges meshing-around's
authors and contributors. The `mesh_bot.py` name pays tribute to those roots.
