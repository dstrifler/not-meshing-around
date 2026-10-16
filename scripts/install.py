"""Interactive Debian/systemd deployment. Invoked by the repository's install.sh."""

import argparse
import datetime
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
UNIT_PATH = Path('/etc/systemd/system/meshbot.service')
SERVICE = 'meshbot.service'
MARKER = '.meshbot-install.json'
MANAGED = '# Managed by not-meshing-around installer: '
PACKAGES = ('python3', 'python3-venv', 'ca-certificates', 'acl', 'passwd', 'util-linux')


@dataclass(frozen=True)
class Options:
    prefix: Path
    user: str
    group: str
    radio: dict


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, text=True, **kwargs)


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
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or '://' in value:
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


def collect_options(source):
    prefix = prompt('Installation directory', '/opt/meshbot', install_path)
    marker = json.loads((prefix / MARKER).read_text()) if (prefix / MARKER).is_file() else {}
    if not isinstance(marker, dict):
        raise ValueError('Invalid installer metadata in the selected destination.')
    group = prompt('Service system group', marker.get('group', 'meshbot'), account_name)
    user = prompt('Service system user', marker.get('user', 'meshbot'), account_name)
    config_path = prefix / 'bot.toml' if (prefix / 'bot.toml').is_file() else source / 'bot.toml'
    config = read_config(config_path)
    old = config.get('radio', {})

    def radio_type(value):
        if value.lower() not in ('tcp', 'serial'):
            raise ValueError('Choose tcp or serial. Console input is not suitable for a service.')
        return value.lower()

    kind = prompt('Radio connection (tcp/serial)', old.get('type') if old.get('type') in ('tcp', 'serial') else 'tcp', radio_type)
    if kind == 'tcp':
        radio = {'type': kind, 'hostname': prompt('Radio hostname or IP', old.get('hostname', 'localhost'), hostname),
                 'port': prompt('Radio TCP port', old.get('port', 4403), port_number)}
    else:
        radio = {'type': kind, 'device': str(prompt('Serial device (prefer /dev/serial/by-id/...)',
                 old.get('device') or '/dev/ttyACM0', absolute_path))}
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
    protected = (plugins, options.prefix / 'botcore', options.prefix / 'venv', options.prefix / 'scripts')
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


def install(options, config, source):
    config, plugins, data = configure(options, config)
    config_text = dump_config(config)  # Validate before packages, accounts, or service changes.
    user, group = validate_accounts(options)
    validate_destination(options, source)
    if plugins.exists():
        reject_links(plugins)
    if data.exists():
        reject_links(data)
    cache_root = options.prefix / '.cache'
    if cache_root.exists():
        reject_links(cache_root)
    loaded = service_fragment()
    if loaded and Path(loaded) != UNIT_PATH:
        raise ValueError(f'{SERVICE} already exists at {loaded}; it will not be overwritten.')
    with tempfile.TemporaryDirectory(prefix='meshbot-install-') as temporary:
        stage = Path(temporary) / 'runtime'
        stage.mkdir()
        stage_runtime(source, stage)  # Supports installing into the checkout itself.
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', *PACKAGES, env=os.environ | {'DEBIAN_FRONTEND': 'noninteractive'})
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
        # An installation below a private home needs traversal, not directory listing.
        for parent in reversed(prefix.parents):
            accessible = subprocess.run(['runuser', '-u', options.user, '--', 'test', '-x', str(parent)]).returncode == 0
            if not accessible:
                grant_traversal(parent, options.user)
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
            run('runuser', '-u', options.user, '--', sys.executable, '-m', 'venv', venv, cwd=prefix)
            python = venv / 'bin/python'
            # An explicit cache also works when reusing an account whose home
            # directory differs from this installation's location.
            pip = ('runuser', '-u', options.user, '--', python, '-m', 'pip',
                   '--cache-dir', cache, '--disable-pip-version-check')
            run(*pip, 'install', '-r', prefix / 'requirements.txt', cwd=prefix)
            run(*pip, 'check', cwd=prefix)
            run('runuser', '-u', options.user, '--', venv / 'bin/python', prefix / 'mesh_bot.py',
                '--config', config_path, '--check', cwd=prefix)
        finally:
            own_installation(prefix, user.pw_uid, group.gr_gid)
        write_owned(UNIT_PATH, render_unit(options, data), 0, 0, 0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', SERVICE)
        run('systemctl', 'reset-failed', SERVICE)
        run('systemctl', 'restart', SERVICE)
        run('systemctl', 'is-active', '--quiet', SERVICE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo or as root.')
    try:
        options, config = collect_options(SOURCE)
        updated, plugins, data = configure(options, config)
        validate_accounts(options)
        validate_destination(options, SOURCE)
        print(f'\nInstall: {options.prefix}\nRun as: {options.user}:{options.group}\nRadio: {options.radio["type"]}')
        if args.dry_run:
            print('Packages: ' + ' '.join(PACKAGES))
            print(f'Plugin directory: {plugins}\nWritable data: {data}\nService: {UNIT_PATH}')
            print(render_unit(options, data))
            print('Dry run: no changes made.')
            return
        install(options, config, SOURCE)
        print(f'\nInstalled, enabled, and started {SERVICE}.')
        print('Check radio connection readiness and logs with:')
        print(f'  sudo systemctl status {SERVICE}\n  sudo journalctl -u {SERVICE} -f')
        print(f'Configuration: {shlex.quote(str(options.prefix / "bot.toml"))}')
    except (OSError, ValueError, TypeError, KeyError, EOFError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f'Installation did not complete: {error}', file=sys.stderr)
        print(f'No data is deleted. Fix the error and rerun; inspect sudo journalctl -u {SERVICE} if startup failed.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
