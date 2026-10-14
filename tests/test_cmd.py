import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from botcore.api import Command, Message, Registration, Response
from botcore.loader import load_plugins
from botcore.mentions import NodeIdentity
from botcore.runtime import PluginServices, Policy, Runtime, packet_payload_bytes, packet_prefix


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cmd_tests_plugin', ROOT / 'plugins.d/cmd.py')
cmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmd)


async def noop(context):
    return None


class CommandPageTests(unittest.TestCase):
    def pages(self, names, limit):
        services = SimpleNamespace(packet_payload_bytes=lambda total: packet_payload_bytes(limit, total))
        return cmd.command_pages(tuple(names), services)

    def assert_pages(self, names, limit):
        pages = self.pages(names, limit)
        recovered = []
        for index, body in enumerate(pages, 1):
            self.assertTrue(body.startswith('CMD | Help\n'))
            self.assertLessEqual(len((packet_prefix(index, len(pages)) + body).encode()), limit)
            recovered.extend(body.split('\n')[1:])
        self.assertEqual(recovered, names)
        return pages

    def test_single_page_matches_requested_format_and_exact_byte_limit(self):
        self.assertEqual(self.assert_pages(['ping'], 15), ('CMD | Help\nping',))
        self.assertEqual(self.pages([], 10), ('CMD | Help',))

    def test_numbering_growth_at_ten_and_one_hundred_packets(self):
        for count in (9, 10, 99, 100, 101):
            names = [f'command{index:04}' for index in range(count)]
            with self.subTest(count=count):
                pages = self.assert_pages(names, 32)
                self.assertEqual(len(pages), count)

    def test_unicode_and_multiword_names_are_whole_and_accounted_in_bytes(self):
        names = ['漢字 route', '🌍 status', 'bbs read', 'longcommand' * 3]
        self.assert_pages(names, 64)

    def test_small_limits_and_overlong_commands_fail_without_truncation(self):
        for names, limit in (([], 9), (['ping', 'pong'], 15), (['x' * 40], 40)):
            with self.subTest(names=names, limit=limit), self.assertRaisesRegex(ValueError, 'too small'):
                self.pages(names, limit)

    def test_packet_budget_rejects_invalid_counts(self):
        for count in (0, -1, True, 1.5, '2'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                packet_payload_bytes(160, count)


class CmdRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []

        async def send(*args):
            self.sent.append(args)

        self.runtime = Runtime(SimpleNamespace(send=send), ':memory:', send_delay=0)
        await self.runtime.activate('cmd', cmd.register({}))
        self.services = PluginServices(self.runtime, 'cmd')

    async def asyncTearDown(self):
        await self.runtime.close()

    def names(self, **kwargs):
        return self.services.command_names(Message('cmd', 123, **kwargs))

    async def test_query_lists_primary_names_without_aliases_and_updates_live(self):
        await self.runtime.activate('ping', Registration(commands=(Command('ping', noop, aliases=('pinging',)),)))
        await self.runtime.activate('background', Registration())
        snapshot = self.names()
        self.assertEqual(snapshot, ('cmd', 'ping'))
        await self.runtime.activate('bbs', Registration(commands=(Command('bbs read', noop),)))
        self.assertEqual(self.names(), ('bbs read', 'cmd', 'ping'))
        self.assertEqual(snapshot, ('cmd', 'ping'))
        self.runtime.plugins['ping'].status = 'failed'
        self.assertEqual(self.names(), ('bbs read', 'cmd'))
        self.runtime.quiesce()
        self.assertEqual(self.names(), ())

    async def test_query_applies_admin_direct_and_blocked_sender_policy(self):
        await self.runtime.activate('restricted', Registration(commands=(
            Command('admin', noop, admin_only=True), Command('private', noop, direct_only=True))))
        self.runtime.policy = Policy(admins=frozenset({123}))
        self.assertEqual(self.names(), ('admin', 'cmd'))
        self.assertEqual(self.names(direct=True), ('admin', 'cmd', 'private'))
        self.runtime.policy = Policy()
        self.assertEqual(self.names(), ('cmd',))
        self.assertEqual(self.names(direct=True), ('cmd', 'private'))
        self.runtime.policy = Policy(blocked=frozenset({123}))
        self.assertEqual(self.names(direct=True), ())
        self.assertFalse(await self.runtime.dispatch(Message('cmd', 123, direct=True)))
        self.assertEqual(self.sent, [])

    async def test_failed_initialization_and_rejected_alias_conflicts_are_absent(self):
        async def fail(services):
            raise RuntimeError('startup failed')

        with self.assertRaises(RuntimeError):
            await self.runtime.activate('failed', Registration(commands=(Command('missing', noop),), start=fail))
        with self.assertRaises(ValueError):
            await self.runtime.activate('conflict', Registration(commands=(Command('other', noop, aliases=('cmd',)),)))
        self.assertEqual(self.names(), ('cmd',))

    async def test_loader_only_lists_enabled_successfully_loaded_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, enabled, body in (('on', True, "return Registration(commands=(Command('available', handler),))"),
                                        ('off', False, "raise AssertionError('disabled import')"),
                                        ('bad', True, "raise ValueError('invalid settings')")):
                (root / f'{name}.toml').write_text(
                    f'[plugin]\nid="{name}"\nversion="1"\napi_version=1\nenabled={str(enabled).lower()}\n')
                (root / f'{name}.py').write_text('from botcore.api import Command, Registration\n'
                    'async def handler(context): pass\ndef register(settings):\n    ' + body + '\n')
            report = await load_plugins(self.runtime, root)
        self.assertEqual([report[name]['status'] for name in ('on', 'off', 'bad')],
                         ['active', 'disabled', 'rejected'])
        self.assertEqual(self.names(), ('available', 'cmd'))

    async def test_real_default_plugins_are_discovered_at_request_time(self):
        # Rebuild without the test's manually activated cmd registration.
        await self.runtime.close()
        self.runtime = Runtime(self.runtime.transport, ':memory:', send_delay=0)
        report = await load_plugins(self.runtime, ROOT / 'plugins.d')
        self.assertEqual(report['cmd']['status'], 'active')
        self.assertEqual(report['ping']['status'], 'active')
        await self.runtime.dispatch(Message('cmd', 123, direct=True))
        body = self.sent[0][0]
        self.assertTrue(body.startswith('CMD | Help\n'))
        names = body.split('\n')[1:]
        self.assertIn('ping', names)
        self.assertIn('cmd', names)
        self.assertNotIn('pinging', names)
        self.assertEqual(names, sorted(names))

    async def test_every_packet_has_a_header_numbering_whole_names_and_original_audience(self):
        await self.runtime.activate('many', Registration(commands=tuple(
            Command(f'command{index:04}', noop) for index in range(101))))
        self.runtime.byte_limit = 32
        await self.runtime.dispatch(Message('CMD', 123, 2, direct=True))
        total = len(self.sent)
        self.assertGreaterEqual(total, 100)
        names = []
        for index, (text, destination, channel, interface) in enumerate(self.sent, 1):
            prefix = f'[{index}/{total}] '
            self.assertTrue(text.startswith(prefix + 'CMD | Help\n'))
            self.assertLessEqual(len(text.encode()), 32)
            self.assertEqual((destination, channel, interface), (123, 2, 'primary'))
            names.extend(text[len(prefix):].split('\n')[1:])
        self.assertEqual(names, list(self.names(direct=True)))

    async def test_public_reply_keeps_channel_and_literal_command_spellings(self):
        await self.runtime.activate('status', Registration(commands=(Command('@status', noop),)))
        self.runtime.transport.node_snapshot = lambda interface: (NodeIdentity(42, 'status'),)
        await self.runtime.dispatch(Message('cmd', 123, 3))
        self.assertEqual(self.sent, [('CMD | Help\n@status\ncmd', None, 3, 'primary')])

    async def test_small_packet_budget_records_error_and_sends_no_partial_reply(self):
        self.runtime.byte_limit = 14
        await self.runtime.activate('ping', Registration(commands=(Command('ping', noop),)))
        with self.assertLogs('botcore.runtime', level='ERROR'):
            await self.runtime.dispatch(Message('cmd', 123, direct=True))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.runtime.health()['cmd']['errors'], 1)

    async def test_preplanned_packets_are_numbered_once_and_mention_normalized(self):
        self.runtime.transport.node_snapshot = lambda interface: (NodeIdentity(42, 'Alice', 'ALIC'),)
        await self.runtime.deliver('cmd', Response(('@ALIC', 'next')), Message('cmd', 123, direct=True))
        self.assertEqual([packet[0] for packet in self.sent], ['[1/2] @!0000002a', '[2/2] next'])
        self.sent.clear()
        await self.runtime.deliver('cmd', Response(('one',)), Message('cmd', 123, direct=True))
        self.assertEqual(self.sent[0][0], 'one')

    async def test_preplanned_packets_are_all_validated_before_any_transmission(self):
        self.runtime.byte_limit = 14
        self.runtime.transport.node_snapshot = lambda interface: (NodeIdentity(42, 'Alice', 'ALIC'),)
        for response in (Response(('short', 'x' * 20)), Response(('short', '@ALIC')),
                         Response(()), Response(('short', '')), Response(('short', 42)),
                         Response(('short',), normalize_mentions='false')):
            with self.subTest(response=response), self.assertRaises((ValueError, TypeError)):
                await self.runtime.deliver('cmd', response, Message('cmd', 123, direct=True))
            self.assertEqual(self.sent, [])

    async def test_preplanned_packets_cannot_bypass_broadcast_policy(self):
        with self.assertRaises(PermissionError):
            await self.runtime.deliver('cmd', Response(('one', 'two'), 'channel', 2),
                                       Message('cmd', 123, direct=True))
        self.assertEqual(self.sent, [])
