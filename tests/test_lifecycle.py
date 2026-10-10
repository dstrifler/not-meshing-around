import asyncio
import contextlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from botcore import controller
from botcore.api import Command, Job, Message, Registration, Response
from botcore.runtime import PluginServices, Runtime
from botcore.transport import MeshtasticTransport


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []

        async def send(*args):
            self.sent.append(args)

        self.runtime = Runtime(SimpleNamespace(send=send), ":memory:", send_delay=0,
                               send_timeout=0.05)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_jobs_wait_for_management_start(self):
        fired = asyncio.Event()

        async def job(services):
            fired.set()

        await self.runtime.activate("timer", Registration(jobs=(Job("tick", 0.001, job),)))
        await asyncio.sleep(0.01)
        self.assertFalse(fired.is_set())
        self.runtime.start()
        self.runtime.start()  # Repeated readiness notification must not duplicate jobs.
        await asyncio.wait_for(fired.wait(), 1)
        self.assertEqual(len(self.runtime.plugins["timer"].tasks), 1)

    async def test_all_jobs_finish_cancellation_before_any_stop_hook(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def job(services):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cancelled.set()

        async def stop(services):
            self.assertTrue(cancelled.is_set())
            self.assertTrue(all(t.done() for t in self.runtime.plugins["first"].tasks))
            self.assertFalse(await self.runtime.dispatch(Message("ping", 1)))
            services.set("stopped", True)  # Storage remains available during hooks.

        await self.runtime.activate("first", Registration(jobs=(Job("tick", 0.001, job),)))
        await self.runtime.activate("last", Registration(stop=stop))
        self.runtime.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.runtime.quiesce()
        await asyncio.sleep(0)
        await asyncio.wait_for(self.runtime.close(), 1)
        self.assertEqual(self.runtime.health()["last"]["errors"], 0)
        self.assertEqual(self.runtime.health()["first"]["status"], "stopped")

    async def test_stalled_send_times_out_and_next_command_can_reply(self):
        entered = asyncio.Event()

        async def send(text, *args):
            if text == "hang":
                entered.set()
                await asyncio.Event().wait()
            self.sent.append(text)

        async def handler(context):
            return Response(context.arguments)

        self.runtime.transport.send = send
        await self.runtime.activate("echo", Registration(commands=(Command("echo", handler),)))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            first = asyncio.create_task(self.runtime.dispatch(Message("echo hang", 1, direct=True)))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(self.runtime.dispatch(Message("echo ok", 2, direct=True)))
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(self.sent, ["ok"])
        self.assertFalse(self.runtime.delivery_lock.locked())
        self.assertEqual(self.runtime.health()["echo"]["errors"], 1)
        self.assertEqual(self.runtime.health()["echo"]["last_error"], "transport send timed out")

    async def test_delivery_lock_wait_is_also_bounded(self):
        await self.runtime.activate("p", Registration())
        await self.runtime.delivery_lock.acquire()
        try:
            with self.assertRaisesRegex(TimeoutError, "queue wait"):
                await asyncio.wait_for(PluginServices(self.runtime, "p").reply(
                    Response("ok"), Message("ping", 1, direct=True)), 1)
        finally:
            self.runtime.delivery_lock.release()
        await self.runtime.deliver("p", Response("ok"), Message("ping", 1, direct=True))
        self.assertEqual(len(self.sent), 1)

    async def test_timeout_does_not_limit_normal_multipart_pacing(self):
        self.runtime.byte_limit = 20
        self.runtime.send_timeout = 0.01
        self.runtime.send_delay = 0.02
        await self.runtime.deliver("p", Response("x" * 40), Message("ping", 1, direct=True))
        self.assertGreater(len(self.sent), 1)
        self.assertFalse(self.runtime.delivery_lock.locked())


class FakePub:
    def __init__(self):
        self.listeners = {}
        self.fail_topic = None

    def subscribe(self, handler, topic):
        if topic == self.fail_topic:
            raise RuntimeError("subscription failed")
        self.listeners.setdefault(topic, []).append(handler)

    def unsubscribe(self, handler, topic):
        self.listeners[topic].remove(handler)

    def publish(self, topic, **kwargs):
        for handler in list(self.listeners.get(topic, [])):
            handler(**kwargs)


class FakeInterface:
    def __init__(self):
        self.isConnected = threading.Event()
        self.myInfo = SimpleNamespace(my_node_num=42)
        self.closed = False
        self.connect_error = None

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.isConnected.set()

    def waitForConfig(self):
        pass

    def close(self):
        self.closed = True
        self.isConnected.clear()


class RadioLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pub, self.interface = FakePub(), FakeInterface()

        def factory(**kwargs):
            self.assertFalse(kwargs["connectNow"])
            return self.interface

        self.modules = patch.dict(sys.modules, {
            "pubsub": SimpleNamespace(pub=self.pub),
            "meshtastic": ModuleType("meshtastic"),
            "meshtastic.tcp_interface": SimpleNamespace(TCPInterface=factory),
            "meshtastic.serial_interface": SimpleNamespace(SerialInterface=factory),
        })
        self.modules.start()
        self.radio = MeshtasticTransport({"type": "tcp", "hostname": "test"})

    async def asyncTearDown(self):
        try:
            await self.radio.close()
        finally:
            self.modules.stop()

    async def test_disconnect_from_sdk_thread_reaches_management(self):
        await self.radio.start(SimpleNamespace())
        self.assertEqual(self.radio.health()["status"], "ready")
        monitor = asyncio.create_task(self.radio.run())
        self.pub.publish("meshtastic.connection.lost", interface=FakeInterface())
        await asyncio.sleep(0)
        self.assertFalse(monitor.done())
        thread = threading.Thread(target=self.pub.publish,
            args=("meshtastic.connection.lost",), kwargs={"interface": self.interface})
        with self.assertLogs("botcore.transport", level="ERROR"):
            thread.start()
            try:
                with self.assertRaisesRegex(ConnectionError, "connection lost"):
                    await asyncio.wait_for(monitor, 1)
            finally:
                thread.join(timeout=1)
        self.assertFalse(self.radio.accepting)
        self.assertEqual(self.radio.health()["status"], "failed")
        await self.radio.close()
        self.assertEqual(self.radio.health()["last_error"], "radio connection lost")
        self.assertTrue(self.interface.closed)
        self.assertTrue(all(not listeners for listeners in self.pub.listeners.values()))

    async def test_failed_handshake_retains_interface_for_cleanup(self):
        self.interface.connect_error = RuntimeError("handshake failed")
        with self.assertLogs("botcore.transport", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "handshake failed"):
                await self.radio.start(SimpleNamespace())
        self.assertEqual(self.radio.health()["status"], "failed")
        await self.radio.close()
        self.assertTrue(self.interface.closed)
        self.assertTrue(all(not listeners for listeners in self.pub.listeners.values()))

    async def test_partial_subscription_failure_is_cleaned_up(self):
        self.pub.fail_topic = "meshtastic.connection.lost"
        with self.assertLogs("botcore.transport", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "subscription failed"):
                await self.radio.start(SimpleNamespace())
        await self.radio.close()
        self.assertTrue(all(not listeners for listeners in self.pub.listeners.values()))

    async def test_cancelled_startup_closes_interface_after_connect_worker_settles(self):
        entered, release = threading.Event(), threading.Event()

        def connect():
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release connection worker")
            self.interface.isConnected.set()

        self.interface.connect = connect
        startup = asyncio.create_task(self.radio.start(SimpleNamespace()))
        try:
            async def wait_for_worker():
                while not entered.is_set():
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(wait_for_worker(), 1)
            startup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await startup
            cleanup = asyncio.create_task(self.radio.close())
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
            release.set()
            await asyncio.wait_for(cleanup, 1)
            self.assertTrue(self.interface.closed)
            self.assertTrue(all(not listeners for listeners in self.pub.listeners.values()))
        finally:
            release.set()
            await asyncio.gather(startup, return_exceptions=True)

    async def test_timed_out_threaded_send_prevents_overlapping_radio_sends(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def send_text(text, **kwargs):
            calls.append(text)
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release blocked send")

        self.interface.sendText = send_text
        await self.radio.start(SimpleNamespace())
        runtime = Runtime(self.radio, ":memory:", send_delay=0, send_timeout=0.05)
        try:
            with self.assertLogs("botcore.transport", level="ERROR"):
                with self.assertRaisesRegex(TimeoutError, "transport send"):
                    await asyncio.wait_for(runtime.deliver("p", Response("first"),
                        Message("ping", 1, direct=True)), 1)
            self.assertTrue(entered.is_set())
            self.assertFalse(runtime.delivery_lock.locked())
            with self.assertRaises(ConnectionError):
                await runtime.deliver("p", Response("second"), Message("ping", 1, direct=True))
            with self.assertRaisesRegex(ConnectionError, "timed out"):
                await self.radio.run()
            self.assertEqual(calls, ["first"])
        finally:
            release.set()
            await runtime.close()

    async def test_stopping_ingress_cancels_pending_commands_before_close(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def dispatch(message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await self.radio.start(SimpleNamespace(dispatch=dispatch))
        packet = {"from": 1, "to": 42, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"ping"}}
        self.radio._enqueue(packet)
        await asyncio.wait_for(entered.wait(), 1)
        await self.radio.stop_receiving()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.interface.closed)
        self.radio._enqueue(packet)
        self.assertFalse(self.radio.pending)


class ManagementLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "bot.toml"
        self.config.write_text('[radio]\ntype="console"\n')
        self.trace = []
        self.runtime = None
        self.transport = self.Transport(self.trace)
        self.output = io.StringIO()

    class Transport:
        def __init__(self, trace):
            self.trace = trace
            self.ready = self.closed = False
            self.start_error = self.run_error = None
            self.job_fired = asyncio.Event()

        async def start(self, runtime):
            self.trace.append("transport.start")
            await asyncio.sleep(0.01)
            if self.start_error:
                raise self.start_error
            self.ready = True

        async def send(self, *args):
            if not self.ready or self.closed:
                raise AssertionError("job sent outside transport ready window")
            self.job_fired.set()

        async def run(self):
            if self.run_error:
                raise self.run_error
            await self.job_fired.wait()

        async def stop_receiving(self):
            self.trace.append("ingress.stop")

        async def close(self):
            self.trace.append("transport.close")
            self.closed = True
            self.ready = False

        def health(self):
            return {"status": "stopped" if self.closed else "ready" if self.ready else "new"}

    async def load(self, runtime, directory):
        self.runtime = runtime

        async def job(services):
            await services.reply(Response("ok"), Message("ping", 1, direct=True))

        async def stop(services):
            self.trace.append("plugin.stop")
            self.assertFalse(self.transport.closed)
            self.assertTrue(all(task.done() for task in runtime.plugins["p"].tasks))
            services.set("cleanup", True)

        await runtime.activate("p", Registration(jobs=(Job("tick", 0.001, job),), stop=stop))
        await asyncio.sleep(0.01)  # Loading later plugins must not enable early jobs.
        return {"p": {"status": "active"}}

    async def run_controller(self, check=False):
        with patch.object(controller, "ConsoleTransport", return_value=self.transport), \
                patch.object(controller, "load_plugins", self.load), \
                contextlib.redirect_stdout(self.output):
            await asyncio.wait_for(controller.run(self.config, check=check), 1)

    async def test_jobs_run_only_while_ready_and_cleanup_precedes_transport_close(self):
        await self.run_controller()
        self.assertTrue(self.transport.job_fired.is_set())
        self.assertEqual(self.runtime.health()["p"]["errors"], 0)
        self.assertEqual(self.trace, ["transport.start", "ingress.stop", "plugin.stop", "transport.close"])
        with self.assertRaises(sqlite3.ProgrammingError):
            self.runtime.db.execute("SELECT 1")

    async def test_failed_transport_start_still_closes_resources_and_plugins(self):
        self.transport.start_error = RuntimeError("partial startup failed")
        with self.assertRaisesRegex(RuntimeError, "partial startup failed"):
            await self.run_controller()
        self.assertTrue(self.transport.closed)
        self.assertFalse(self.transport.job_fired.is_set())
        self.assertEqual(self.runtime.health()["p"]["status"], "stopped")
        self.assertEqual(self.trace[-2:], ["plugin.stop", "transport.close"])

    async def test_transport_failure_triggers_management_shutdown(self):
        self.transport.run_error = ConnectionError("radio connection lost")
        with self.assertRaisesRegex(ConnectionError, "connection lost"):
            await self.run_controller()
        self.assertTrue(self.transport.closed)
        self.assertEqual(self.runtime.health()["p"]["status"], "stopped")
        self.assertIn('"transport"', self.output.getvalue())

    async def test_check_runs_hooks_without_opening_transport_or_running_jobs(self):
        await self.run_controller(check=True)
        self.assertFalse(self.transport.job_fired.is_set())
        self.assertNotIn("transport.start", self.trace)
        self.assertEqual(self.trace, ["ingress.stop", "plugin.stop", "transport.close"])


if __name__ == "__main__":
    unittest.main()
