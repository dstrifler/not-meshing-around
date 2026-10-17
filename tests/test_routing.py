import asyncio
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botcore.api import Command, Event, Message, Registration, Response, Routing, Subscription
from botcore.loader import load_plugins
from botcore.runtime import PluginServices, Policy, Runtime


ROOT = Path(__file__).resolve().parents[1]


class RoutingConfigTests(unittest.TestCase):
    def test_defaults_partial_overrides_and_empty_channels(self):
        self.assertEqual(Routing.from_config({}), Routing())
        default = Routing(reply_mode='dm')
        self.assertEqual(Routing.from_config({'channels': [2]}, default),
                         Routing(channels=(2,), reply_mode='dm'))
        routing = Routing.from_config({'channels': []})
        self.assertFalse(routing.accepts(Message('ping', 1)))
        self.assertTrue(routing.accepts(Message('ping', 1, 7, direct=True)))

    def test_invalid_toml_routing_is_rejected(self):
        cases = [False, [], 'both', {'listen_mode': 'channel'}, {'listen_mode': ['dm']},
                 {'listen_mode': True}, {'reply_mode': 'channel'}, {'reply_mode': False},
                 {'channels': 2}, {'channels': 'all'}, {'channels': [True]},
                 {'channels': ['2']}, {'channels': [2.0]}, {'channels': [-1]},
                 {'channels': [8]}, {'channels': [2, 2]}, {'listen_dms': True}]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ValueError):
                Routing.from_config(config)

    def test_registration_routing_is_immutable(self):
        routing = Routing(channels=(2,))
        with self.assertRaises(AttributeError):
            routing.listen_mode = 'dm'
        with self.assertRaises(ValueError):
            Routing(channels=[2])


class RoutingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent, self.seen = [], []

        async def send(*args):
            self.sent.append(args)

        self.runtime = Runtime(SimpleNamespace(send=send), ':memory:', send_delay=0)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def activate(self, routing=Routing(), command=None):
        async def handler(context):
            self.seen.append(context.message)
            return Response('ok')

        await self.runtime.activate('test', Registration(
            commands=(command or Command('test', handler, aliases=('alias',)),), routing=routing))

    async def test_listen_modes_filter_channels_and_dms_before_handler_and_discovery(self):
        await self.activate()
        for mode in ('channels', 'dm', 'both'):
            self.runtime.plugins['test'].registration = replace(
                self.runtime.plugins['test'].registration, routing=Routing(mode, (2,)))
            for channel in (0, 2, 7):
                for direct in (False, True):
                    expected = mode != 'channels' if direct else mode != 'dm' and channel == 2
                    with self.subTest(mode=mode, channel=channel, direct=direct):
                        self.seen.clear()
                        message = Message('ALIAS', 123, channel, direct=direct)
                        self.assertEqual(self.runtime.command_names(message), ('test',) if expected else ())
                        self.assertEqual(await self.runtime.dispatch(message), expected)
                        self.assertEqual(len(self.seen), int(expected))

    async def test_reply_modes_override_plugin_destination_for_every_packet(self):
        await self.activate()
        for mode in ('origin', 'dm'):
            self.runtime.plugins['test'].registration = replace(
                self.runtime.plugins['test'].registration, routing=Routing(reply_mode=mode))
            for direct in (False, True):
                for destination in ('reply', 'private'):
                    with self.subTest(mode=mode, direct=direct, destination=destination):
                        self.sent.clear()
                        await self.runtime.deliver('test', Response(('one', 'two'), destination),
                                                   Message('test', 123, 2, direct=direct))
                        target = 123 if direct or mode == 'dm' else None
                        self.assertEqual(self.sent, [('[1/2] one', target, 2, 'primary'),
                                                     ('[2/2] two', target, 2, 'primary')])

    async def test_dm_never_becomes_broadcast_even_with_broadcast_permission(self):
        await self.activate()
        self.runtime.policy = Policy(broadcast_plugins=frozenset({'test'}))
        for mode in ('origin', 'dm'):
            self.runtime.plugins['test'].registration = replace(
                self.runtime.plugins['test'].registration, routing=Routing(reply_mode=mode))
            with self.assertRaises(PermissionError):
                await self.runtime.deliver('test', Response('secret', 'channel'),
                                           Message('test', 123, 2, direct=True))
        self.assertEqual(self.sent, [])

    async def test_replies_cannot_move_to_another_channel_or_interface(self):
        await self.activate()
        self.runtime.policy = Policy(broadcast_plugins=frozenset({'test'}))
        for response in (Response('secret', channel=3), Response('secret', interface='secondary')):
            with self.subTest(response=response), self.assertRaises(PermissionError):
                await self.runtime.deliver('test', response, Message('test', 123, 2))
        self.assertEqual(self.sent, [])

    async def test_delayed_replies_recheck_routing_and_blocked_senders(self):
        await self.activate(Routing(channels=(2,), reply_mode='dm'))
        services = PluginServices(self.runtime, 'test')
        message = Message('test', 123, 2)
        await services.reply(Response('later'), message)
        self.assertEqual(self.sent, [('later', 123, 2, 'primary')])
        self.runtime.policy = Policy(blocked=frozenset({123}))
        with self.assertRaises(PermissionError):
            await services.reply(Response('later'), message)
        self.runtime.policy = Policy()
        self.runtime.plugins['test'].registration = replace(
            self.runtime.plugins['test'].registration, routing=Routing(channels=(3,)))
        with self.assertRaises(PermissionError):
            await services.reply(Response('later'), message)
        self.assertEqual(len(self.sent), 1)

    async def test_global_policy_and_command_restrictions_remain_in_force(self):
        async def handler(context):
            self.seen.append(context)

        await self.activate(Routing(), Command('test', handler, admin_only=True, direct_only=True))
        self.runtime.policy = Policy(admins=frozenset({1}), blocked=frozenset({3}))
        for message in (Message('test', 1), Message('test', 2, direct=True),
                        Message('test', 3, direct=True)):
            self.assertFalse(await self.runtime.dispatch(message))
            self.assertEqual(self.runtime.command_names(message), ())
        self.assertEqual(self.seen, [])
        self.assertTrue(await self.runtime.dispatch(Message('test', 1, direct=True)))

    async def test_events_and_unsolicited_sends_do_not_use_command_routing(self):
        delivered = asyncio.Event()

        async def event_handler(event, services):
            await services.send(Response('scheduled alert', 'channel', 7))
            delivered.set()

        await self.runtime.activate('test', Registration(
            events=(Subscription('alert', event_handler),), routing=Routing('dm', ())))
        services = PluginServices(self.runtime, 'test')
        with self.assertRaises(PermissionError):
            await services.send(Response('alert', 'channel', 7))
        self.runtime.policy = Policy(broadcast_plugins=frozenset({'test'}))
        task = asyncio.create_task(self.runtime.event_loop())
        try:
            await services.emit(Event('alert'))
            await asyncio.wait_for(delivered.wait(), 1)
            self.assertEqual(self.sent, [('scheduled alert', None, 7, 'primary')])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_api_routing_never_activates(self):
        with self.assertRaisesRegex(TypeError, 'Registration.routing'):
            await self.runtime.activate('bad', Registration(routing={'listen_mode': 'dm'}))
        self.assertNotIn('bad', self.runtime.plugins)


class RoutingLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sent = []

        async def send(*args):
            self.sent.append(args)

        self.runtime = Runtime(SimpleNamespace(send=send), ':memory:', send_delay=0)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.tmp.cleanup()

    def plugin(self, name='example', routing='', settings='', source=None):
        (self.root / f'{name}.toml').write_text(
            f'[plugin]\nid="{name}"\nversion="1"\napi_version=1\nenabled=true\n' + routing + settings)
        (self.root / f'{name}.py').write_text(source or
            'from botcore.api import Command, Registration, Response, Routing\n'
            'async def handler(context): return Response("ok")\n'
            'def register(settings):\n'
            '    return Registration(commands=(Command("example", handler),), routing=Routing(reply_mode="dm"))\n')

    async def test_operator_overrides_registration_defaults_without_changing_settings(self):
        self.plugin(routing='[routing]\nlisten_mode="channels"\nchannels=[2]\nreply_mode="origin"\n')
        report = await load_plugins(self.runtime, self.root)
        self.assertEqual(report['example']['status'], 'active')
        self.assertFalse(await self.runtime.dispatch(Message('example', 123, 2, direct=True)))
        self.assertFalse(await self.runtime.dispatch(Message('example', 123, 3)))
        self.assertTrue(await self.runtime.dispatch(Message('example', 123, 2)))
        self.assertEqual(self.sent, [('ok', None, 2, 'primary')])

    async def test_missing_routing_preserves_plugin_defaults(self):
        self.plugin()
        await load_plugins(self.runtime, self.root)
        await self.runtime.dispatch(Message('example', 123, 7))
        self.assertEqual(self.sent, [('ok', 123, 7, 'primary')])

    async def test_invalid_routing_rejected_before_startup_and_other_plugins_load(self):
        self.plugin(name='good')
        self.plugin(name='bad', routing='[routing]\nlisten_mode="channel"\n', source=
            'from botcore.api import Registration\n'
            'async def start(services): services.set("started", True)\n'
            'def register(settings): return Registration(start=start)\n')
        with self.assertLogs('botcore.loader', level='WARNING'):
            report = await load_plugins(self.runtime, self.root)
        self.assertEqual(report['bad']['status'], 'rejected')
        self.assertIn('routing.listen_mode', report['bad']['reason'])
        self.assertEqual(report['good']['status'], 'active')
        self.assertNotIn('bad', self.runtime.plugins)
        self.assertIsNone(PluginServices(self.runtime, 'bad').get('started'))

    async def test_cmd_lists_only_commands_available_on_the_request_route(self):
        for name in ('cmd', 'ping'):
            shutil.copyfile(ROOT / f'plugins.d/{name}.py', self.root / f'{name}.py')
        shutil.copyfile(ROOT / 'plugins.d/cmd.toml', self.root / 'cmd.toml')
        (self.root / 'ping.toml').write_text(
            '[plugin]\nid="ping"\nversion="1"\napi_version=1\nenabled=true\n'
            '[routing]\nlisten_mode="channels"\nchannels=[2]\n')
        await load_plugins(self.runtime, self.root)
        for direct, channel, include_ping in ((False, 2, True), (False, 3, False), (True, 2, False)):
            self.sent.clear()
            await self.runtime.dispatch(Message('cmd', 123, channel, direct=direct))
            names = self.sent[0][0].splitlines()[1:]
            self.assertEqual('ping' in names, include_ping)
            self.assertNotIn('pinging', names)

    async def test_ping_legacy_private_setting_and_new_operator_override(self):
        shutil.copyfile(ROOT / 'plugins.d/ping.py', self.root / 'ping.py')
        # Simulate the installer preserving the old manifest on upgrade.
        for legacy, explicit, target in ((True, None, 123), (False, None, None),
                                         (True, 'origin', None), (False, 'dm', 123)):
            with self.subTest(legacy=legacy, explicit=explicit):
                await self.runtime.close()
                self.runtime = Runtime(self.runtime.transport, ':memory:', send_delay=0)
                self.sent.clear()
                routing = f'[routing]\nreply_mode="{explicit}"\n' if explicit else ''
                (self.root / 'ping.toml').write_text(
                    '[plugin]\nid="ping"\nversion="1"\napi_version=1\nenabled=true\n' +
                    routing + f'[settings]\nreply_privately={str(legacy).lower()}\n')
                await load_plugins(self.runtime, self.root)
                await self.runtime.dispatch(Message('ping', 123, 2))
                self.assertEqual(self.sent[0][1:], (target, 2, 'primary'))

    async def test_real_auto_ping_uses_core_routing_for_initial_and_delayed_replies(self):
        shutil.copyfile(ROOT / 'plugins.d/ping.py', self.root / 'ping.py')
        for mode, target in (('origin', None), ('dm', 123)):
            with self.subTest(mode=mode):
                await self.runtime.close()
                self.runtime = Runtime(self.runtime.transport, ':memory:', send_delay=0)
                self.sent.clear()
                (self.root / 'ping.toml').write_text(
                    '[plugin]\nid="ping"\nversion="1"\napi_version=1\nenabled=true\n'
                    f'[routing]\nlisten_mode="channels"\nchannels=[2]\nreply_mode="{mode}"\n'
                    '[settings]\nallow_channel_sequences=true\n')
                await load_plugins(self.runtime, self.root)
                module = sys.modules['bot_plugin_ping']
                with patch.object(module, 'monotonic', return_value=0) as clock:
                    self.assertTrue(await self.runtime.dispatch(Message('ping 2', 123, 2)))
                    for when in (20, 40):
                        clock.return_value = when
                        await self.runtime.plugins['ping'].registration.jobs[0].handler(
                            PluginServices(self.runtime, 'ping'))
                self.assertEqual(len(self.sent), 3)
                self.assertTrue(all(item[1:] == (target, 2, 'primary') for item in self.sent))
