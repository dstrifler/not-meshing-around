import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botcore import controller
from botcore.api import Command, Job, Registration, Subscription
from botcore.runtime import CleanupError, Runtime
from botcore.transport import MeshtasticTransport


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime(SimpleNamespace(), ":memory:")

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_slow_and_failed_hooks_do_not_skip_other_plugins(self):
        calls = []

        async def fast(services):
            calls.append("fast")
            services.set("cleaned", True)

        async def slow(services):
            calls.append("slow")
            await asyncio.Event().wait()

        async def broken(services):
            calls.append("broken")
            raise OSError("resource release failed")

        for name, hook in (("fast", fast), ("broken", broken), ("slow", slow)):
            await self.runtime.activate(name, Registration(stop=hook))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            with self.assertRaises(CleanupError) as caught:
                await asyncio.wait_for(self.runtime.close(timeout=0.05), 10)
        self.assertCountEqual(calls, ["fast", "broken", "slow"])
        self.assertEqual(set(caught.exception.failures), {"broken", "slow"})
        self.assertEqual(self.runtime.health()["fast"]["status"], "stopped")
        self.assertEqual(self.runtime.health()["broken"]["status"], "failed")
        self.assertEqual(self.runtime.health()["slow"]["last_error"], "shutdown hook timed out")
        with self.assertRaises(sqlite3.ProgrammingError):
            self.runtime.db.execute("SELECT 1")
        await self.runtime.close()
        self.assertEqual(len(calls), 3)

    async def test_all_hooks_begin_before_waiting_for_one_to_finish(self):
        first, second = asyncio.Event(), asyncio.Event()

        async def first_stop(services):
            first.set()
            await second.wait()
            services.set("cleaned", True)

        async def second_stop(services):
            second.set()
            await first.wait()
            services.set("cleaned", True)

        await self.runtime.activate("first", Registration(stop=first_stop))
        await self.runtime.activate("second", Registration(stop=second_stop))
        await asyncio.wait_for(self.runtime.close(timeout=0.1), 10)
        self.assertTrue(first.is_set() and second.is_set())
        self.assertTrue(all(item["status"] == "stopped" for item in self.runtime.health().values()))

    async def test_failed_startup_cleanup_is_preserved_for_management(self):
        async def start(services):
            raise ValueError("initialization failed")

        async def stop(services):
            raise OSError("startup resource release failed")

        with self.assertLogs("botcore.runtime", level="ERROR"):
            with self.assertRaisesRegex(ValueError, "initialization failed"):
                await self.runtime.activate("broken", Registration(start=start, stop=stop))
        with self.assertRaisesRegex(CleanupError, "startup resource release failed"):
            await self.runtime.close()
        self.assertEqual(self.runtime.health()["broken"]["errors"], 1)

    async def test_cancelled_child_in_stop_hook_does_not_interrupt_other_cleanup(self):
        other_started, release_other = asyncio.Event(), asyncio.Event()
        background = asyncio.create_task(asyncio.Event().wait())
        other_tasks, cleaned = [], []

        async def cancelled_stop(services):
            await other_started.wait()
            background.cancel()
            await background

        async def other_stop(services):
            other_tasks.append(asyncio.current_task())
            other_started.set()
            await release_other.wait()
            services.set("cleaned", True)
            cleaned.append(services.get("cleaned"))

        await self.runtime.activate("cancelled", Registration(stop=cancelled_stop))
        await self.runtime.activate("other", Registration(stop=other_stop))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            closing = asyncio.create_task(self.runtime.close())
            try:
                async def cancellation_reported():
                    while self.runtime.health()["cancelled"]["status"] != "failed":
                        await asyncio.sleep(0)

                await asyncio.wait_for(cancellation_reported(), 10)
                release_other.set()
                with self.assertRaises(CleanupError) as caught:
                    await asyncio.wait_for(closing, 10)
                self.assertEqual(set(caught.exception.failures), {"cancelled"})
                self.assertEqual(str(caught.exception.failures["cancelled"]), "shutdown hook cancelled")
                self.assertEqual(cleaned, [True])
                self.assertEqual(self.runtime.health()["other"]["status"], "stopped")
                self.assertEqual(self.runtime.health()["other"]["errors"], 0)
                self.assertEqual(self.runtime.health()["cancelled"]["errors"], 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    self.runtime.db.execute("SELECT 1")
            finally:
                release_other.set()
                background.cancel()
                await asyncio.gather(closing, background, *other_tasks, return_exceptions=True)

    async def test_cancelling_close_still_propagates_after_hooks_release_resources(self):
        entered = [asyncio.Event(), asyncio.Event()]
        cleaned = []

        def hook(index):
            async def stop(services):
                entered[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    services.set("cleaned", True)
                    cleaned.append(index)
            return stop

        for index in range(2):
            await self.runtime.activate(f"plugin{index}", Registration(stop=hook(index)))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            closing = asyncio.create_task(self.runtime.close())
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 10)
                closing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await closing
            finally:
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
        self.assertCountEqual(cleaned, [0, 1])
        self.assertTrue(all(state["status"] == "failed" for state in self.runtime.health().values()))
        with self.assertRaises(sqlite3.ProgrammingError):
            self.runtime.db.execute("SELECT 1")


class RadioConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_radio_is_rejected_before_plugin_loading_or_storage(self):
        cases = ('type="tcp"', 'type="tcp"\nhostname=""', 'type="tcp"\nhostname=123',
                 'type="tcp"\nhostname="node"\nport="4403"',
                 'type="tcp"\nhostname="node"\nport=true',
                 'type="tcp"\nhostname="node"\nport=0',
                 'type="tcp"\nhostname="node"\nport=65536',
                 'type="serial"\ndevice=""', 'type="serial"\ndevice=false')
        for settings in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "bot.toml"
                config.write_text('[radio]\n' + settings + '\n')
                with patch.object(controller, "load_plugins") as load:
                    with self.assertRaisesRegex(ValueError, "radio"):
                        await controller.run(config, check=True)
                    load.assert_not_called()
                self.assertFalse((root / "data").exists())

    async def test_valid_radio_check_never_imports_sdk_or_opens_hardware(self):
        async def load(runtime, directory):
            await runtime.activate("valid", Registration())
            return {"valid": {"status": "active"}}

        for settings in ('type="tcp"\nhostname="node"\nport=4403',
                         'type="serial"\ndevice="/dev/ttyACM0"', 'type="serial"'):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "bot.toml"
                config.write_text('[radio]\n' + settings + '\n')
                with patch.dict(sys.modules, {"meshtastic": None, "pubsub": None}), \
                        patch.object(controller, "load_plugins", load), \
                        patch.object(MeshtasticTransport, "start") as start, \
                        patch("builtins.print"):
                    await controller.run(config, check=True)
                    start.assert_not_called()


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Runtime(SimpleNamespace(), ":memory:")

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_generators_are_rejected_without_consumption_or_partial_activation(self):
        async def handler(context):
            pass

        objects = {"commands": Command("demo", handler), "jobs": Job("tick", 1, handler),
                   "events": Subscription("demo", handler)}
        for field, item in objects.items():
            consumed = []

            def entries():
                consumed.append(True)
                yield item

            with self.subTest(field=field):
                with self.assertRaisesRegex(TypeError, "must be a tuple"):
                    await self.runtime.activate(field, Registration(**{field: entries()}))
                self.assertEqual(consumed, [])
                self.assertNotIn(field, self.runtime.plugins)
                self.assertEqual(self.runtime.commands, {})

    async def test_alias_generator_is_rejected_before_lifecycle_hooks(self):
        called = []

        async def handler(context):
            pass

        async def start(services):
            called.append(True)

        with self.assertRaisesRegex(TypeError, "Command.aliases"):
            await self.runtime.activate("bad", Registration(
                commands=(Command("demo", handler, aliases=(name for name in ("alias",))),), start=start))
        self.assertEqual(called, [])
        self.assertEqual(self.runtime.plugins, {})


class CheckExitStatusTests(unittest.TestCase):
    def test_hook_failure_timeout_and_cancellation_return_nonzero_after_other_cleanup(self):
        for body, error in (("raise OSError('release failed')", "release failed"),
                            ("await asyncio.Event().wait()", "shutdown hook timed out"),
                            ("raise asyncio.CancelledError('cancelled child task')", "shutdown hook cancelled")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plugins = root / "plugins"
                plugins.mkdir()
                (root / "bot.toml").write_text('[core]\nplugins="plugins"\nshutdown_timeout=0.1\n')
                (plugins / "broken.toml").write_text(
                    '[plugin]\nid="broken"\nversion="1"\napi_version=1\nenabled=true\n')
                (plugins / "broken.py").write_text('import asyncio\nfrom botcore.api import Registration\n'
                    f'async def stop(services):\n    {body}\n'
                    'def register(settings): return Registration(stop=stop)\n')
                (plugins / "healthy.toml").write_text(
                    '[plugin]\nid="healthy"\nversion="1"\napi_version=1\nenabled=true\n')
                (plugins / "healthy.py").write_text('import asyncio\nfrom botcore.api import Registration\n'
                    'async def stop(services):\n    await asyncio.sleep(0.01)\n    services.set("cleaned", True)\n'
                    'def register(settings): return Registration(stop=stop)\n')
                result = subprocess.run([sys.executable, "mesh_bot.py", "--config", str(root / "bot.toml"), "--check"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                if error == 'release failed':
                    self.assertIn('error_type=OSError', result.stderr)
                else:
                    self.assertIn(error, result.stderr)
                self.assertIn('transport_final_state status=stopped', result.stderr)
                self.assertIn('plugin_final_state plugin=broken status=failed', result.stderr)
                self.assertIn('plugin_final_state plugin=healthy status=stopped', result.stderr)
                with sqlite3.connect(root / "data/core.db") as database:
                    self.assertEqual(database.execute(
                        'SELECT value FROM state WHERE plugin=? AND key=?', ("healthy", "cleaned")).fetchone(),
                        ("true",))


if __name__ == "__main__":
    unittest.main()
