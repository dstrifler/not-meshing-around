import asyncio
import json
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
    def test_hook_failure_and_timeout_return_nonzero_after_other_cleanup(self):
        for body, error in (("raise OSError('release failed')", "release failed"),
                            ("await asyncio.Event().wait()", "shutdown hook timed out")):
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
                result = subprocess.run([sys.executable, "mesh_bot.py", "--config", str(root / "bot.toml"), "--check"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(error, result.stderr)
                health = json.loads(result.stdout.splitlines()[-1])
                self.assertEqual(health["transport"]["status"], "stopped")
                self.assertEqual(health["plugins"]["broken"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
