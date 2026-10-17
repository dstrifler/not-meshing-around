# Linux service installation

The installer supports Debian 12+, Ubuntu 24.04+, and Raspberry Pi OS Bookworm+
with systemd and Python 3.11 or newer. It installs OS dependencies through apt
and Python dependencies into a virtual environment. Network access to configured
apt and Python package repositories is required.

Check out the repository wherever you want, then run:

```sh
sudo bash /path/to/not-meshing-around/install.sh
```

When already root, omit `sudo`. The current working directory does not matter.
The installer locates its files relative to its own checkout and copies the
runtime into the chosen destination; it does not move the checkout or change
Git's global configuration.

It asks for:

1. Installation directory, default `/opt/meshbot`.
2. Service system group, default `meshbot`.
3. Service system user, default `meshbot`.
4. TCP or serial radio connection.
5. TCP hostname/IP and port (defaults `localhost`, `4403`), or the serial device
   (default `/dev/ttyACM0`; a stable `/dev/serial/by-id/...` path is preferable).

TCP can connect to a network radio or an existing Meshtastic daemon. The installer
does not install or require `meshtasticd`. Console input is intended for interactive
development and is not offered for the service.

For a preview that makes no changes:

```sh
sudo bash install.sh --dry-run
```

The preview requires Python 3.11+ to be installed already. A real install can
bootstrap Python and venv support from apt on supported releases.

## Files, accounts, and permissions

The installer creates non-root system accounts with a disabled login shell. It
can reuse a compatible system account with the selected primary group; it refuses
root, normal login accounts, or accounts in privileged groups. The service runs
as the selected user and group, independently of the person installing it.

The installation contains the runtime, enabled plugin pairs, configuration,
documentation, and a freshly created `venv/`; it excludes Git history, tests,
the archived original bot, and the checkout's virtual environment. Python packages
are installed as the service account. The selected user and group own the entire
installation tree recursively: its root directory, code, configuration and backups,
plugins, venv, data, and `.cache/pip`. Reruns repair existing ownership before pip
runs and reapply it after dependency installation and bot validation, including
when those checks fail. Venv symlink targets outside the installation are untouched.
The systemd unit in `/etc/systemd/system` remains owned by root.

Both dependency installation and `pip check` explicitly use the service-owned
`<installation>/.cache/pip`, so downloads and built wheels can be reused on later
installs. This also works when reusing an account whose home directory is elsewhere.
Updating the checkout and rerunning the installer repairs the cache ownership of
earlier installations. Systemd retains a read-only filesystem for the running bot
except its data and `.cache` directories and normal virtual filesystems; this is
independent of the installation's on-disk ownership.

Installation paths with spaces and systemd metacharacters are escaped. A destination
under a private home directory receives narrowly scoped traversal ACLs on otherwise
inaccessible parents, without granting directory listing or write access. The
service does not hide home directories. Use a dedicated installation directory;
the installer refuses unrelated nonempty directories and unrelated existing
`meshbot.service` units. Installing into the checkout root itself is supported,
but transfers ownership of that tree to the service account; a separate checkout
is easier to update with normal Git commands.

For installer-managed layouts, `core.plugins` and `core.database` must resolve
inside the installation directory. The database needs a separate data directory
outside the core, plugins, installer, and venv trees. Installer-managed trees must
not contain symlinks; the venv's normal interpreter/library symlinks are handled
without changing the files they point to.

Serial services receive the `dialout` supplementary group. An existing device must
be a character device owned by that group; set an appropriate udev rule if your
device uses different permissions. The installer does not grant root-group access
or make serial devices world-writable. A selected device can be absent during
installation, but must be connected before the bot can operate.

## Service and configuration

The installer validates the installed bot using `--check` as its service account,
writes `/etc/systemd/system/meshbot.service`, reloads systemd, enables the unit,
and starts it. The unit uses the installed venv and an absolute configuration
path; it does not depend on shell activation or an interactive terminal.

```sh
sudo systemctl status meshbot.service
sudo journalctl -u meshbot.service -f
sudo systemctl restart meshbot.service
```

Configuration is `<installation>/bot.toml`; plugin settings are in its plugin
directory. The new checkout's default configuration is also saved as
`bot.toml.dist`. Service enable/start and `--check` are not hardware connection
tests: inspect the journal for radio readiness or startup errors. The service
restarts after failures, with a rate limit to avoid an endless rapid restart loop.

Logging defaults to readable text in the journal, with message contents disabled.
The `[logging]` table in `bot.toml` can select journald, TCP/UDP syslog, or both;
see [logging configuration](logging.md). Updates preserve existing configuration,
so add the table manually to customize it. The installer captures stdout/stderr
in the journal but does not change host journal retention or configure rsyslog.

## Reinstalling or updating

Update your original checkout, then rerun its installer and choose the existing
installation directory. Account names and radio settings from that installation
become the prompt defaults. The installer records its destination early so a
failed first installation can be resumed.

Reruns update runtime and plugin Python code and install dependencies, while
preserving existing plugin TOML settings, custom plugins, and state files. Existing
`bot.toml` values outside the prompted radio settings are preserved. If radio
settings change, the original file is backed up as
`bot.toml.before-install-<timestamp>` before rewriting it. TOML values are retained;
comments remain in the backup. Unchanged configuration files are not rewritten.

The existing service is stopped before updating its executable files. Failed
checks or dependency installs stop the installer without claiming success; a
previous service may remain stopped until the problem is fixed and installation
is rerun. Runtime files are not rolled back automatically, and accounts or existing
data are never deleted by this installer. It manages one `meshbot.service` per host.
