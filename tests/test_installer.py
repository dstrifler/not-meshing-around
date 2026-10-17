import contextlib
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REAL_SUBPROCESS_RUN = subprocess.run
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_tests_module', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


class InstallerValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / 'install with spaces'
        self.options = installer.Options(self.prefix, 'meshbot', 'meshbot',
                                         {'type': 'tcp', 'hostname': 'localhost', 'port': 4403})
        self.unit = patch.object(installer, 'UNIT_PATH', self.root / 'meshbot.service')
        self.unit.start()
        self.addCleanup(self.unit.stop)

    def test_default_prompts_and_serial_selection(self):
        with patch('builtins.input', side_effect=['', '', '', '', '', '']):
            options, config = installer.collect_options(ROOT)
        self.assertEqual((str(options.prefix), options.user, options.group), ('/opt/meshbot', 'meshbot', 'meshbot'))
        self.assertEqual(options.radio, {'type': 'tcp', 'hostname': 'localhost', 'port': 4403})
        with patch('builtins.input', side_effect=[str(self.prefix), 'botgroup', 'botuser', 'serial', '/dev/serial/by-id/test']):
            options, _ = installer.collect_options(ROOT)
        self.assertEqual(options.radio, {'type': 'serial', 'device': '/dev/serial/by-id/test'})

    def test_existing_radio_and_account_names_supply_defaults(self):
        self.prefix.mkdir()
        (self.prefix / installer.MARKER).write_text(json.dumps({'user': 'oldbot', 'group': 'oldgroup'}))
        (self.prefix / 'bot.toml').write_text('[radio]\ntype="tcp"\nhostname="radio.example"\nport=5555\n')
        with patch('builtins.input', side_effect=[str(self.prefix), '', '', '', '', '']):
            options, _ = installer.collect_options(ROOT)
        self.assertEqual((options.user, options.group), ('oldbot', 'oldgroup'))
        self.assertEqual(options.radio, {'type': 'tcp', 'hostname': 'radio.example', 'port': 5555})

    def test_rejects_root_accounts_invalid_names_and_bad_addresses(self):
        for value in ('root', '-bad', 'bad name', 'x\nGroup=root', 'a' * 32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                installer.account_name(value)
        for value in ('/', 'relative/path', '/opt/x\nExecStart=/bad'):
            with self.assertRaises(ValueError):
                installer.install_path(value)
        for value in ('https://node', '', 'my node', 'node\x7f'):
            with self.assertRaises(ValueError):
                installer.hostname(value)
        for value in ('0', '65536', '-1', 'abc', '４４０３'):
            with self.assertRaises(ValueError):
                installer.port_number(value)
        self.assertEqual(installer.hostname('2001:db8::1'), '2001:db8::1')

    def test_radio_update_preserves_other_config_values_and_removes_stale_connection_keys(self):
        config = {'core': {'message_bytes': 40}, 'policy': {'admins': [123]},
                  'radio': {'type': 'serial', 'device': '/dev/old', 'connect_timeout': 50}}
        result, plugins, data = installer.configure(self.options, config)
        self.assertEqual(result['radio'], self.options.radio | {'connect_timeout': 50})
        self.assertEqual(result['policy'], config['policy'])
        self.assertEqual(result['core'], config['core'])
        self.assertEqual((plugins, data), (self.prefix / 'plugins.d', self.prefix / 'data'))
        self.assertEqual(config['radio']['device'], '/dev/old')

    def test_config_serialization_roundtrips_nested_tables_arrays_dates_and_quoted_values(self):
        config = {'core': {'plugins': 'my plugins', 'database': 'data/state.db'},
                  'settings': {'list': [{'a': 1}, {'a': 2}], 'when': datetime.date(2026, 10, 14),
                               'text': 'quotes " slash \\ unicode 🌍\n', 'on': False, 'n': 1.25},
                  'odd.key': {'inner.key': {'value': 'literal'}}}
        self.assertEqual(tomllib.loads(installer.dump_config(config)), config)

    def test_rejects_external_or_overlapping_state_and_code_directories(self):
        for core in ({'database': '/outside/state.db'}, {'database': 'core.db'},
                     {'database': 'venv/state.db'}, {'database': 'plugins.d/state.db'},
                     {'plugins': 'botcore/nested'}, {'plugins': '/outside/plugins'}):
            with self.subTest(core=core), self.assertRaises(ValueError):
                installer.configure(self.options, {'core': core})

    def test_private_parent_traversal_does_not_unmask_read_or_write_permissions(self):
        acl = 'user::rwx\nuser:123:rw-\ngroup::rwx\nmask::r--\nother::---\n'
        with patch.object(installer, 'capture', return_value=acl), patch.object(installer, 'run') as run:
            installer.grant_traversal(self.root, 'meshbot')
        run.assert_called_once_with('setfacl', '-n', '-m', 'u:meshbot:--x,m::r-x', '--', self.root)

    def test_existing_unmanaged_directory_service_and_symlinks_are_not_overwritten(self):
        self.prefix.mkdir()
        (self.prefix / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'not empty'):
            installer.validate_destination(self.options, ROOT)
        (self.prefix / installer.MARKER).write_text('{}')
        installer.validate_destination(self.options, ROOT)
        (self.prefix / 'venv').symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            installer.validate_destination(self.options, ROOT)
        (self.prefix / 'venv').unlink()
        installer.UNIT_PATH.write_text('[Service]\nUser=unrelated\n')
        with self.assertRaisesRegex(ValueError, 'another'):
            installer.validate_destination(self.options, ROOT)

    def test_existing_login_or_privileged_accounts_are_rejected(self):
        user = SimpleNamespace(pw_uid=1000, pw_gid=900, pw_shell='/bin/bash')
        group = SimpleNamespace(gr_gid=900)
        with patch.object(installer.pwd, 'getpwnam', return_value=user), \
                patch.object(installer.grp, 'getgrnam', return_value=group), \
                patch.object(installer, 'system_limit', return_value=999):
            with self.assertRaisesRegex(ValueError, 'disabled login'):
                installer.validate_accounts(self.options)
            user.pw_uid, user.pw_shell = 900, '/usr/sbin/nologin'
            with patch.object(installer.os, 'getgrouplist', return_value=[900, 27]), \
                    patch.object(installer.grp, 'getgrgid', return_value=SimpleNamespace(gr_name='sudo')):
                with self.assertRaisesRegex(ValueError, 'privileged'):
                    installer.validate_accounts(self.options)

    def test_serial_unit_uses_dialout_without_running_as_root_or_requiring_meshtasticd(self):
        options = installer.Options(self.prefix, 'botuser', 'botgroup', {'type': 'serial', 'device': '/dev/ttyACM0'})
        unit = installer.render_unit(options, self.prefix / 'data')
        self.assertIn('User=botuser\nGroup=botgroup\nSupplementaryGroups=dialout', unit)
        self.assertIn('NoNewPrivileges=true', unit)
        self.assertIn('ReadWritePaths=', unit)
        self.assertIn(installer.unit_quote(self.prefix / '.cache'), unit)
        self.assertNotIn('meshtasticd', unit)
        self.assertNotIn('ProtectHome=true', unit)
        self.assertNotIn('PrivateTmp=true', unit)

    def test_unit_escapes_spaces_quotes_percent_and_literal_dollars(self):
        options = installer.Options(self.root / 'path %n $HOME "quoted" \\ slash', 'meshbot', 'meshbot', self.options.radio)
        unit = installer.render_unit(options, options.prefix / 'data')
        self.assertIn('%%n', unit)
        self.assertIn('\\"quoted\\"', unit)
        self.assertIn('ExecStart=:/usr/bin/env -- ', unit)  # ':' disables environment substitution.
        self.assertIn('$HOME', unit)

    def test_dry_run_performs_no_installation_or_subprocess_actions(self):
        with patch.object(installer, 'collect_options', return_value=(self.options, {})), \
                patch.object(installer, 'validate_accounts', return_value=(None, None)), patch.object(installer.os, 'geteuid', return_value=0), \
                patch.object(sys, 'argv', ['install.py', '--dry-run']), \
                patch.object(installer, 'run') as run, patch.object(installer, 'install') as install, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            installer.main()
        self.assertIn('Dry run: no changes made', output.getvalue())
        run.assert_not_called()
        install.assert_not_called()
        self.assertFalse(self.prefix.exists())

    def test_systemd_lookup_handles_missing_units_without_ignoring_bus_failures(self):
        missing = subprocess.CalledProcessError(1, 'systemctl', output='LoadState=not-found\nFragmentPath=\n')
        with patch.object(installer, 'capture', side_effect=missing):
            self.assertEqual(installer.service_fragment(), '')
        with patch.object(installer, 'capture', return_value='LoadState=loaded\nFragmentPath=/etc/systemd/system/meshbot.service\n'):
            self.assertEqual(installer.service_fragment(), '/etc/systemd/system/meshbot.service')
        with patch.object(installer, 'capture', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.service_fragment()


class InstallerFlowTests(unittest.TestCase):
    """Exercise real copying/configuration with every privileged command mocked."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        redirected = contextlib.redirect_stdout(io.StringIO())
        redirected.__enter__()
        self.addCleanup(redirected.__exit__, None, None, None)
        self.prefix = self.root / 'destination with spaces'
        self.options = installer.Options(self.prefix, 'botuser', 'botgroup',
                                         {'type': 'tcp', 'hostname': 'radio.local', 'port': 4403})
        self.user = SimpleNamespace(pw_uid=900, pw_gid=901, pw_shell='/usr/sbin/nologin')
        self.group = SimpleNamespace(gr_gid=901)
        self.commands = []
        self.ownership = {}
        self.loaded = ''
        self.fail_pip = False
        self.fail_check = False
        patches = [patch.object(installer, 'UNIT_PATH', self.root / 'meshbot.service'),
                   patch.object(installer, 'STATE', self.root / 'root-state'),
                   patch.object(installer, 'ROOT_UID', os.getuid()),
                   patch.object(installer, 'trusted_file'),
                   patch.object(installer, 'secure_directory', side_effect=lambda path, mode, **kwargs: path.mkdir(parents=True, exist_ok=True, mode=mode)),
                   patch.object(installer, 'dependency_plan', return_value=installer.Dependencies()),
                   patch.object(installer, 'validate_accounts', return_value=(None, None)),
                   patch.object(installer.pwd, 'getpwnam', return_value=self.user),
                   patch.object(installer.grp, 'getgrnam', return_value=self.group),
                   patch.object(installer.os, 'chown', side_effect=self.record_ownership),
                   patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)),
                   patch.object(installer, 'service_fragment', side_effect=lambda: self.loaded),
                   patch.object(installer, 'run', side_effect=self.command_stub)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def command_stub(self, *args, **kwargs):
        self.commands.append(tuple(map(str, args)))
        if 'venv' in args:
            binary = self.prefix / 'venv/bin/python'
            binary.parent.mkdir(parents=True, exist_ok=True)
            if not binary.exists():
                binary.write_text('#!/bin/sh\nexit 0\n')
                binary.chmod(0o755)
                # Files produced by the simulated runuser command inherit its identity.
                self.record_ownership(binary.parent, self.user.pw_uid, self.group.gr_gid)
                self.record_ownership(binary, self.user.pw_uid, self.group.gr_gid)
        if self.fail_pip and 'install' in args and 'pip' in args:
            raise subprocess.CalledProcessError(1, args)
        if self.fail_check and '--check' in args:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)

    def record_ownership(self, path, uid, gid, **kwargs):
        self.ownership[Path(path).absolute()] = (uid, gid)

    def assert_installation_owner(self):
        for path in (self.prefix, *self.prefix.rglob('*')):
            with self.subTest(path=path):
                self.assertEqual(self.ownership.get(path.absolute()), (self.user.pw_uid, self.group.gr_gid))

    def install(self):
        config = self.prefix / 'bot.toml' if (self.prefix / 'bot.toml').is_file() else ROOT / 'bot.toml'
        installer.install(self.options, installer.read_config(config), ROOT)

    def test_fresh_install_copies_runtime_creates_venv_checks_config_and_starts_service(self):
        self.install()
        config = installer.read_config(self.prefix / 'bot.toml')
        self.assertEqual(config['radio']['hostname'], 'radio.local')
        self.assertEqual(json.loads((self.prefix / installer.MARKER).read_text())['user'], 'botuser')
        self.assertTrue((self.prefix / 'botcore/runtime.py').is_file())
        self.assertTrue((self.prefix / 'plugins.d/cmd.toml').is_file())
        self.assertFalse((self.prefix / '.git').exists())
        self.assertFalse((self.prefix / 'integration').exists())
        self.assertTrue(any(command[-3:-1] == ('-m', 'venv') for command in self.commands))
        self.assertTrue(any('--check' in command and command[:3] == ('runuser', '-u', 'botuser') for command in self.commands))
        self.assertIn(('systemctl', 'enable', installer.SERVICE), self.commands)
        self.assertIn(('systemctl', 'restart', installer.SERVICE), self.commands)
        self.assertIn('User=botuser', installer.UNIT_PATH.read_text())
        self.assert_installation_owner()
        # Only the unit outside the installation remains owned by root.
        self.assertTrue(any(path.parent == self.root and owner == (0, 0)
                            for path, owner in self.ownership.items()))

    def test_rerun_repairs_ownership_of_existing_nested_files_and_cache(self):
        self.install()
        for path in (self.prefix / '.cache/pip/old-cache', self.prefix / 'custom/nested/file'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('preserve')
        for path in (self.prefix, *self.prefix.rglob('*')):
            self.ownership[path.absolute()] = (0, 0)
        self.install()
        self.assert_installation_owner()
        self.assertEqual((self.prefix / 'custom/nested/file').read_text(), 'preserve')
        self.assertEqual((self.prefix / '.cache/pip/old-cache').read_text(), 'preserve')

    def test_rerun_preserves_database_and_plugin_settings_and_backs_up_radio_changes(self):
        self.install()
        self.loaded = str(installer.UNIT_PATH)
        database = self.prefix / 'data/core.db'
        database.write_bytes(b'existing SQLite data')
        settings = self.prefix / 'plugins.d/ping.toml'
        settings.write_text(settings.read_text().replace('enabled = true', 'enabled = false'))
        config_path = self.prefix / 'bot.toml'
        original = config_path.read_text().replace('radio.local', 'previous.radio')
        config_path.write_text(original)
        self.commands.clear()
        self.install()
        self.assertEqual(database.read_bytes(), b'existing SQLite data')
        self.assertIn('enabled = false', settings.read_text())
        self.assertEqual(next(self.prefix.glob('bot.toml.before-install-*')).read_text(), original)
        self.assertIn(('systemctl', 'stop', installer.SERVICE), self.commands)
        self.assertEqual(installer.read_config(config_path)['radio']['hostname'], 'radio.local')

    @unittest.skipIf(importlib.util.find_spec('pip') is None, 'Requires Python with pip')
    def test_real_pip_uses_owned_cache_instead_of_unwritable_external_default(self):
        self.install()
        command = next(command for command in self.commands if 'pip' in command and command[-1] == 'check')
        arguments = list(command[command.index('--') + 1:])
        arguments[0] = sys.executable  # Use real pip rather than the flow test's fake interpreter.
        cache = self.root / 'unwritable-cache'
        cache.mkdir(mode=0o555)
        environment = os.environ | {'PIP_CACHE_DIR': str(cache), 'PIP_CONFIG_FILE': os.devnull}
        environment.pop('PIP_NO_CACHE_DIR', None)
        result = REAL_SUBPROCESS_RUN(arguments, cwd=self.prefix, capture_output=True, text=True,
                                     env=environment, timeout=30)
        self.assertIn(result.returncode, (0, 1), result.stderr)  # check can report unrelated package conflicts.
        self.assertNotIn('cache has been disabled', result.stderr)
        self.assertEqual(list(cache.iterdir()), [])
        selected_cache = REAL_SUBPROCESS_RUN(arguments[:-1] + ['cache', 'dir'], cwd=self.prefix,
                                            capture_output=True, text=True, env=environment, timeout=30)
        self.assertEqual(selected_cache.returncode, 0, selected_cache.stderr)
        self.assertEqual(Path(selected_cache.stdout.strip()), self.prefix / '.cache/pip')

    def test_service_account_owns_cache_and_tree_before_dependency_operations(self):
        original = self.command_stub
        def check_owner(*args, **kwargs):
            if 'pip' in args:
                self.assert_installation_owner()
                self.assertTrue((self.prefix / '.cache/pip').is_dir())
            return original(*args, **kwargs)
        with patch.object(installer, 'run', side_effect=check_owner):
            self.install()

    def test_failed_venv_install_remains_resumable_and_never_enables_or_starts_service(self):
        self.fail_pip = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertTrue((self.prefix / installer.MARKER).is_file())
        self.assertFalse(installer.UNIT_PATH.exists())
        self.assertNotIn(('systemctl', 'enable', installer.SERVICE), self.commands)
        self.assertNotIn(('systemctl', 'restart', installer.SERVICE), self.commands)
        self.assert_installation_owner()
        self.fail_pip = False
        self.install()
        self.assertTrue(installer.UNIT_PATH.exists())

    def test_unrelated_systemd_service_fails_before_any_install_actions(self):
        self.loaded = '/usr/lib/systemd/system/meshbot.service'
        with self.assertRaisesRegex(ValueError, 'will not be overwritten'):
            self.install()
        self.assertEqual(self.commands, [])
        self.assertFalse(self.prefix.exists())

    def test_failed_bot_configuration_check_prevents_service_activation(self):
        self.fail_check = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertFalse(installer.UNIT_PATH.exists())
        self.assertNotIn(('systemctl', 'enable', installer.SERVICE), self.commands)
        self.assertNotIn(('systemctl', 'restart', installer.SERVICE), self.commands)

    def test_venv_interpreter_symlinks_do_not_change_external_file_permissions(self):
        self.install()
        interpreter = self.prefix / 'venv/bin/python'
        interpreter.unlink()
        external = self.root / 'system-python'
        external.write_text('unchanged')
        external.chmod(0o700)
        interpreter.symlink_to(external)
        self.install()
        self.assertEqual(external.stat().st_mode & 0o777, 0o700)
        self.assertEqual(external.read_text(), 'unchanged')
        self.assertNotIn(external, self.ownership)

    def test_nested_symlinks_in_managed_state_are_not_followed(self):
        self.install()
        victim = self.root / 'outside'
        victim.write_text('keep')
        (self.prefix / 'data/linked').symlink_to(victim)
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.install()
        self.assertEqual(victim.read_text(), 'keep')
        self.assertEqual(self.commands, [])

    def test_installing_from_destination_itself_uses_staging(self):
        self.install()
        self.loaded = str(installer.UNIT_PATH)
        installer.install(self.options, installer.read_config(self.prefix / 'bot.toml'), self.prefix)
        self.assertTrue((self.prefix / 'scripts/install.py').is_file())
        self.assertEqual(installer.read_config(self.prefix / 'bot.toml')['radio']['hostname'], 'radio.local')

    def test_execution_creates_separate_install_and_uninstall_logs(self):
        plan = {'action': 'install', 'options': installer.asdict(self.options) | {'prefix': str(self.prefix)},
                'config': installer.dump_config(installer.read_config(ROOT / 'bot.toml')),
                'plugins': str(self.prefix / 'plugins.d'), 'data': str(self.prefix / 'data'),
                'dependencies': installer.asdict(installer.Dependencies())}
        with patch.object(installer, 'LOGS', self.root / 'logs'), \
                patch.object(installer.os, 'geteuid', return_value=0), patch.object(installer, 'validate_host'):
            installer.execute_plan(plan)
            installer.execute_plan(installer.uninstall_plan())
        records = list((self.root / 'logs').glob('*.log'))
        self.assertEqual(len(records), 2)
        self.assertTrue(any(path.name.startswith('install-') for path in records))
        self.assertTrue(any(path.name.startswith('uninstall-') for path in records))
        self.assertTrue(all('SUCCESS' in path.read_text() for path in records))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in records))
        self.assertIsNone(installer.ACTIVE_LOG)

    def test_manifest_is_external_to_service_owned_tree_and_tracks_partial_installs(self):
        self.fail_pip = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        record = installer.read_manifest()
        self.assertEqual(record['status'], 'installing')
        self.assertEqual(record['prefix'], str(self.prefix))
        self.assertTrue(record['created_user'])
        self.assertTrue(record['created_group'])
        self.assertIn('botcore/runtime.py', record['files'])
        self.assertNotIn('bot.toml', record['files'])
        self.assertNotIn('plugins.d/ping.py', record['files'])
        self.assertFalse(installer.STATE.is_relative_to(self.prefix))
        self.assertTrue(any(path.parent == installer.STATE and owner == (0, 0)
                            for path, owner in self.ownership.items()))
        installer.uninstall(installer.uninstall_plan())
        self.assertTrue((self.prefix / 'bot.toml').exists())
        self.assertFalse((self.prefix / 'venv').exists())

    def test_uninstall_retains_settings_plugins_data_and_unknown_or_modified_files(self):
        self.install()
        data = self.prefix / 'data/core.db'
        data.write_bytes(b'keep database')
        modified = self.prefix / 'botcore/runtime.py'
        modified.write_text('keep local modifications')
        unknown = self.prefix / 'botcore/custom.txt'
        unknown.write_text('keep unknown file')
        victim = self.root / 'outside.txt'
        victim.write_text('outside stays')
        (self.prefix / installer.MARKER).write_text(json.dumps({'prefix': str(victim)}))
        self.commands.clear()
        installer.uninstall(installer.uninstall_plan())
        self.assertFalse(installer.UNIT_PATH.exists())
        self.assertFalse((self.prefix / 'mesh_bot.py').exists())
        self.assertFalse((self.prefix / 'venv').exists())
        self.assertFalse((self.prefix / '.cache').exists())
        self.assertEqual(modified.read_text(), 'keep local modifications')
        self.assertEqual(unknown.read_text(), 'keep unknown file')
        self.assertEqual(data.read_bytes(), b'keep database')
        self.assertTrue((self.prefix / 'bot.toml').exists())
        self.assertTrue((self.prefix / 'plugins.d/ping.py').exists())
        self.assertEqual(victim.read_text(), 'outside stays')
        self.assertEqual(installer.read_manifest()['status'], 'uninstalled')
        self.assertIn(('systemctl', 'stop', installer.SERVICE), self.commands)
        self.assertIn(('systemctl', 'disable', installer.SERVICE), self.commands)
        self.assertFalse(any(command[0] in ('apt-get', 'userdel', 'groupdel') for command in self.commands))

    def test_purge_after_normal_uninstall_removes_only_explicit_config_plugin_and_data_paths(self):
        self.install()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('outside')
        (self.prefix / 'data/link').symlink_to(outside, target_is_directory=True)
        (self.prefix / 'unrelated').write_text('retain unknown root file')
        (self.prefix / 'bot.toml.before-install-old').write_text('old settings')
        installer.uninstall(installer.uninstall_plan())
        installer.uninstall(installer.uninstall_plan(purge=True))
        for name in ('data', 'plugins.d', 'bot.toml', 'bot.toml.before-install-old', installer.MARKER):
            self.assertFalse((self.prefix / name).exists(), name)
        self.assertEqual((outside / 'keep').read_text(), 'outside')
        self.assertTrue((self.prefix / 'unrelated').exists())
        self.assertEqual(installer.read_manifest()['status'], 'purged')

    def test_uninstall_rejects_inventory_escape_and_redirected_prefix_before_service_changes(self):
        self.install()
        manifest = installer.STATE / installer.MANIFEST
        record = json.loads(manifest.read_text())
        record['files']['../outside'] = '0' * 64
        manifest.write_text(json.dumps(record))
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, 'relative path'):
            installer.uninstall_plan()
        self.assertEqual(self.commands, [])
        record['files'].pop('../outside')
        manifest.write_text(json.dumps(record))
        moved = self.root / 'moved'
        self.prefix.rename(moved)
        self.prefix.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            installer.uninstall_plan()
        self.assertEqual(self.commands, [])

    def test_purge_never_follows_a_replaced_data_directory_symlink(self):
        self.install()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('retained')
        (self.prefix / 'data').rmdir()
        (self.prefix / 'data').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'redirected directory'):
            installer.uninstall(installer.uninstall_plan(purge=True))
        self.assertEqual((outside / 'keep').read_text(), 'retained')

    def test_legacy_uninstall_removes_service_only_and_refuses_purge(self):
        self.install()
        (installer.STATE / installer.MANIFEST).unlink()
        self.commands.clear()
        plan = installer.uninstall_plan()
        self.assertIsNone(plan['record'])
        with self.assertRaisesRegex(ValueError, 'Legacy installation'):
            installer.uninstall_plan(purge=True)
        installer.uninstall(plan)
        self.assertFalse(installer.UNIT_PATH.exists())
        self.assertTrue((self.prefix / 'mesh_bot.py').exists())
        self.assertTrue((self.prefix / 'venv').exists())

    def test_changed_dependency_transaction_requires_reapproval_before_package_install(self):
        changed = installer.Dependencies(changes=(('extra-package', '', '1.0'),))
        with patch.object(installer, 'dependency_plan', return_value=changed), \
                patch.object(installer, 'confirm', return_value=False) as approval:
            with self.assertRaisesRegex(ValueError, 'transaction changed'):
                installer.install(self.options, {}, ROOT, dependencies=installer.Dependencies())
        approval.assert_called_once()
        self.assertEqual(self.commands, [('apt-get', 'update')])
        self.assertFalse(self.prefix.exists())
        with patch.object(installer, 'dependency_plan', return_value=changed), \
                patch.object(installer, 'confirm', return_value=True):
            installer.install(self.options, {}, ROOT, dependencies=installer.Dependencies())
        self.assertTrue(installer.UNIT_PATH.exists())

    def test_automation_stops_on_changed_dependencies_and_failed_apt_never_deploys(self):
        changed = installer.Dependencies(changes=(('extra-package', '', '1.0'),))
        with patch.object(installer, 'dependency_plan', return_value=changed), \
                patch.object(installer, 'confirm') as approval:
            with self.assertRaisesRegex(ValueError, 'transaction changed'):
                installer.install(self.options, {}, ROOT, dependencies=installer.Dependencies(), automatic=True)
        approval.assert_not_called()
        self.assertFalse(self.prefix.exists())
        self.commands.clear()
        with patch.object(installer, 'run', side_effect=subprocess.CalledProcessError(100, 'apt-get')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.install()
        self.assertFalse(self.prefix.exists())


class InstallerPlanningAndLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.options = installer.Options(self.root / 'new install', 'meshbot', 'meshbot',
                                         {'type': 'tcp', 'hostname': 'localhost', 'port': 4403})

    @contextlib.contextmanager
    def planning(self, arguments):
        with patch.object(installer, 'collect_options', return_value=(self.options, {})), \
                patch.object(installer, 'preflight', return_value=({}, self.options.prefix / 'plugins.d', self.options.prefix / 'data')), \
                patch.object(installer, 'dependency_plan', return_value=installer.Dependencies()), \
                patch.object(installer, 'validate_host'), \
                patch.object(sys, 'argv', ['install.py', *arguments]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            yield output

    def test_decline_and_dry_run_do_not_log_escalate_or_execute(self):
        for arguments in ([], ['--dry-run']):
            with self.subTest(arguments=arguments), self.planning(arguments) as output, \
                    patch.object(installer, 'confirm', return_value=False), \
                    patch.object(installer, 'execute_plan') as execute, \
                    patch.object(installer, 'OperationLog') as log, \
                    patch.object(installer.subprocess, 'run') as subprocess_run:
                installer.main()
            execute.assert_not_called()
            log.assert_not_called()
            subprocess_run.assert_not_called()
            self.assertIn('no changes made', output.getvalue())
            self.assertIn('OS requirements:', output.getvalue())
            self.assertIn('Meshtastic Python client library', output.getvalue())
            self.assertFalse(self.options.prefix.exists())

    def test_sudo_authentication_occurs_only_after_approval_and_payload_is_not_in_argv(self):
        ordering = []
        def subprocess_run(command, **kwargs):
            ordering.append(command)
            return SimpleNamespace(returncode=0)
        def approve(label):
            ordering.append('approved')
            return True
        with self.planning([]), patch.object(installer.os, 'geteuid', return_value=1000), \
                patch.object(installer, 'confirm', side_effect=approve), \
                patch.object(installer.subprocess, 'run', side_effect=subprocess_run) as child:
            installer.main()
        self.assertEqual(ordering[0], 'approved')
        self.assertEqual(ordering[1], ['sudo', '-v'])
        self.assertIn('--execute-plan', ordering[2])
        self.assertNotIn(str(self.options.prefix), ordering[2])
        payload = json.loads(child.call_args.kwargs['input'])
        self.assertEqual(payload['options']['prefix'], str(self.options.prefix))

    def test_uninstall_decline_and_dry_run_share_no_write_guarantee(self):
        plan = {'action': 'uninstall', 'prefix': str(self.options.prefix), 'purge': False, 'record': None}
        for flags in (['-u'], ['--uninstall', '--dry-run']):
            with self.planning(flags) as output, patch.object(installer, 'uninstall_plan', return_value=plan), \
                    patch.object(installer, 'confirm', return_value=False), \
                    patch.object(installer, 'execute_plan') as execute:
                installer.main()
            execute.assert_not_called()
            self.assertIn('no changes made', output.getvalue())

    def test_dependency_simulation_parses_full_transaction_and_missing_direct_requirements(self):
        results = [SimpleNamespace(returncode=1, stdout='python3\tinstalled\t3.11\n', stderr='missing package'),
                   SimpleNamespace(returncode=0, stdout='Inst python3-venv (3.11 Debian)\nInst extra [1.0] (2.0 Debian)\n', stderr='')]
        with patch.object(installer.subprocess, 'run', side_effect=results) as run:
            plan = installer.dependency_plan()
        self.assertEqual(plan.present, (('python3', '3.11'),))
        self.assertEqual(plan.changes, (('python3-venv', '', '3.11'), ('extra', '1.0', '2.0')))
        self.assertIn('-s', run.call_args.args[0])
        self.assertIn('Debug::NoLocking=1', run.call_args.args[0])
        self.assertIn('--no-remove', run.call_args.args[0])
        self.assertNotIn('DEBIAN_FRONTEND', run.call_args.kwargs['env'] if 'DEBIAN_FRONTEND' not in os.environ else {})

    def test_unavailable_simulation_is_explicit_and_package_removals_are_rejected(self):
        installed = SimpleNamespace(returncode=1, stdout='', stderr='')
        for status, stdout in ((100, ''), (0, 'Remv shared-package [1.0]\n')):
            with patch.object(installer.subprocess, 'run', side_effect=[installed, SimpleNamespace(returncode=status, stdout=stdout, stderr='cache unavailable')]):
                if status:
                    self.assertFalse(installer.dependency_plan().available)
                else:
                    with self.assertRaisesRegex(ValueError, 'removals'):
                        installer.dependency_plan()

    def test_operation_log_is_private_persistent_and_redacts_subprocess_output(self):
        # Only the root-directory ownership check is mocked; file creation,
        # subprocess output, exit status, permissions, and redaction are real.
        def directory(path, mode, **kwargs):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
        with patch.object(installer, 'LOGS', self.root / 'logs'), \
                patch.object(installer, 'ROOT_UID', os.getuid()), \
                patch.object(installer, 'secure_directory', side_effect=directory), \
                contextlib.redirect_stdout(io.StringIO()) as console:
            with installer.OperationLog('install') as journal:
                installer.run(sys.executable, '-c', 'print("password=VERY_PRIVATE https://name:pass@example.com/path Authorization: Bearer abcdef")')
                location = journal.path
            with installer.OperationLog('uninstall') as another:
                another.write('completed cleanup')
        contents = location.read_text()
        for secret in ('VERY_PRIVATE', 'name:pass', 'abcdef'):
            self.assertNotIn(secret, contents)
            self.assertNotIn(secret, console.getvalue())
        self.assertIn('exit_status=0', contents)
        self.assertIn('SUCCESS', contents)
        self.assertEqual(location.stat().st_mode & 0o777, 0o600)
        self.assertTrue(location.exists())
        self.assertNotEqual(location, another.path)
        self.assertIsNone(installer.ACTIVE_LOG)

    def test_command_failure_has_log_excerpt_and_nonzero_status(self):
        def directory(path, mode, **kwargs):
            path.mkdir(mode=mode, parents=True, exist_ok=True)
        with patch.object(installer, 'LOGS', self.root / 'logs'), \
                patch.object(installer, 'ROOT_UID', os.getuid()), \
                patch.object(installer, 'secure_directory', side_effect=directory), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as failure:
            with self.assertRaises(subprocess.CalledProcessError):
                with installer.OperationLog('install') as journal:
                    installer.run(sys.executable, '-c', 'print("step failed"); raise SystemExit(7)')
        self.assertIn('Full log:', failure.getvalue())
        self.assertIn('step failed', failure.getvalue())
        self.assertIn('exit_status=7', journal.path.read_text())
        self.assertIn('FAILED', journal.path.read_text())
        self.assertIsNone(installer.ACTIVE_LOG)

    def test_root_metadata_rejects_service_writable_directory_and_symlink(self):
        target = self.root / 'metadata'
        target.write_text('{}')
        link = self.root / 'redirect'
        link.symlink_to(target)
        for candidate in (target, link):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, 'Untrusted'):
                installer.trusted_file(candidate)
        with self.assertRaisesRegex(ValueError, 'securely owned'):
            installer.secure_directory(self.root, 0o755)

    def test_log_creation_supports_ubuntu_shared_log_parent_without_relaxing_manifest_checks(self):
        def info(path, *args, **kwargs):
            permissions = 0o775 if path == Path('/var/log') else 0o755
            return SimpleNamespace(st_uid=0, st_mode=0o040000 | permissions)
        with patch.object(Path, 'stat', info), patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'chmod') as chmod:
            installer.secure_directory(Path('/var/log/meshbot'), 0o700, shared_log_parent=True)
            chmod.assert_not_called()  # chmod is performed through the validated fd instead.
            with self.assertRaisesRegex(ValueError, 'securely owned'):
                installer.secure_directory(Path('/var/log/meshbot'), 0o700)

    def test_shell_launcher_rejects_sourcing_without_changing_callers_directory(self):
        script = 'before=$PWD; source "$1" --help; result=$?; test "$PWD" = "$before" || exit 9; printf "source_status=%s\n" "$result"'
        result = REAL_SUBPROCESS_RUN(['bash', '-c', script, 'bash', str(ROOT / 'install.sh')],
                                     cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('source_status=1', result.stdout)
        self.assertIn('Do not source', result.stderr)
        result = REAL_SUBPROCESS_RUN(['bash', str(ROOT / 'install.sh'), '--help'],
                                     cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--uninstall', result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sigterm_records_failure_and_terminates_the_active_command(self):
        pidfile = self.root / 'child.pid'
        script = r"""
import os, signal, sys, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install as installer
root = Path(sys.argv[2])
installer.LOGS = root / 'logs'
installer.secure_directory = lambda path, mode, **kwargs: path.mkdir(parents=True, exist_ok=True, mode=mode)
installer.ROOT_UID = os.getuid()
installer.os.geteuid = lambda: 0
installer.validate_host = lambda *args: None
code = 'import os, sys, time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'
installer.uninstall = lambda plan: installer.run(sys.executable, '-c', code, root / 'child.pid')
def interrupt():
    deadline = time.monotonic() + 3
    while not (root / 'child.pid').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    os.kill(os.getpid(), signal.SIGTERM)
timer = threading.Thread(target=interrupt, daemon=True)
timer.start()
try:
    installer.execute_plan({'action': 'uninstall', 'prefix': str(root / 'bot'), 'record': None, 'purge': False})
except KeyboardInterrupt:
    raise SystemExit(2)
"""
        try:
            result = REAL_SUBPROCESS_RUN([sys.executable, '-c', script, str(ROOT / 'scripts'), str(self.root)],
                                         capture_output=True, text=True, timeout=6)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Full log:', result.stderr)
            self.assertIn('FAILED:', next((self.root / 'logs').glob('*.log')).read_text())
            self.assertTrue(pidfile.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            if pidfile.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(pidfile.read_text()), 9)

    def test_noninteractive_dry_run_from_another_directory_performs_no_writes(self):
        result = REAL_SUBPROCESS_RUN(['bash', str(ROOT / 'install.sh'), '--dry-run', '--non-interactive',
                                     '--install-dir', str(self.options.prefix)], cwd=self.root,
                                     capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Dry run: no changes made.', result.stdout)
        self.assertIn('APT', result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
