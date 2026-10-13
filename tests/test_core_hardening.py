import asyncio
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botcore import controller
from botcore.api import Command, Event, EventQueueFull, Message, Registration, Subscription
from botcore.runtime import PluginServices, Policy, Runtime


class PolicyConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_policy_is_rejected_before_plugins_or_database_are_opened(self):
        for field, value in (("blocked", '["123"]'), ("admins", '[true, 1]'),
                             ("blocked", '"123"'), ("blocked", '[-1]'),
                             ("blocked", '[4294967296]'), ("blocked", '[{}]'),
                             ("broadcast_plugins", '"ping"'),
                             ("broadcast_plugins", '[123]'),
                             ("broadcast_plugins", '["Not a plugin"]')):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "bot.toml"
                config.write_text(f'[policy]\n{field}={value}\n')
                with patch.object(controller, "load_plugins") as load:
                    with self.assertRaisesRegex(ValueError, "policy"):
                        await controller.run(config, check=True)
                    load.assert_not_called()
                self.assertFalse((Path(tmp) / "data").exists())

    def test_integer_and_hex_node_ids_enforce_the_same_policy(self):
        config = tomllib.loads('[policy]\nadmins=[0x1234abcd]\nblocked=[123]\nbroadcast_plugins=["ping"]')
        policy = Policy.from_config(config["policy"])

        async def handler(context):
            pass

        self.assertFalse(policy.allows(Message("demo", 123), Command("demo", handler)))
        self.assertTrue(policy.allows(Message("demo", 0x1234abcd),
                                      Command("demo", handler, admin_only=True)))
        self.assertFalse(policy.allows(Message("demo", 456),
                                       Command("demo", handler, admin_only=True)))
        self.assertEqual(policy.broadcast_plugins, frozenset({"ping"}))


class EventBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime(SimpleNamespace(), ":memory:")

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_full_queue_rejects_emission_without_waiting_for_a_consumer(self):
        await self.runtime.activate("p", Registration())
        for _ in range(self.runtime.events.maxsize):
            self.runtime.enqueue_event(Event("padding"))
        with self.assertRaises(EventQueueFull):
            await asyncio.wait_for(PluginServices(self.runtime, "p").emit(Event("extra")), 1)
        self.assertEqual(self.runtime.events.qsize(), self.runtime.events.maxsize)

    async def test_subscriber_overflow_does_not_stall_other_subscribers(self):
        observed = asyncio.Event()

        async def producer(event, services):
            await services.emit(Event("child"))
            await services.emit(Event("overflow"))

        async def observer(event, services):
            observed.set()

        await self.runtime.activate("producer", Registration(events=(Subscription("seed", producer),)))
        await self.runtime.activate("observer", Registration(events=(Subscription("seed", observer),)))
        self.runtime.enqueue_event(Event("seed"))
        for _ in range(self.runtime.events.maxsize - 1):
            self.runtime.enqueue_event(Event("padding"))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            consumer = asyncio.create_task(self.runtime.event_loop())
            try:
                await asyncio.wait_for(observed.wait(), 1)
            finally:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
        self.assertEqual(self.runtime.health()["producer"]["errors"], 1)
        self.assertEqual(self.runtime.health()["producer"]["last_error"], "event queue is full")


class ProcessLifecycleTests(unittest.TestCase):
    child = '''
import asyncio, sys
from pathlib import Path
from botcore import controller
from botcore.api import Registration
root, phase = Path(sys.argv[1]), sys.argv[2]
class SlowTransport(controller.ConsoleTransport):
    async def start(self, runtime):
        (root / "starting").write_text("radio")
        await asyncio.Event().wait()
    async def close(self):
        (root / "transport-cleaned").write_text("yes")
async def load(runtime, directory):
    async def start(services):
        if phase == "plugin":
            (root / "starting").write_text("plugin")
            await asyncio.Event().wait()
    async def stop(services):
        (root / "cleaning").write_text("yes")
        if phase == "second-signal":
            await asyncio.sleep(.2)
        (root / "plugin-cleaned").write_text("yes")
    await runtime.activate("demo", Registration(start=start, stop=stop))
    return {"demo": {"status": "active"}}
controller.ConsoleTransport = SlowTransport
controller.load_plugins = load
asyncio.run(controller.run(root / "bot.toml"))
'''

    def await_marker(self, process, path):
        deadline = time.monotonic() + 2
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(path.exists(), f"Child did not reach {path.name}")

    def test_sigterm_during_initialization_runs_cleanup(self):
        for phase in ("plugin", "radio", "second-signal"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "bot.toml").write_text('[core]\nstartup_timeout=5\nshutdown_timeout=1\n[radio]\ntype="console"\n')
                process = subprocess.Popen([sys.executable, "-c", self.child, str(root), phase],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.await_marker(process, root / "starting")
                    process.send_signal(signal.SIGTERM)
                    if phase == "second-signal":
                        self.await_marker(process, root / "cleaning")
                        process.send_signal(signal.SIGTERM)
                    _, errors = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 0, errors.decode())
                    self.assertTrue((root / "plugin-cleaned").exists())
                    self.assertTrue((root / "transport-cleaned").exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_stuck_sdk_worker_does_not_hold_process_exit(self):
        source = '''
import asyncio, threading
from botcore.workers import blocking_call
async def main():
    worker = asyncio.create_task(blocking_call(threading.Event().wait))
    await asyncio.sleep(.01)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
asyncio.run(main())
'''
        result = subprocess.run([sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
