"""Interactive Debian/systemd deployment. Invoked by the repository's install.sh."""

import argparse
import contextlib
import datetime
import grp
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
UNIT_PATH = Path('/etc/systemd/system/meshbot.service')
SERVICE = 'meshbot.service'
MARKER = '.meshbot-install.json'
MANAGED = '# Managed by not-meshing-around installer: '
PACKAGES = ('python3', 'python3-venv', 'ca-certificates', 'acl', 'passwd', 'util-linux')
STATE = Path('/var/lib/meshbot-installer')
LOGS = Path('/var/log/meshbot')
MANIFEST = 'installation.json'
INSTALLER_VERSION = 2
ROOT_UID = 0
ACTIVE_LOG = None
CHILD_EXECUTION = False


def sanitize(value):
    value = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@', r'\1<redacted>@', str(value))
    value = re.sub(r'(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+', r'\1<redacted>', value)
    value = re.sub(r'''(?ix)\b((?:(?:private|public|api|access|channel)[_ -]?)?key|password|passwd|token|secret|psk)
                       \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1=<redacted>', value)
    return ''.join(char if char in '\n\t' or ord(char) >= 32 and ord(char) != 127 else '?' for char in value)


def secure_directory(path, mode, *, shared_log_parent=False):
    """Root metadata must not be replaceable through a writable ancestor."""
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError(f'Root metadata path must not contain symlinks: {parent}')
        if not parent.exists():
            parent.mkdir(mode=mode if parent == path else 0o755)
        info = parent.stat()
        forbidden = 0o002 if shared_log_parent and parent == Path('/var/log') else 0o022
        if info.st_uid != ROOT_UID or info.st_mode & forbidden or not parent.is_dir():
            raise ValueError(f'Root metadata directory is not securely owned: {parent}')
    if not shared_log_parent:
        path.chmod(mode)


def trusted_file(path):
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if parent.is_symlink() or not parent.is_dir() or info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise ValueError(f'Untrusted installer metadata directory: {parent}')
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != ROOT_UID or info.st_mode & 0o022:
        raise ValueError(f'Untrusted installer metadata: {path}')


class OperationLog:
    def __init__(self, action):
        self.action, self.path, self.stream = action, None, None
        self.tail = deque(maxlen=20)

    def __enter__(self):
        global ACTIVE_LOG
        secure_directory(LOGS, 0o700, shared_log_parent=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        self.path = LOGS / f'{self.action}-{stamp}.log'
        directory = os.open(LOGS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != ROOT_UID or info.st_mode & 0o022:
                raise ValueError('Log directory changed ownership or permissions before it was opened.')
            # /var/log may be shared with syslog. Anchor chmod/create to the
            # checked directory, so replacing its pathname cannot redirect I/O.
            os.fchmod(directory, 0o700)
            descriptor = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
        finally:
            os.close(directory)
        self.stream = os.fdopen(descriptor, 'w', encoding='utf-8')
        ACTIVE_LOG = self
        try:
            self.write(f'Installer version={INSTALLER_VERSION} action={self.action} uid={os.geteuid()}')
            self.write(f'Platform={os.uname().sysname} architecture={os.uname().machine}')
            release = Path('/etc/os-release')
            self.write(release.read_text() if release.is_file() else 'OS release metadata unavailable')
            print(f'Log: {self.path}')
        except BaseException:
            ACTIVE_LOG = None
            self.stream.close()
            raise
        return self

    def write(self, value):
        for line in sanitize(value).splitlines():
            rendered = f'{datetime.datetime.now().astimezone().isoformat(timespec="seconds")} {line}'
            self.stream.write(rendered + '\n')
            self.tail.append(rendered)
        self.stream.flush()

    def __exit__(self, kind, error, traceback):
        global ACTIVE_LOG
        try:
            self.write('SUCCESS' if error is None else f'FAILED: {error}')
            if error is not None:
                print('\nOperation failed. Recent log output:', file=sys.stderr)
                print('\n'.join(self.tail), file=sys.stderr)
                print(f'Full log: {self.path}', file=sys.stderr)
        finally:
            ACTIVE_LOG = None
            self.stream.close()


def progress(index, total, label):
    filled = 20 * index // total
    stamp = '' if sys.stdout.isatty() else datetime.datetime.now().astimezone().isoformat(timespec='seconds') + ' '
    print(f'{stamp}[{"#" * filled}{"." * (20 - filled)}] {index}/{total} {label}', flush=True)
    if ACTIVE_LOG:
        ACTIVE_LOG.write(f'PHASE {index}/{total}: {label}')


@dataclass(frozen=True)
class Options:
    prefix: Path
    user: str
    group: str
    radio: dict


@dataclass(frozen=True)
class Dependencies:
    present: tuple = ()
    changes: tuple = ()
    details: str = ''
    available: bool = True

    def signature(self):
        return self.available, tuple(sorted(self.changes))


def dependency_plan():
    """Read the existing dpkg/APT cache without locks, downloads, or writes."""
    environment = os.environ | {'LC_ALL': 'C'}
    installed = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\t${Version}\n',
                                *PACKAGES], capture_output=True, text=True, env=environment)
    present = []
    for line in installed.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) == 3 and fields[1] == 'installed':
            present.append((fields[0], fields[2]))
    simulation = subprocess.run(['apt-get', '-s', '-o', 'Debug::NoLocking=1', '--no-remove',
                                 'install', *PACKAGES], capture_output=True, text=True, env=environment)
    details = sanitize(simulation.stdout + simulation.stderr)
    if simulation.returncode:
        return Dependencies(tuple(present), (), details, False)
    changes = []
    for line in simulation.stdout.splitlines():
        match = re.match(r'^Inst (\S+)(?: \[([^\]]+)\])? \((\S+)', line)
        if match:
            changes.append((match[1], match[2] or '', match[3]))
        elif line.startswith('Remv '):
            raise ValueError('APT proposed package removals; refusing this dependency transaction.')
    return Dependencies(tuple(present), tuple(changes), details)


def show_dependencies(plan, source=SOURCE, verbose=False):
    print('\nOS requirements: ' + ' '.join(PACKAGES))
    print('Already installed: ' + (', '.join(f'{name} ({version})' for name, version in plan.present) or 'none detected'))
    if plan.available:
        print(f'APT transaction: {len(plan.changes)} package installs/upgrades, no removals')
        for name, old, version in plan.changes:
            print(f'  {name}: {old + " -> " if old else "new "}{version}')
    else:
        print('APT preview unavailable with the current cache. After refresh, the package plan must be reviewed again.')
        print(plan.details[-1500:])
    if verbose and plan.available:
        print(plan.details)
    requirements = [line.strip() for line in (source / 'requirements.txt').read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#')]
    print('Python requirements (installed in the bot venv):')
    for requirement in requirements:
        label = ' (Meshtastic Python client library; uses your existing radio/meshtasticd)' if requirement == 'meshtastic' else ''
        print(f'  {sanitize(requirement)}{label}')
    print('pip also installs their supporting dependencies; resolved versions are recorded in the install log.')


def confirm(label):
    if CHILD_EXECUTION:
        # The approved plan arrived over stdin; sudo authentication uses the TTY.
        with open('/dev/tty', 'r+') as terminal:
            terminal.write(label + ' [y/N]: ')
            terminal.flush()
            answer = terminal.readline()
    else:
        answer = input(label + ' [y/N]: ')
    return answer.strip().lower() in ('y', 'yes')


def run(*args, **kwargs):
    command = list(map(str, args))
    if ACTIVE_LOG is None:
        return subprocess.run(command, check=True, text=True, **kwargs)
    ACTIVE_LOG.write('$ ' + shlex.join(command))
    capture_output = kwargs.pop('stdout', None) == subprocess.PIPE
    output = []
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace', start_new_session=True, **kwargs) as child:
        try:
            for line in child.stdout:
                ACTIVE_LOG.write(line)
                if capture_output:
                    output.append(line)
            status = child.wait()
        except BaseException:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            raise
    ACTIVE_LOG.write(f'exit_status={status}')
    result = subprocess.CompletedProcess(command, status, ''.join(output))
    if status:
        raise subprocess.CalledProcessError(status, command, output=result.stdout)
    return result


def capture(*args):
    return run(*args, stdout=subprocess.PIPE).stdout.strip()


def service_fragment():
    try:
        output = capture('systemctl', 'show', SERVICE, '--property=LoadState', '--property=FragmentPath')
    except subprocess.CalledProcessError as error:
        # systemctl versions can return failure for a not-yet-installed unit.
        output = error.stdout or ''
        if 'LoadState=not-found' not in output.splitlines():
            raise
    properties = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if properties.get('LoadState') == 'not-found':
        return ''
    if properties.get('LoadState') != 'loaded' or not properties.get('FragmentPath'):
        raise ValueError(f'{SERVICE} is not a normal loaded service; inspect its existing systemd configuration.')
    return properties['FragmentPath']


def account_name(value):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', value) or value == 'root':
        raise ValueError('Use a non-root account name of 1–31 lowercase letters, digits, underscores, or hyphens.')
    return value


def absolute_path(value):
    if not value.startswith('/') or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Use an absolute path without control characters.')
    return Path(value)


def install_path(value):
    path = absolute_path(value).resolve()
    absolute_path(str(path))  # Symlink targets must obey the same unit-file rules.
    if path == Path('/'):
        raise ValueError('The filesystem root cannot be the installation directory.')
    return path


def hostname(value):
    if (not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or '://' in value or any(char in value for char in '/@')):
        raise ValueError('Enter a hostname or IP address, without a URL scheme or whitespace.')
    return value


def port_number(value):
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('TCP port must be an integer from 1 to 65535.')
    return int(value)


def prompt(label, default, validate=str):
    while True:
        value = input(f'{label} [{default}]: ').strip() or str(default)
        try:
            return validate(value)
        except ValueError as error:
            print(error, file=sys.stderr)


def read_config(path):
    with path.open('rb') as stream:
        return tomllib.load(stream)


def collect_options(source, args=None):
    def setting(label, default, validate=str, argument=None):
        value = getattr(args, argument, None) if argument else None
        if value is not None:
            return validate(str(value))
        if getattr(args, 'non_interactive', False):
            return validate(str(default))
        return prompt(label, default, validate)

    prefix = setting('Installation directory', '/opt/meshbot', install_path, 'install_dir')
    marker = json.loads((prefix / MARKER).read_text()) if (prefix / MARKER).is_file() else {}
    if not isinstance(marker, dict):
        raise ValueError('Invalid installer metadata in the selected destination.')
    group = setting('Service system group', marker.get('group', 'meshbot'), account_name, 'service_group')
    user = setting('Service system user', marker.get('user', 'meshbot'), account_name, 'service_user')
    config_path = prefix / 'bot.toml' if (prefix / 'bot.toml').is_file() else source / 'bot.toml'
    config = read_config(config_path)
    old = config.get('radio', {})

    def radio_type(value):
        if value.lower() not in ('tcp', 'serial'):
            raise ValueError('Choose tcp or serial. Console input is not suitable for a service.')
        return value.lower()

    kind = setting('Radio connection (tcp/serial)', old.get('type') if old.get('type') in ('tcp', 'serial') else 'tcp', radio_type, 'radio_type')
    if kind == 'tcp':
        if getattr(args, 'serial_device', None):
            raise ValueError('--serial-device requires --radio-type serial.')
        radio = {'type': kind, 'hostname': setting('Radio hostname or IP', old.get('hostname', 'localhost'), hostname, 'radio_host'),
                 'port': setting('Radio TCP port', old.get('port', 4403), port_number, 'radio_port')}
    else:
        if getattr(args, 'radio_host', None) or getattr(args, 'radio_port', None):
            raise ValueError('--radio-host/--radio-port require --radio-type tcp.')
        radio = {'type': kind, 'device': str(setting('Serial device (prefer /dev/serial/by-id/...)',
                 old.get('device') or '/dev/ttyACM0', absolute_path, 'serial_device'))}
    return Options(prefix, user, group, radio), config


def inside(prefix, value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{label} must be a nonempty path.')
    path = (prefix / value).resolve()
    absolute_path(str(path))
    if not path.is_relative_to(prefix) or path == prefix:
        raise ValueError(f'{label} must be inside the installation directory in its own subdirectory.')
    return path


def configure(options, config):
    if not isinstance(config.get('core', {}), dict) or not isinstance(config.get('radio', {}), dict):
        raise ValueError('core and radio must be TOML tables.')
    config = dict(config)
    radio = {key: value for key, value in config.get('radio', {}).items()
             if key not in ('type', 'hostname', 'port', 'device')}
    config['radio'] = radio | options.radio
    core = config.get('core', {})
    plugins = inside(options.prefix, core.get('plugins', 'plugins.d'), 'core.plugins')
    database = inside(options.prefix, core.get('database', 'data/core.db'), 'core.database')
    data = database.parent
    protected = (plugins, *(options.prefix / name for name in ('botcore', 'venv', 'scripts', 'docs', '.cache')))
    if data == options.prefix or any(data.is_relative_to(path) or path.is_relative_to(data) for path in protected):
        raise ValueError('The database needs a separate data directory, outside code, plugins, and venv.')
    if any(plugins.is_relative_to(path) or path.is_relative_to(plugins) for path in protected[1:]):
        raise ValueError('The plugin directory must be separate from core code, installer code, and venv.')
    if options.radio['type'] == 'serial':
        device = Path(options.radio['device'])
        if device.exists() and not device.is_char_device():
            raise ValueError('The selected serial path exists but is not a character device.')
        if device.exists() and grp.getgrgid(device.stat().st_gid).gr_name != 'dialout':
            raise ValueError('Configure the serial device with group dialout (for example via udev), then rerun.')
    return config, plugins, data


def system_limit(name, default):
    text = Path('/etc/login.defs').read_text()
    match = re.search(rf'^\s*{name}\s+(\d+)\s*(?:#.*)?$', text, re.MULTILINE)
    return int(match[1]) if match else default


def lookup(function, name):
    try:
        return function(name)
    except KeyError:
        return None


def validate_accounts(options):
    account_name(options.user)
    account_name(options.group)
    group = lookup(grp.getgrnam, options.group)
    user = lookup(pwd.getpwnam, options.user)
    gid_max = system_limit('SYS_GID_MAX', system_limit('GID_MIN', 1000) - 1)
    uid_max = system_limit('SYS_UID_MAX', system_limit('UID_MIN', 1000) - 1)
    if group and not 0 < group.gr_gid <= gid_max:
        raise ValueError('Existing group is not a non-root system group; choose another name.')
    if user:
        if not 0 < user.pw_uid <= uid_max or Path(user.pw_shell).name not in ('nologin', 'false'):
            raise ValueError('Existing user must be a non-root system account with a disabled login shell.')
        if not group or user.pw_gid != group.gr_gid:
            raise ValueError('Existing user must already have the selected primary group; choose a different account.')
        privileged = {'root', 'sudo', 'wheel', 'docker', 'lxd', 'disk', 'shadow'}
        if any(grp.getgrgid(gid).gr_name in privileged for gid in os.getgrouplist(options.user, user.pw_gid)):
            raise ValueError('Existing user belongs to a privileged group; choose a dedicated service account.')
    return user, group


def validate_destination(options, source):
    prefix = options.prefix
    if (UNIT_PATH.is_relative_to(prefix) or
            any(prefix.is_relative_to(path) or path.is_relative_to(prefix) for path in (STATE, LOGS))):
        raise ValueError('Installation directory must be separate from root-owned installer state, logs, and service files.')
    if prefix != source and any(prefix.is_relative_to(source / name) for name in ('botcore', 'plugins.d', 'scripts', 'docs')):
        raise ValueError('Choose a destination outside the checkout\'s runtime subdirectories, or the checkout root itself.')
    if prefix.exists():
        if not prefix.is_dir():
            raise ValueError('Installation destination is not a directory.')
        if any(prefix.iterdir()) and prefix != source and not (prefix / MARKER).is_file():
            raise ValueError('Destination is not empty or an installer-managed installation; choose another directory.')
    if UNIT_PATH.is_symlink():
        raise ValueError(f'Refusing to replace a symlink at {UNIT_PATH}.')
    if UNIT_PATH.exists() and not UNIT_PATH.read_text().startswith(MANAGED + json.dumps(str(prefix)) + '\n'):
        raise ValueError(f'{UNIT_PATH} already belongs to another installation or service.')
    # These trees are managed as code or bot-owned data; never follow links out.
    for name in ('bot.toml', 'venv', 'botcore', 'plugins.d', 'scripts', '.cache', MARKER):
        path = prefix / name
        if path.is_symlink():
            raise ValueError(f'Installer-managed paths must not be symlinks: {path}')
        if path.exists() and name in ('venv', 'botcore', 'plugins.d', 'scripts', '.cache') and not path.is_dir():
            raise ValueError(f'Installer-managed directory is not a directory: {path}')


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(key) + ' = ' + toml_value(item) for key, item in value.items()) + '}'
    raise ValueError(f'Unsupported TOML value: {type(value).__name__}')


def dump_config(config):
    lines = []

    def table(value, path=()):
        if path:
            lines.extend(('', '[' + '.'.join(json.dumps(key) for key in path) + ']'))
        for key, item in value.items():
            if not isinstance(item, dict):
                lines.append(json.dumps(key) + ' = ' + toml_value(item))
        for key, item in value.items():
            if isinstance(item, dict):
                table(item, (*path, key))

    table(config)
    text = '\n'.join(lines).lstrip('\n') + '\n'
    if tomllib.loads(text) != config:
        raise ValueError('Configuration could not be preserved during serialization.')
    return text


def unit_quote(value):
    """Quote ExecStart arguments and whitespace-separated path lists."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_unit(options, data):
    prefix = options.prefix
    serial_group = 'SupplementaryGroups=dialout\n' if options.radio['type'] == 'serial' else ''
    return (f'{MANAGED}{json.dumps(str(prefix))}\n[Unit]\nDescription=not-meshing-around mesh bot\n'
            'Wants=network-online.target\nAfter=network-online.target\n'
            'StartLimitIntervalSec=120\nStartLimitBurst=3\n\n[Service]\nType=simple\n'
            f'User={options.user}\nGroup={options.group}\n{serial_group}'
            # WorkingDirectory is a scalar path: quotes would become part of
            # the path. A final slash prevents a trailing backslash or space
            # in a directory name from becoming unit-file continuation/trim.
            f'WorkingDirectory={str(prefix).replace("%", "%%")}/\n'
            # env execs the venv interpreter without a shell. Keeping the first
            # executable path fixed also supports characters systemd forbids
            # in that field; ':' disables $VARIABLE expansion in arguments.
            f'ExecStart=:/usr/bin/env -- {unit_quote(prefix / "venv/bin/python")} {unit_quote(prefix / "mesh_bot.py")} '
            f'--config {unit_quote(prefix / "bot.toml")}\n'
            'Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n'
            'StandardOutput=journal\nStandardError=journal\nSyslogIdentifier=meshbot\nSyslogLevelPrefix=yes\n'
            'Restart=on-failure\nRestartSec=5\nKillSignal=SIGTERM\nTimeoutStopSec=90\n'
            'UMask=0027\nNoNewPrivileges=true\nProtectSystem=strict\n'
            'ProtectKernelTunables=true\nProtectControlGroups=true\nRestrictSUIDSGID=true\n'
            f'ReadWritePaths={unit_quote(data)} {unit_quote(prefix / ".cache")}\n\n[Install]\nWantedBy=multi-user.target\n')


def reject_links(path):
    if path.is_symlink():
        raise ValueError(f'Refusing to follow a symlink in an installer-managed tree: {path}')
    if path.is_dir():
        for child in path.iterdir():
            reject_links(child)


def own_tree(path, uid, gid, *, code=False):
    reject_links(path)
    for root, directories, files in os.walk(path):
        directory = Path(root)
        os.chown(directory, uid, gid)
        directory.chmod(0o750)
        for name in files:
            file = directory / name
            os.chown(file, uid, gid)
            if code:
                file.chmod(0o750 if file.stat().st_mode & 0o111 else 0o640)
            else:
                file.chmod(0o640)


def write_owned(path, text, uid, gid, mode=0o640):
    if path.is_symlink():
        raise ValueError(f'Refusing to replace a symlink: {path}')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.chown(temporary, uid, gid)
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def stage_runtime(source, stage):
    for name in ('mesh_bot.py', 'requirements.txt', 'bot.toml', 'launch.sh', 'bootstrap.sh', 'install.sh',
                 'README.md', 'LICENSE.md', 'botcore', 'plugins.d', 'scripts', 'docs'):
        path = source / name
        reject_links(path)
        if path.is_dir():
            shutil.copytree(path, stage / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        else:
            shutil.copy2(path, stage / name)


def deploy_runtime(stage, prefix, plugins, uid, gid):
    for name in ('botcore', 'scripts', 'docs'):
        target = prefix / name
        reject_links(target)
        shutil.copytree(stage / name, target, dirs_exist_ok=True)
        own_tree(target, uid, gid, code=True)
    plugins.mkdir(parents=True, exist_ok=True)
    reject_links(plugins)
    for source in (stage / 'plugins.d').iterdir():
        target = plugins / source.name
        if source.suffix == '.toml' and target.exists():
            continue
        if source.is_file() and source.suffix in ('.py', '.toml'):
            shutil.copy2(source, target)
    own_tree(plugins, uid, gid, code=True)
    for name in ('mesh_bot.py', 'requirements.txt', 'launch.sh', 'bootstrap.sh', 'install.sh', 'README.md', 'LICENSE.md'):
        target = prefix / name
        if target.is_symlink():
            raise ValueError(f'Refusing to replace a symlink: {target}')
        shutil.copy2(stage / name, target)
        os.chown(target, uid, gid)
        target.chmod(0o750 if name.endswith('.sh') else 0o640)
    write_owned(prefix / 'bot.toml.dist', (stage / 'bot.toml').read_text(), uid, gid)


def own_installation(prefix, uid, gid):
    """Repair the whole installation, including old files, without following links."""
    def traversal_failed(error):
        raise error
    for root, directories, files in os.walk(prefix, followlinks=False, onerror=traversal_failed):
        os.chown(root, uid, gid, follow_symlinks=False)
        for name in (*directories, *files):
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)


def grant_traversal(parent, user):
    acl = capture('getfacl', '--omit-header', '--numeric', '--', parent)
    mask = re.search(r'^mask::([rwx-]{3})', acl, re.MULTILINE) or re.search(r'^group::([rwx-]{3})', acl, re.MULTILINE)
    if not mask:
        raise ValueError(f'Could not read directory access permissions: {parent}')
    # Only expand the shared ACL mask for traversal. Letting setfacl recalculate
    # it from every entry could unintentionally enable masked read/write access.
    permissions = mask[1][:2] + 'x'
    run('setfacl', '-n', '-m', f'u:{user}:--x,m::{permissions}', '--', parent)


RUNTIME_FILES = ('mesh_bot.py', 'requirements.txt', 'launch.sh', 'bootstrap.sh', 'install.sh', 'README.md', 'LICENSE.md')


def relative_path(value):
    if (not isinstance(value, str) or not value or Path(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid relative path in installation inventory.')
    return value


def read_manifest():
    path = STATE / MANIFEST
    if not path.exists() and not path.is_symlink():
        return None
    trusted_file(path)
    record = json.loads(path.read_text())
    if (not isinstance(record, dict) or record.get('version') != INSTALLER_VERSION
            or record.get('service') != str(UNIT_PATH)
            or not isinstance(record.get('files'), dict)
            or any(not isinstance(record.get(key), str) for key in ('prefix', 'user', 'group', 'plugins', 'data'))
            or record.get('status') not in ('installing', 'installed', 'uninstalled', 'purged')):
        raise ValueError('Unrecognized root-owned installation inventory.')
    prefix = install_path(record['prefix'])
    if str(prefix) != record['prefix']:
        raise ValueError('Installation directory has changed through a symlink; refusing removal.')
    for name, digest in record['files'].items():
        relative_path(name)
        if (Path(name).parts[0] not in (*RUNTIME_FILES, 'bot.toml.dist', 'botcore', 'scripts', 'docs')
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Invalid file inventory.')
    for name in (record['plugins'], record['data']):
        relative_path(name)
    account_name(record['user'])
    account_name(record['group'])
    return record


def save_manifest(record):
    secure_directory(STATE, 0o755)
    write_owned(STATE / MANIFEST, json.dumps(record, indent=2) + '\n', 0, 0, 0o644)


def inventory(stage, options, plugins, data, previous):
    files = dict(previous['files']) if previous else {}
    for name in (*RUNTIME_FILES, 'botcore', 'scripts', 'docs'):
        path = stage / name
        sources = sorted(path.rglob('*')) if path.is_dir() else [path]
        for source in sources:
            if source.is_file():
                files[str(source.relative_to(stage))] = hashlib.sha256(source.read_bytes()).hexdigest()
    files['bot.toml.dist'] = hashlib.sha256((stage / 'bot.toml').read_bytes()).hexdigest()
    return {'version': INSTALLER_VERSION, 'prefix': str(options.prefix), 'service': str(UNIT_PATH),
            'user': options.user, 'group': options.group,
            'plugins': str(plugins.relative_to(options.prefix)), 'data': str(data.relative_to(options.prefix)),
            'files': files, 'status': 'installing',
            'runtime_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            'updated': datetime.datetime.now().astimezone().isoformat()}


@contextlib.contextmanager
def removal_parent(prefix, relative):
    """Anchor deletion to opened directories, never following service-owned links."""
    relative_path(relative)
    descriptors = []
    try:
        # Walk from / as the installation's parents can also be under a home.
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for part in (*prefix.parts[1:], *Path(relative).parts[:-1]):
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        yield descriptor, Path(relative).name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def remove_recorded_file(prefix, relative, digest=None):
    try:
        with removal_parent(prefix, relative) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError(f'Refusing non-regular file: {relative}')
                matches = digest is None or hashlib.file_digest(stream, 'sha256').hexdigest() == digest
            if matches:
                os.unlink(name, dir_fd=parent)
                if ACTIVE_LOG:
                    ACTIVE_LOG.write(f'Removed file: {relative}')
            else:
                print(f'Keeping modified file: {relative}')
    except FileNotFoundError:
        pass


def remove_recorded_tree(prefix, relative):
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('This platform lacks safe directory removal support.')
    try:
        with removal_parent(prefix, relative) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f'Refusing to remove a redirected directory: {relative}')
            shutil.rmtree(name, dir_fd=parent)
            if ACTIVE_LOG:
                ACTIVE_LOG.write(f'Removed directory: {relative}')
    except FileNotFoundError:
        pass


def uninstall_plan(prefix=None, purge=False):
    record = read_manifest()
    if record:
        recorded = Path(record['prefix'])
        if prefix is not None and prefix != recorded:
            raise ValueError('Selected directory does not match the root-owned installation inventory.')
        prefix = recorded
    elif UNIT_PATH.exists():
        trusted_file(UNIT_PATH)
        header = UNIT_PATH.read_text().partition('\n')[0]
        if not header.startswith(MANAGED):
            raise ValueError('The existing service is not managed by this installer.')
        recorded = install_path(json.loads(header[len(MANAGED):]))
        if prefix is not None and prefix != recorded:
            raise ValueError('Selected directory does not match the managed service.')
        prefix = recorded
    else:
        raise ValueError('No installer-managed installation was found.')
    if UNIT_PATH.exists() or UNIT_PATH.is_symlink():
        trusted_file(UNIT_PATH)
        if not UNIT_PATH.read_text().startswith(MANAGED + json.dumps(str(prefix)) + '\n'):
            raise ValueError('The systemd unit belongs to another installation.')
    loaded = service_fragment() if Path('/run/systemd/system').is_dir() else ''
    if loaded and Path(loaded) != UNIT_PATH:
        raise ValueError('A different service unit is loaded; refusing uninstall.')
    if purge and not record:
        raise ValueError('Legacy installation has no trusted file inventory. Rerun the installer before using --purge.')
    if record:
        # Old layouts must not allow cache/venv removal to consume retained data.
        retained = [Path(record['plugins']), Path(record['data'])]
        for tree in ('venv', '.cache', 'botcore', 'scripts', 'docs'):
            if any(path.is_relative_to(tree) or Path(tree).is_relative_to(path) for path in retained):
                raise ValueError('Inventory overlaps runtime and preserved data directories.')
    return {'action': 'uninstall', 'prefix': str(prefix), 'purge': purge, 'record': record}


def uninstall(plan):
    # Re-read trusted metadata and check it against the exact displayed plan.
    current = uninstall_plan(Path(plan['prefix']), plan['purge'])
    if current != plan:
        raise ValueError('Installation inventory changed after approval; rerun to review it.')
    prefix, record = Path(plan['prefix']), plan['record']
    progress(1, 4, 'Stopping and disabling service')
    if UNIT_PATH.exists() or service_fragment():
        run('systemctl', 'stop', SERVICE)
    if UNIT_PATH.exists():
        run('systemctl', 'disable', SERVICE)
        UNIT_PATH.unlink()
    run('systemctl', 'daemon-reload')
    progress(2, 4, 'Removing recorded runtime files')
    if record:
        for relative, digest in record['files'].items():
            remove_recorded_file(prefix, relative, digest)
        for relative in ('venv', '.cache'):
            remove_recorded_tree(prefix, relative)
        progress(3, 4, 'Removing configuration and data' if plan['purge'] else 'Preserving configuration, plugins, and data')
        if plan['purge']:
            for relative in (record['plugins'], record['data']):
                remove_recorded_tree(prefix, relative)
            for relative in ('bot.toml', MARKER, *(path.name for path in prefix.glob('bot.toml.before-install-*'))):
                remove_recorded_file(prefix, relative)
        directories = {str(parent) for relative in record['files'] for parent in Path(relative).parents if str(parent) != '.'}
        for relative in sorted(directories, key=lambda value: len(Path(value).parts), reverse=True):
            try:
                with removal_parent(prefix, relative) as (parent, name):
                    os.rmdir(name, dir_fd=parent)
            except (FileNotFoundError, OSError):
                pass  # Retain directories containing modified or unknown files.
        record = dict(record, status='purged' if plan['purge'] or record['status'] == 'purged' else 'uninstalled')
        save_manifest(record)
    else:
        progress(3, 4, 'Legacy installation: retaining all files (no trusted inventory)')
    progress(4, 4, 'Verifying service removal')
    if service_fragment():
        raise ValueError('The service remains loaded after removal; inspect the systemd configuration.')
    print(f'Uninstalled {SERVICE}; accounts, OS packages, and logs retained.')
    print(f'Retained files, if any: {prefix}')


def preflight(options, config, source, *, inspect_service=True):
    config, plugins, data = configure(options, config)
    dump_config(config)
    user, group = validate_accounts(options)
    validate_destination(options, source)
    if plugins.exists():
        if not plugins.is_dir():
            raise ValueError('The plugin directory path is not a directory.')
        reject_links(plugins)
    if data.exists():
        if not data.is_dir():
            raise ValueError('The data directory path is not a directory.')
        reject_links(data)
    cache_root = options.prefix / '.cache'
    if cache_root.exists():
        reject_links(cache_root)
    loaded = service_fragment() if inspect_service else ''
    if loaded and Path(loaded) != UNIT_PATH:
        raise ValueError(f'{SERVICE} already exists at {loaded}; it will not be overwritten.')
    previous = read_manifest()
    if previous and previous['prefix'] != str(options.prefix):
        if previous['status'] != 'purged' or UNIT_PATH.exists():
            raise ValueError('Another installation is recorded. Uninstall and purge it before choosing a new directory.')
        previous = None
    return config, plugins, data, user, group, loaded, previous


def install(options, config, source, *, dependencies=None, automatic=False):
    config, plugins, data, user, group, loaded, previous = preflight(options, config, source)
    user_was_missing, group_was_missing = user is None, group is None
    config_text = dump_config(config)
    cache_root = options.prefix / '.cache'
    with tempfile.TemporaryDirectory(prefix='meshbot-install-') as temporary:
        stage = Path(temporary) / 'runtime'
        stage.mkdir()
        stage_runtime(source, stage)  # Supports installing into the checkout itself.
        progress(1, 7, 'Refreshing package metadata')
        run('apt-get', 'update')
        refreshed = dependency_plan()
        if not refreshed.available:
            raise ValueError('APT could not resolve dependencies after refreshing its cache: ' + refreshed.details)
        if dependencies is not None and refreshed.signature() != dependencies.signature():
            print('\nAPT changed the approved package transaction after refreshing metadata:')
            show_dependencies(refreshed, source)
            if ACTIVE_LOG:
                ACTIVE_LOG.write('Revised APT transaction: ' + refreshed.details)
            if automatic or not confirm('Approve this revised package transaction?'):
                raise ValueError('Package transaction changed. No packages installed; rerun to review the refreshed plan.')
        progress(2, 7, 'Installing system dependencies')
        run('apt-get', '-o', 'Dpkg::Use-Pty=0', '--no-remove', 'install', '-y', *PACKAGES,
            env=os.environ | {'DEBIAN_FRONTEND': 'noninteractive', 'LC_ALL': 'C'})
        progress(3, 7, 'Preparing service accounts and directories')
        if group is None:
            run('groupadd', '--system', options.group)
        if user is None:
            run('useradd', '--system', '--gid', options.group, '--home-dir', options.prefix,
                '--no-create-home', '--shell', '/usr/sbin/nologin', options.user)
        user, group = pwd.getpwnam(options.user), grp.getgrnam(options.group)
        if options.radio['type'] == 'serial' and lookup(grp.getgrnam, 'dialout') is None:
            run('groupadd', '--system', 'dialout')
        prefix = options.prefix
        prefix.mkdir(parents=True, exist_ok=True)
        if loaded:
            run('systemctl', 'stop', SERVICE)
        os.chown(prefix, user.pw_uid, group.gr_gid)
        prefix.chmod(0o750)
        # Record the destination before copying/building so failed first installs
        # can be resumed without treating their partial files as unrelated data.
        write_owned(prefix / MARKER, json.dumps({'version': 1, 'user': options.user, 'group': options.group}, indent=2) + '\n', user.pw_uid, group.gr_gid)
        record = inventory(stage, options, plugins, data, previous)
        # Use the preflight account snapshot rather than the accounts just created.
        record['created_user'] = previous.get('created_user', False) if previous and previous['user'] == options.user else user_was_missing
        record['created_group'] = previous.get('created_group', False) if previous and previous['group'] == options.group else group_was_missing
        initial_packages = previous.get('packages', {}).get('initially_present') if previous else None
        record['packages'] = {'requirements': list(PACKAGES), 'transaction': list(refreshed.changes),
                              'initially_present': initial_packages if initial_packages is not None else list((dependencies or refreshed).present)}
        save_manifest(record)
        # An installation below a private home needs traversal, not directory listing.
        for parent in reversed(prefix.parents):
            accessible = subprocess.run(['runuser', '-u', options.user, '--', 'test', '-x', str(parent)]).returncode == 0
            if not accessible:
                grant_traversal(parent, options.user)
        progress(4, 7, 'Installing runtime and preserving operator configuration')
        deploy_runtime(stage, prefix, plugins, user.pw_uid, group.gr_gid)
        data.mkdir(parents=True, exist_ok=True)
        own_tree(data, user.pw_uid, group.gr_gid)
        config_path = prefix / 'bot.toml'
        if not config_path.exists() or read_config(config_path) != config:
            if config_path.exists():
                backup = prefix / ('bot.toml.before-install-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                shutil.copy2(config_path, backup)
                os.chown(backup, user.pw_uid, group.gr_gid)
                backup.chmod(0o640)
            write_owned(config_path, config_text, user.pw_uid, group.gr_gid)
        os.chown(config_path, user.pw_uid, group.gr_gid)
        config_path.chmod(0o640)
        venv = prefix / 'venv'
        venv.mkdir(exist_ok=True)
        cache = cache_root / 'pip'
        cache.mkdir(parents=True, exist_ok=True)
        own_installation(prefix, user.pw_uid, group.gr_gid)
        try:
            progress(5, 7, 'Creating venv and installing Python dependencies')
            run('runuser', '-u', options.user, '--', sys.executable, '-m', 'venv', venv, cwd=prefix)
            python = venv / 'bin/python'
            # An explicit cache also works when reusing an account whose home
            # directory differs from this installation's location.
            pip = ('runuser', '-u', options.user, '--', python, '-m', 'pip',
                   '--cache-dir', cache, '--disable-pip-version-check')
            run(*pip, 'install', '-r', prefix / 'requirements.txt', cwd=prefix)
            run(*pip, 'check', cwd=prefix)
            run(*pip, 'list', '--format=freeze', cwd=prefix)
            progress(6, 7, 'Validating the bot as its service account')
            run('runuser', '-u', options.user, '--', venv / 'bin/python', prefix / 'mesh_bot.py',
                '--config', config_path, '--check', cwd=prefix)
        finally:
            own_installation(prefix, user.pw_uid, group.gr_gid)
        progress(7, 7, 'Enabling and starting the service')
        write_owned(UNIT_PATH, render_unit(options, data), 0, 0, 0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', SERVICE)
        run('systemctl', 'reset-failed', SERVICE)
        run('systemctl', 'restart', SERVICE)
        run('systemctl', 'is-active', '--quiet', SERVICE)
        record['status'] = 'installed'
        save_manifest(record)


def validate_host(dry_run=False):
    if not Path('/etc/debian_version').is_file() or shutil.which('apt-get') is None:
        raise ValueError('Supported hosts: Debian 12+, Ubuntu 24.04+, Raspberry Pi OS Bookworm+ with Python 3.11+.')
    if not dry_run and not Path('/run/systemd/system').is_dir():
        raise ValueError('A running systemd host is required. Use --dry-run to preview without changes.')


def show_plan(plan, verbose=False):
    if plan['action'] == 'uninstall':
        record = plan['record']
        print(f"\nUninstall: {plan['prefix']}\nService: {UNIT_PATH}")
        if record:
            print(f"Remove: {len(record['files'])} recorded runtime files if unchanged, venv, and pip cache.")
            print('Keep: modified/unknown files, system accounts, traversal ACLs, OS packages, and installation logs.')
            print(('PURGE: remove bot.toml/backups, plugin directory (including custom plugins), and data directory.'
                   if plan['purge'] else 'Keep: bot.toml/backups, all plugins, and persistent data. Use --purge to remove these.'))
        else:
            print('Legacy installation: remove service only; retain ALL files and accounts (no trusted inventory).')
            print('Rerun the new installer first if you want inventoried file removal or --purge.')
    else:
        options = plan['options']
        print(f"\nInstallation summary\nInstall/update: {options['prefix']}\nRun as: {options['user']}:{options['group']}")
        print('Radio: ' + ', '.join(f'{key}={value}' for key, value in options['radio'].items()))
        print(f"Plugin directory: {plan['plugins']}\nWritable data: {plan['data']}\nService: {UNIT_PATH}")
        print('Replace: runtime and bundled plugin Python code. Preserve: plugin settings, custom plugins, state,')
        print('and other bot settings. Changed radio configuration is backed up before rewriting.')
        print('Create/reuse the validated non-root system account; enable and start the service.')
        show_dependencies(Dependencies(**plan['dependencies']), verbose=verbose)
    print(f'Root-owned inventory: {STATE / MANIFEST}\nOperation logs: {LOGS}/<action>-<timestamp>.log')


def options_from_plan(plan):
    values = plan['options']
    return Options(install_path(values['prefix']), account_name(values['user']), account_name(values['group']), values['radio'])


def execute_plan(plan, automatic=False):
    if os.geteuid() != 0:
        raise ValueError('The approved execution phase requires root privileges.')
    validate_host()
    if plan['action'] == 'install':
        options = options_from_plan(plan)
        config = tomllib.loads(plan['config'])
    elif plan['action'] != 'uninstall':
        raise ValueError('Unknown installer action.')

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'interrupted by signal {signum}')

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with OperationLog(plan['action']) as journal:
            description = io.StringIO()
            with contextlib.redirect_stdout(description):
                show_plan(plan, verbose=True)
            journal.write(description.getvalue())
            if plan['action'] == 'uninstall':
                uninstall(plan)
            else:
                preflight(options, config, SOURCE)
                values = plan['dependencies']
                dependencies = Dependencies(tuple(tuple(item) for item in values['present']),
                                            tuple(tuple(item) for item in values['changes']),
                                            values['details'], values['available'])
                install(options, config, SOURCE, dependencies=dependencies, automatic=automatic)
                print(f'\nInstalled, enabled, and started {SERVICE}.')
                print(f'Logs: sudo journalctl -u {SERVICE} -f')
                print(f'Configuration: {shlex.quote(str(options.prefix / "bot.toml"))}')
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    global CHILD_EXECUTION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-u', '--uninstall', action='store_true')
    parser.add_argument('--purge', action='store_true', help='with --uninstall, also remove configuration, plugins, and data')
    parser.add_argument('--dry-run', action='store_true', help='preview only; no writes or privilege escalation')
    parser.add_argument('-y', '--yes', action='store_true', help='approve the displayed plan; changed APT transactions still stop automation')
    parser.add_argument('--non-interactive', action='store_true', help='use supplied values/defaults; require --yes unless --dry-run')
    parser.add_argument('--verbose', action='store_true', help='include the full dependency simulation in the summary')
    parser.add_argument('--install-dir')
    parser.add_argument('--service-user')
    parser.add_argument('--service-group')
    parser.add_argument('--radio-type', choices=('tcp', 'serial'))
    parser.add_argument('--radio-host')
    parser.add_argument('--radio-port')
    parser.add_argument('--serial-device')
    parser.add_argument('--execute-plan', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.purge and not args.uninstall:
        parser.error('--purge requires --uninstall.')
    if args.non_interactive and not args.yes and not args.dry_run:
        parser.error('--non-interactive requires --yes unless using --dry-run.')
    if args.execute_plan and (args.uninstall or args.purge or args.dry_run or args.non_interactive
                              or args.install_dir or args.service_user or args.service_group or args.radio_type
                              or args.radio_host or args.radio_port or args.serial_device):
        parser.error('--execute-plan cannot be combined with planning options.')
    try:
        if args.execute_plan:
            CHILD_EXECUTION = True
            execute_plan(json.load(sys.stdin), automatic=args.yes)
            return
        validate_host(args.dry_run)
        if args.uninstall:
            if any((args.service_user, args.service_group, args.radio_type, args.radio_host, args.radio_port, args.serial_device)):
                parser.error('Radio/account settings do not apply to --uninstall.')
            plan = uninstall_plan(install_path(args.install_dir) if args.install_dir else None, args.purge)
        else:
            options, config = collect_options(SOURCE, args)
            updated, plugins, data, *_ = preflight(options, config, SOURCE,
                                                  inspect_service=Path('/run/systemd/system').is_dir())
            plan = {'action': 'install', 'options': asdict(options) | {'prefix': str(options.prefix)},
                    'config': dump_config(config), 'plugins': str(plugins), 'data': str(data),
                    'dependencies': asdict(dependency_plan())}
        show_plan(plan, args.verbose)
        if args.dry_run:
            print('Dry run: no changes made.')
            return
        if not args.yes and not confirm('Proceed with ' + plan['action'] + '?'):
            print('Cancelled: no changes made.')
            return
        if os.geteuid() == 0:
            execute_plan(plan, automatic=args.yes or args.non_interactive)
        else:
            # Authenticate only after approval. Plan/configuration travel through
            # a pipe, never process arguments, environment dumps, or a temporary file.
            subprocess.run(['sudo', '-v'], check=True)
            command = ['sudo', '-n', '--', '/usr/bin/python3', str(SOURCE / 'scripts/install.py'), '--execute-plan']
            if args.yes:
                command.append('--yes')
            subprocess.run(command, input=json.dumps(plan), text=True, check=True)
    except (OSError, ValueError, TypeError, KeyError, EOFError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(sanitize(f'Operation did not complete: {error}'), file=sys.stderr)
        print('If execution began, inspect the operation log printed above. No automatic rollback is attempted.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
