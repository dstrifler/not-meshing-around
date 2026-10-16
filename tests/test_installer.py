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
                patch.object(installer, 'validate_accounts'), patch.object(installer.os, 'geteuid', return_value=0), \
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


if __name__ == '__main__':
    unittest.main()
