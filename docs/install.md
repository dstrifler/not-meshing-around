# Linux service installation

The installer supports Debian 12+, Ubuntu 24.04+, and Raspberry Pi OS Bookworm+
with systemd and **system Python 3.11 or newer already available to run the wizard**.
The launcher performs no early package bootstrapping. If Python is missing or too
old, it exits without changing the host; provision a supported system Python first.
After approval, it installs OS dependencies through apt and Python dependencies
into a virtual environment. Network access to configured
apt and Python package repositories is required.

Check out the repository wherever you want, then run:

```sh
bash /path/to/not-meshing-around/install.sh
```

The wizard can plan a fresh installation without root. After approval it asks
for sudo authentication once and executes the approved plan as root. Running
with `sudo bash ...` or directly as root is also supported. Planning an existing
private installation may require starting with sudo so its configuration is
readable. The current working directory does not matter.
The installer locates its files relative to its own checkout and copies the
runtime into the chosen destination; it does not move the checkout or change
Git's global configuration. Execute the launcher; sourcing it is rejected,
and its directory handling does not change the caller's shell directory.
For example, starting the installer from `~/not-meshing-around` and choosing
`/opt/meshbot` leaves you in `~/not-meshing-around` when it finishes, fails, or
is cancelled. Venv and pip commands use the destination as their own working
directory without changing the installer's starting directory.

It asks for:

1. Installation directory, default `/opt/meshbot`.
2. Service system group, default `meshbot`.
3. Service system user, default `meshbot`.
4. TCP or serial radio connection.
5. TCP hostname/IP and port (defaults `localhost`, `4403`), or the serial device
   (default `/dev/ttyACM0`; a stable `/dev/serial/by-id/...` path is preferable).

All prompts and read-only validation finish before the installation summary.
The summary identifies the destination, account, radio endpoint, plugin/data
paths, service, preserved/replaced files, dependency changes, and log locations.
`Proceed with install? [y/N]` defaults to **No**. Declining or reaching EOF during
planning creates no files, logs, accounts, or package/service changes. No sudo
request occurs before approval when planning unprivileged.

The dependency preview lists these direct OS requirements:
`python3 python3-venv ca-certificates acl passwd util-linux`. It distinguishes
already-installed requirements and displays the complete simulated APT
install/upgrade transaction, including supporting packages and versions. Use
`--verbose` to include APT's complete simulation output. Planning uses the existing
package cache, no locks/downloads, and never runs `apt-get update`. If that cache
cannot resolve dependencies, the summary explicitly marks the preview incomplete.
After approval, the installer refreshes metadata and simulates again. A changed
transaction is displayed and requires a second approval before package installation;
`--yes`/noninteractive execution stops instead and asks you to rerun against the
refreshed cache. No full system upgrade, package removal, or autoremove is run.

Python requirements are listed separately: **Meshtastic Python client library**
(`meshtastic`) and `PyPubSub`, plus the supporting dependencies pip resolves.
These are venv packages; this does not install another `meshtasticd`. The operation
log records pip's resolved installed package versions.

TCP can connect to a network radio or an existing Meshtastic daemon. The installer
does not install or require `meshtasticd`. Console input is intended for interactive
development and is not offered for the service.

For a preview that makes no changes:

```sh
sudo bash install.sh --dry-run
```

Preview is also available without prompts:

```sh
bash install.sh --dry-run --non-interactive --install-dir /opt/meshbot
```

Every wizard parameter has a command-line equivalent: `--install-dir`,
`--service-user`, `--service-group`, `--radio-type tcp|serial`, `--radio-host`,
`--radio-port`, and `--serial-device`. Supplied values replace their prompts.
`--non-interactive` uses supplied values and existing/default values for the rest;
it requires explicit `--yes` (`-y`) except with `--dry-run`.

```sh
bash install.sh --non-interactive --yes --install-dir /opt/meshbot \
  --service-user meshbot --service-group meshbot \
  --radio-type tcp --radio-host localhost --radio-port 4403
```

`--yes` approves the displayed plan but does not waive changed-dependency checks.
Use `bash install.sh --help` for all options. A running systemd host is required
for execution; a dry run can also inspect the layout in a Debian-based container.

## Progress and installer logs

After approval, the terminal shows ASCII progress across named stages. Detailed
apt, venv/pip, validation, and service output goes to a timestamped root-only file:

```text
/var/log/meshbot/install-YYYYMMDD-HHMMSS-ffffff.log
/var/log/meshbot/uninstall-YYYYMMDD-HHMMSS-ffffff.log
```

Log directories use mode `0700`, log files `0600`, and existing logs are retained.
Each log records installer version, OS/architecture, approved parameters and
APT transaction, phase/command output and exit status, removal actions, and the
final result. Logs use local timestamps with timezone offsets. Redirected terminal
output receives timestamped stage lines without animation. A failure shows the
recent log excerpt and full log path, then exits unsuccessfully. Ctrl+C/SIGTERM
interrupt execution and are recorded; active command groups are terminated.
Configuration contents and environment variables are not dumped. Recognizable
password/key/token assignments, authorization headers, and URL credentials are
redacted from commands, output, and failure excerpts. Treat these operational
logs as sensitive even with redaction.

Installer logs are separate from application journald/syslog output. They remain
after uninstall and purge. No logs are created for declined plans or dry runs.

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
outside the core, plugins, installer, and venv, docs, and cache trees. Installer-managed trees must
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
data are retained during installation. It manages one `meshbot.service` per host.


## Uninstall and purge

Run the installer from your checkout (or its installed copy):

```sh
bash install.sh --uninstall --dry-run
bash install.sh -u
bash install.sh --uninstall --purge
```

Uninstall discovers the installation from root-owned metadata, displays its
removal/preservation summary, and defaults confirmation to No. It stops and
disables `meshbot.service`, removes the managed unit, reloads systemd, removes
unchanged inventoried runtime files plus venv/cache, and verifies service removal.
The inventory lives at `/var/lib/meshbot-installer/installation.json`, outside the
service-owned tree. It records file hashes, paths, account creation information,
package transaction/pre-existing requirements, runtime digest, and installation
status. It is written before deployment so interrupted installs remain removable.
Deployed hashes remain authoritative during an update; planned replacements have
separate pending hashes. Runtime files are replaced atomically, then each deployed
hash is committed. If interruption lands between replacement and that commit,
uninstall accepts either known version. A retry recovers only pending hashes that
match the file actually on disk; it does not adopt locally modified content.
The service-owned `.meshbot-install.json` remains only a reinstall/defaults marker;
it does not authorize root file deletion.

Ordinary uninstall preserves:

- `bot.toml` and configuration backups;
- the full plugin directory, including custom plugins and settings;
- the configured data directory/database;
- modified runtime files and unknown files;
- system accounts/groups, traversal ACLs, OS packages, and installer logs.

`--purge` must accompany `--uninstall`. It adds removal of `bot.toml`, its installer
backups, the entire recorded plugin directory, and the entire recorded data
directory. This includes custom plugins and stored data; inspect the summary
before approving. Other unknown/modified files and Git history remain, so the
installation root may remain. Shared system packages, accounts, and ACLs are
never automatically removed, even with purge. The root-owned inventory and logs
are retained for diagnostics. Reinstall into the same directory can reuse
preserved settings; choosing another directory requires purging the prior
recorded installation first.

For installations made before inventory support, `--uninstall` offers **service
removal only** and retains every installation file. `--purge` is refused without
a trusted inventory. Rerun the new installer at the existing destination first
if you want full inventoried removal. An unrelated service, untrusted metadata,
or redirected installation path is rejected. Removal walks opened directories
without following symlinks, and modified files are retained rather than guessed.
