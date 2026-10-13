"""Exercise the installed SDK's TCP loss paths without opening a socket."""

import asyncio
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

from botcore import controller
from botcore.api import Registration
from botcore.transport import MeshtasticTransport
from botcore.workers import blocking_call


class TCPDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = Mock()
        self.socket.recv.return_value = b""
        self.radio = MeshtasticTransport({"type": "tcp", "hostname": "test"})

        def connect(interface):
            interface.socket = self.socket
            interface.myInfo = SimpleNamespace(my_node_num=42)
            interface.isConnected.set()

        # Leave _readBytes/_writeBytes/_reconnect as the installed SDK implements
        # them. Only startup I/O and configuration exchange are replaced.
        self.startup = contextlib.ExitStack()
        self.startup.enter_context(patch.object(TCPInterface, "connect", connect))
        self.startup.enter_context(patch.object(TCPInterface, "waitForConfig"))
        self.reconnect = self.startup.enter_context(patch.object(TCPInterface, "myConnect"))
        self.config = self.startup.enter_context(patch.object(TCPInterface, "_startConfig"))
        self.addCleanup(self.startup.close)

    async def asyncTearDown(self):
        await self.radio.close()

    async def assert_loss(self, operation):
        await self.radio.start(SimpleNamespace())
        interface = self.radio.interface
        monitor = asyncio.create_task(self.radio.run())
        try:
            with self.assertLogs("botcore.transport", level="ERROR"):
                with self.assertRaisesRegex(ConnectionError, "radio connection lost"):
                    await blocking_call(operation, interface)
                with self.assertRaisesRegex(ConnectionError, "radio connection lost"):
                    await asyncio.wait_for(monitor, 10)
            self.assertEqual(self.radio.health(),
                             {"status": "failed", "last_error": "radio connection lost"})
            self.assertFalse(self.radio.accepting)
            self.assertTrue(interface._wantExit)
            self.reconnect.assert_not_called()
            self.config.assert_not_called()
            with self.assertRaises(ConnectionError):
                await self.radio.send("ping", 123, 0, "primary")
            await self.radio.close()
            self.socket.close.assert_called()
            self.assertIsNone(self.radio.interface)
            self.assertFalse(pub.isSubscribed(self.radio.connection_lost, "meshtastic.connection.lost"))
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def test_socket_eof_fails_transport_without_sdk_loss_notification(self):
        await self.assert_loss(lambda interface: interface._readBytes(1))

    async def test_socket_write_error_disables_sdk_reconnect(self):
        self.socket.sendall.side_effect = OSError("socket disconnected")
        await self.assert_loss(lambda interface: interface._writeBytes(b"ping"))

    async def test_reconnect_racing_shutdown_does_not_report_failure(self):
        await self.radio.start(SimpleNamespace())
        await self.radio.stop_receiving()
        with self.assertRaises(ConnectionError):
            await blocking_call(self.radio.interface._reconnect)
        await asyncio.sleep(0)
        self.assertFalse(self.radio._failure.is_set())
        self.reconnect.assert_not_called()
        await self.radio.close()
        self.assertEqual(self.radio.health()["status"], "stopped")

    async def test_socket_eof_triggers_management_cleanup_and_nonzero_error(self):
        stopped = []
        runtimes = []

        async def load(runtime, directory):
            runtimes.append(runtime)

            async def stop(services):
                services.set("stopped", True)
                stopped.append(True)

            await runtime.activate("demo", Registration(stop=stop))
            return {"demo": {"status": "active"}}

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bot.toml"
            config.write_text('[radio]\ntype="tcp"\nhostname="test"\n')
            with patch.object(controller, "MeshtasticTransport", return_value=self.radio), \
                    patch.object(controller, "load_plugins", load), \
                    contextlib.redirect_stdout(io.StringIO()):
                task = asyncio.create_task(controller.run(config))
                try:
                    async def ready():
                        while self.radio.status != "ready":
                            if task.done():
                                task.result()
                                self.fail("controller exited before transport was ready")
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(ready(), 10)
                    with self.assertLogs("botcore.transport", level="ERROR"):
                        with self.assertRaises(ConnectionError):
                            await blocking_call(self.radio.interface._readBytes, 1)
                        with self.assertRaisesRegex(ConnectionError, "radio connection lost"):
                            await asyncio.wait_for(task, 10)
                    self.assertEqual(stopped, [True])
                    self.assertEqual(runtimes[0].health()["demo"]["status"], "stopped")
                    with self.assertRaises(sqlite3.ProgrammingError):
                        runtimes[0].db.execute("SELECT 1")
                    self.socket.close.assert_called()
                    self.assertIsNone(self.radio.interface)
                    self.reconnect.assert_not_called()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
