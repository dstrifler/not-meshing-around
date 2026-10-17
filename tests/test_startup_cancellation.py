import asyncio
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from botcore.api import Command, Message, Registration
from botcore.loader import load_plugins
from botcore.runtime import CleanupError, PluginServices, Runtime


CANCEL_SOURCE = '''
import asyncio
from botcore.api import Command, Job, Registration, Response
async def cancel(mode):
    if mode == "child":
        task = asyncio.create_task(asyncio.sleep(3600))
        task.cancel()
        await task
    elif mode == "self":
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
    else:
        raise asyncio.CancelledError("plugin cancelled")
'''


def write_plugin(directory, name, body):
    (directory / f"{name}.py").write_text(CANCEL_SOURCE + body)
    (directory / f"{name}.toml").write_text(
        f'[plugin]\nid="{name}"\nversion="1"\napi_version=1\nenabled=true\n')


class StartupCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.sent = []

        async def send(text, *args):
            self.sent.append(text)

        self.runtime = Runtime(SimpleNamespace(send=send), ":memory:", send_delay=0)

    async def asyncTearDown(self):
        try:
            with contextlib.suppress(CleanupError):
                await self.runtime.close()
        finally:
            self.tmp.cleanup()

    async def load(self):
        task = asyncio.create_task(load_plugins(self.runtime, self.directory))
        try:
            return await asyncio.wait_for(task, 10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def healthy_plugin(self):
        write_plugin(self.directory, "z_healthy", '''
async def handler(context):
    return Response("healthy")
def register(settings):
    return Registration(commands=(Command("healthy", handler),))
''')

    async def test_start_cancellation_rejects_plugin_and_loads_healthy_plugins(self):
        self.healthy_plugin()
        for mode in ("child", "self", "raise"):
            write_plugin(self.directory, f"a_{mode}", f'''
async def start(services):
    services.set("started", True)
    await cancel({mode!r})
async def stop(services):
    services.set("cleaned", True)
async def handler(context):
    return Response("must not run")
async def job(services):
    raise AssertionError("rejected plugin job ran")
def register(settings):
    return Registration(commands=(Command("broken", handler),),
        jobs=(Job("tick", 1, job),), start=start, stop=stop)
''')
        with self.assertLogs("botcore.loader", level="WARNING"):
            report = await self.load()
        for mode in ("child", "self", "raise"):
            name = f"a_{mode}"
            self.assertEqual(report[name], {"status": "rejected", "reason": "plugin startup cancelled"})
            self.assertEqual(self.runtime.health()[name]["status"], "failed")
            self.assertTrue(PluginServices(self.runtime, name).get("cleaned"))
            self.assertEqual(self.runtime.plugins[name].tasks, [])
        self.assertEqual(set(self.runtime.commands), {"healthy"})
        self.assertEqual(report["z_healthy"]["status"], "active")
        await self.runtime.dispatch(Message("healthy", 1, direct=True))
        self.assertEqual(self.sent, ["healthy"])

    async def test_rollback_cancellation_is_retained_and_loading_continues(self):
        self.healthy_plugin()
        for mode in ("child", "self", "raise"):
            write_plugin(self.directory, f"a_{mode}", f'''
async def start(services):
    raise ValueError("startup failed")
async def stop(services):
    await cancel({mode!r})
def register(settings):
    return Registration(start=start, stop=stop)
''')
        with self.assertLogs("botcore.runtime", level="ERROR"), \
                self.assertLogs("botcore.loader", level="WARNING"):
            report = await self.load()
        for mode in ("child", "self", "raise"):
            name = f"a_{mode}"
            self.assertEqual(report[name], {"status": "rejected", "reason": "startup failed"})
            self.assertEqual(self.runtime.health()[name]["errors"], 1)
            self.assertEqual(self.runtime.health()[name]["last_error"], "startup cleanup cancelled")
        self.assertEqual(report["z_healthy"]["status"], "active")
        with self.assertRaises(CleanupError) as caught:
            await self.runtime.close()
        self.assertEqual(set(caught.exception.failures), {"a_child", "a_self", "a_raise"})
        self.assertEqual(self.runtime.health()["z_healthy"]["status"], "stopped")
        with self.assertRaises(sqlite3.ProgrammingError):
            self.runtime.db.execute("SELECT 1")

    async def test_management_cancellation_prevents_activation_and_runs_rollback(self):
        for finish in ("propagate", "return", "raise"):
            with self.subTest(finish=finish):
                entered = asyncio.Event()

                async def start(services):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if finish == "return":
                            return
                        if finish == "raise":
                            raise OSError("startup cancellation cleanup failed")
                        raise

                async def stop(services):
                    await asyncio.sleep(0)
                    services.set("cleaned", True)

                async def command(context):
                    pass

                name = f"cancel_{finish}"
                task = asyncio.create_task(self.runtime.activate(name, Registration(
                    commands=(Command(name, command),), start=start, stop=stop)))
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    logs = self.assertLogs("botcore.runtime", level="ERROR") if finish == "raise" else contextlib.nullcontext()
                    with logs:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 10)
                    self.assertNotIn(name, self.runtime.commands)
                    self.assertEqual(self.runtime.health()[name]["status"], "failed")
                    self.assertTrue(PluginServices(self.runtime, name).get("cleaned"))
                    self.assertEqual(self.runtime.health()[name]["errors"], 1 if finish == "raise" else 0)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_local_rollback_cancellation_preserves_original_management_cancellation(self):
        entered = asyncio.Event()

        async def start(services):
            entered.set()
            await asyncio.Event().wait()

        async def stop(services):
            child = asyncio.create_task(asyncio.sleep(3600))
            child.cancel()
            await child

        task = asyncio.create_task(self.runtime.activate("demo", Registration(start=start, stop=stop)))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            with self.assertLogs("botcore.runtime", level="ERROR"):
                task.cancel("management stop")
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(task, 10)
            self.assertEqual(caught.exception.args, ("management stop",))
            self.assertEqual(self.runtime.health()["demo"]["last_error"], "startup cleanup cancelled")
            with self.assertRaisesRegex(CleanupError, "startup cleanup cancelled"):
                await self.runtime.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_management_can_interrupt_rollback_without_losing_its_errors(self):
        for finish in ("propagate", "return", "raise"):
            with self.subTest(finish=finish):
                entered = asyncio.Event()

                async def start(services):
                    raise ValueError("startup failed")

                async def stop(services):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if finish == "return":
                            return
                        if finish == "raise":
                            raise OSError("rollback release failed")
                        raise

                name = f"rollback_{finish}"
                task = asyncio.create_task(self.runtime.activate(name, Registration(start=start, stop=stop)))
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    with self.assertLogs("botcore.runtime", level="ERROR"):
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 10)
                    expected = "rollback release failed" if finish == "raise" else "startup cleanup cancelled"
                    self.assertEqual(self.runtime.health()[name]["last_error"], expected)
                    self.assertEqual(self.runtime.health()[name]["errors"], 1)
                    self.assertEqual(str(self.runtime.plugins[name].cleanup_error), expected)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(CleanupError) as caught:
            await self.runtime.close()
        self.assertEqual(len(caught.exception.failures), 3)


class StartupCheckTests(unittest.TestCase):
    def test_check_loads_healthy_plugin_and_reports_rollback_failures(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plugins = root / "plugins"
                plugins.mkdir()
                (root / "bot.toml").write_text('[core]\nplugins="plugins"\n')
                start = 'raise ValueError("startup failed")' if rollback else 'await cancel("child")'
                stop = 'await cancel("child")' if rollback else 'services.set("cleaned", True)'
                write_plugin(plugins, "a_broken", f'''
async def start(services):
    {start}
async def stop(services):
    {stop}
def register(settings):
    return Registration(start=start, stop=stop)
''')
                write_plugin(plugins, "z_healthy", '''
async def stop(services):
    await asyncio.sleep(0.01)
    services.set("cleaned", True)
def register(settings):
    return Registration(stop=stop)
''')
                result = subprocess.run([sys.executable, "mesh_bot.py", "--config", str(root / "bot.toml"), "--check"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1 if rollback else 0, result.stderr)
                self.assertIn('plugin_final_state plugin=z_healthy status=stopped', result.stderr)
                self.assertIn('plugin_final_state plugin=a_broken status=failed', result.stderr)
                self.assertIn('transport_final_state status=stopped', result.stderr)
                if rollback:
                    self.assertIn("startup cleanup cancelled", result.stderr)
                with sqlite3.connect(root / "data/core.db") as database:
                    self.assertEqual(database.execute(
                        'SELECT value FROM state WHERE plugin=? AND key=?', ("z_healthy", "cleaned")).fetchone(),
                        ("true",))
