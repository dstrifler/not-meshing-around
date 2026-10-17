import asyncio
import io
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botcore import controller
from botcore.api import Command, Event, EventQueueFull, Message, Registration, Response
from botcore.observability import (LoggingConfig, LoggingSession, RateLimit, RemoteSyslog,
                                   TextFormatter, event)
from botcore.runtime import PluginServices, Runtime
from botcore.transport import MeshtasticTransport, radio_message


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('botcore.test')


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('logging worker did not reach expected state')
        time.sleep(.002)


class LoggingConfigTests(unittest.TestCase):
    def test_defaults_and_invalid_settings(self):
        self.assertEqual(LoggingConfig.from_config({}), LoggingConfig())
        invalid = [False, [], {'level': 'verbose'}, {'level': 10}, {'level': ['INFO']},
                   {'message_content': 1}, {'message_content': 'false'}, {'format': 'json'},
                   {'output': 'file'}, {'output': 'rsyslog'}, {'output': 'both'},
                   {'rsyslog_host': False}, {'rsyslog_host': 'host\ninjected'},
                   {'rsyslog_host': 'user:password@host'}, {'rsyslog_protocol': 'tls'},
                   {'rsyslog_port': 0}, {'rsyslog_port': 65536}, {'rsyslog_port': True},
                   {'rsyslog_port': '514'}]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                LoggingConfig.from_config(config)
        for output in ('journald', 'rsyslog', 'both'):
            for protocol in ('tcp', 'udp'):
                config = LoggingConfig.from_config({'output': output, 'rsyslog_host': '::1',
                                                    'rsyslog_protocol': protocol, 'rsyslog_port': 5514})
                self.assertEqual(config.output, output)

    def test_private_fields_and_control_characters_are_safe_in_both_content_modes(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                stream = io.StringIO()
                session = LoggingSession(LoggingConfig(level='DEBUG', message_content=enabled), stream)
                try:
                    event(LOG, logging.DEBUG, 'received', content='hello\nFORGED\x1b world password=hunter2',
                          private_key='PRIVATE', publicKey='PUBLIC', channel_key='CHANNEL',
                          packet={'text': 'PACKET_SECRET'}, byte_count=42)
                    logging.getLogger('meshtastic.mesh_interface').error('SDK_PACKET_SECRET')
                finally:
                    session.close()
                value = stream.getvalue()
                self.assertEqual(len(value.splitlines()), 1)
                for secret in ('hunter2', 'PRIVATE', 'PUBLIC', 'CHANNEL', 'PACKET_SECRET', 'SDK_PACKET_SECRET'):
                    self.assertNotIn(secret, value)
                self.assertIn('byte_count=42', value)
                self.assertEqual('hello' in value, enabled)
                if enabled:
                    self.assertIn('\\u000aFORGED\\u001b', value)

    def test_exception_logs_keep_type_and_frames_but_never_raw_messages(self):
        stream = io.StringIO()
        session = LoggingSession(LoggingConfig(message_content=True), stream)
        try:
            try:
                raise ValueError('EXCEPTION_PAYLOAD_SECRET')
            except ValueError as error:
                event(LOG, logging.ERROR, 'operation_failed', error=error)
        finally:
            session.close()
        value = stream.getvalue()
        self.assertIn('error_type=ValueError', value)
        self.assertIn('test_exception_logs', value)
        self.assertNotIn('EXCEPTION_PAYLOAD_SECRET', value)

    def test_session_restores_existing_logging_and_level_filters(self):
        logger = logging.getLogger('botcore')
        original = (logger.handlers[:], logger.level, logger.propagate)
        stream = io.StringIO()
        session = LoggingSession(LoggingConfig(level='WARNING'), stream)
        try:
            event(LOG, logging.INFO, 'not_visible')
            event(LOG, logging.WARNING, 'visible')
        finally:
            session.close()
        self.assertNotIn('not_visible', stream.getvalue())
        self.assertIn('visible', stream.getvalue())
        self.assertEqual((logger.handlers, logger.level, logger.propagate), original)

    def test_core_validation_and_storage_codes_are_actionable_without_raw_error_text(self):
        stream = io.StringIO()
        session = LoggingSession(LoggingConfig(), stream)
        try:
            from botcore.api import Routing
            try:
                Routing(listen_mode='channel')
            except ValueError as error:
                event(LOG, logging.WARNING, 'plugin_rejected', error=error)
            event(LOG, logging.ERROR, 'io_failed', error=PermissionError(13, 'FILE_PAYLOAD_SECRET'))
            connection = sqlite3.connect(':memory:')
            try:
                connection.execute('SELECT * FROM PRIVATE_TABLE_NAME')
            except sqlite3.Error as error:
                event(LOG, logging.ERROR, 'storage_failed', error=error)
            finally:
                connection.close()
        finally:
            session.close()
        value = stream.getvalue()
        self.assertIn('routing.listen_mode', value)
        self.assertIn('errno=13', value)
        self.assertIn('sqlite_errorname=SQLITE_ERROR', value)
        self.assertNotIn('FILE_PAYLOAD_SECRET', value)
        self.assertNotIn('PRIVATE_TABLE_NAME', value)

    def test_repeated_warnings_report_suppressed_counts(self):
        limiter = RateLimit()
        with patch('botcore.observability.time.monotonic', return_value=100) as now:
            self.assertEqual(limiter.allow(), 0)
            for _ in range(20):
                self.assertIsNone(limiter.allow())
            now.return_value = 131
            self.assertEqual(limiter.allow(), 20)

    def test_native_journal_priority_prefixes_match_the_actual_stream(self):
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            with patch.dict(os.environ, {'JOURNAL_STREAM': f'{info.st_dev}:{info.st_ino}'}):
                session = LoggingSession(LoggingConfig(), stream)
                try:
                    event(LOG, logging.INFO, 'normal')
                    event(LOG, logging.ERROR, 'failed')
                finally:
                    session.close()
            stream.seek(0)
            lines = stream.read().splitlines()
            self.assertTrue(lines[0].startswith('<6>'))
            self.assertTrue(lines[1].startswith('<3>'))
        stream = io.StringIO()
        with patch.dict(os.environ, {'JOURNAL_STREAM': 'stale:descriptor'}):
            session = LoggingSession(LoggingConfig(), stream)
            try:
                event(LOG, logging.WARNING, 'normal_terminal')
            finally:
                session.close()
        self.assertNotIn('<4>', stream.getvalue())


class RemoteLoggingTests(unittest.TestCase):
    def test_udp_receiver_gets_rfc5424_text_and_both_matches_journal_body(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(3)
            stream = io.StringIO()
            config = LoggingConfig(output='both', rsyslog_host='127.0.0.1',
                                   rsyslog_protocol='udp', rsyslog_port=receiver.getsockname()[1])
            session = LoggingSession(config, stream)
            try:
                event(LOG, logging.WARNING, 'test_delivery', sender='!1234abcd', content='HIDDEN')
                payload = receiver.recv(65535).decode('utf-8')
            finally:
                session.close()
            self.assertTrue(payload.startswith('<12>1 '))
            self.assertIn(' meshbot ', payload)
            self.assertTrue(payload.endswith(stream.getvalue().strip()))
            self.assertNotIn('HIDDEN', payload)
            self.assertNotIn('\x00', payload)

    def test_tcp_octet_counting_frames_utf8_without_journal_duplication(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            receiver.listen(1)
            receiver.settimeout(3)
            stream = io.StringIO()
            session = LoggingSession(LoggingConfig(output='rsyslog', rsyslog_host='127.0.0.1',
                rsyslog_port=receiver.getsockname()[1], message_content=True), stream)
            try:
                event(LOG, logging.INFO, 'test_delivery', content='🌍 hello\nworld')
                with receiver.accept()[0] as connection:
                    connection.settimeout(3)
                    length = b''
                    while (byte := connection.recv(1)) != b' ':
                        self.assertTrue(byte)
                        length += byte
                    size = int(length)
                    data = b''
                    while len(data) < size:
                        chunk = connection.recv(size - len(data))
                        self.assertTrue(chunk)
                        data += chunk
            finally:
                session.close()
            self.assertEqual(len(data), size)
            self.assertIn('🌍 hello\\u000aworld', data.decode('utf-8'))
            self.assertTrue(data.startswith(b'<14>1 '))
            self.assertEqual(stream.getvalue(), '')

    def test_connection_failure_is_local_rate_limited_and_recovers(self):
        stream = io.StringIO()
        connection = Mock()
        with patch('botcore.observability.socket.create_connection', side_effect=[
                ConnectionRefusedError('NETWORK_SECRET'), connection]):
            session = LoggingSession(LoggingConfig(output='rsyslog', rsyslog_host='loghost'), stream)
            remote = next(h for h in session.handlers if isinstance(h, RemoteSyslog))
            try:
                event(LOG, logging.INFO, 'first')
                wait_until(lambda: remote.failures == 1 and remote.records.unfinished_tasks == 0
                           and remote.notices.unfinished_tasks == 0)
                for _ in range(20):
                    event(LOG, logging.INFO, 'during_outage')
                wait_until(lambda: remote.records.unfinished_tasks == 0)
                self.assertEqual(stream.getvalue().count('syslog_unavailable'), 1)
                self.assertNotIn('NETWORK_SECRET', stream.getvalue())
                remote._retry_at = 0
                event(LOG, logging.INFO, 'after_recovery')
                wait_until(lambda: connection.sendall.called and remote.records.unfinished_tasks == 0
                           and remote.notices.unfinished_tasks == 0)
                self.assertIn('syslog_recovered', stream.getvalue())
            finally:
                session.close()
        self.assertGreaterEqual(remote.dropped, 21)
        connection.close.assert_called_once()

    def test_blocked_dns_bounded_queue_and_shutdown_never_wait_for_network(self):
        entered, release = threading.Event(), threading.Event()
        connection = Mock()

        def connect(*args, **kwargs):
            entered.set()
            release.wait(5)
            return connection

        stream = io.StringIO()
        fallback = logging.StreamHandler(stream)
        fallback.setFormatter(TextFormatter())
        with patch('botcore.observability.socket.create_connection', side_effect=connect):
            remote = RemoteSyslog(LoggingConfig(output='rsyslog', rsyslog_host='unreachable'),
                                  fallback, capacity=2)
            remote.setFormatter(TextFormatter())
            record = logging.LogRecord('botcore.test', logging.INFO, '', 0, 'queued', (), None)
            try:
                remote.handle(record)
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                for _ in range(100):
                    remote.handle(record)
                self.assertLess(time.monotonic() - started, .5)
                self.assertLessEqual(remote.records.qsize(), 2)
                self.assertGreater(remote.dropped, 0)
                wait_until(lambda: remote.notices.unfinished_tasks == 0)
                self.assertEqual(stream.getvalue().count('syslog_queue_full'), 1)
                started = time.monotonic()
                remote.close()
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertIn('syslog_shutdown_incomplete', stream.getvalue())
            finally:
                release.set()
                remote.worker.join(timeout=2)
                remote.close()
            self.assertFalse(remote.worker.is_alive())
            connection.close.assert_called_once()

    def test_stalled_local_notice_cannot_block_remote_delivery_or_shutdown(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        connection = Mock()

        class StalledSink(logging.Handler):
            def emit(self, record):
                entered.set()
                release.wait(5)

        with patch('botcore.observability.socket.create_connection', side_effect=[
                ConnectionRefusedError('offline'), connection]):
            remote = RemoteSyslog(LoggingConfig(output='rsyslog', rsyslog_host='loghost'), StalledSink())
            remote.setFormatter(TextFormatter())
            closer = threading.Thread(target=lambda: (remote.close(), closed.set()), daemon=True)
            record = logging.LogRecord('botcore.test', logging.INFO, '', 0, 'queued', (), None)
            try:
                remote.handle(record)
                self.assertTrue(entered.wait(2))
                remote._retry_at = 0
                remote.handle(record)
                wait_until(lambda: connection.sendall.called)
                closer.start()
                self.assertTrue(closed.wait(1.5), 'local stderr stalled logging shutdown')
                self.assertTrue(remote.notice_worker.is_alive())
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(2)
                remote.close()
                remote.worker.join(2)
                remote.notice_worker.join(2)
            self.assertFalse(remote.notice_worker.is_alive())

    def test_queue_overflow_producer_and_notice_queue_stay_bounded_with_stalled_stderr(self):
        network_entered, notice_entered, release = (threading.Event() for _ in range(3))
        produced = threading.Event()

        def connect(*args, **kwargs):
            network_entered.set()
            release.wait(5)
            return Mock()

        class StalledSink(logging.Handler):
            def emit(self, record):
                notice_entered.set()
                release.wait(5)

        with patch('botcore.observability.socket.create_connection', side_effect=connect):
            remote = RemoteSyslog(LoggingConfig(output='rsyslog', rsyslog_host='loghost'),
                                  StalledSink(), capacity=2)
            remote.setFormatter(TextFormatter())
            record = logging.LogRecord('botcore.test', logging.INFO, '', 0, 'queued', (), None)

            def produce():
                for _ in range(100):
                    remote.handle(record)
                produced.set()

            producer = threading.Thread(target=produce, daemon=True)
            try:
                remote.handle(record)
                self.assertTrue(network_entered.wait(2))
                producer.start()
                self.assertTrue(produced.wait(1), 'queue overflow blocked the producer on stderr')
                self.assertTrue(notice_entered.wait(2))
                for _ in range(100):
                    remote._notice('syslog_recovered', force=True)
                self.assertEqual(remote.notices.qsize(), 16)
                self.assertEqual(remote.records.qsize(), 2)
                self.assertGreater(remote.dropped, 0)
            finally:
                release.set()
                if producer.ident is not None:
                    producer.join(2)
                remote.close()
                remote.worker.join(2)
                remote.notice_worker.join(2)

    def test_blocked_pipe_does_not_trap_python_logging_shutdown_at_process_exit(self):
        # Fill a real local output pipe without reading it. The diagnostic writer
        # must not hold a stream/Handler lock that atexit's logging.shutdown needs.
        script = '''
import logging, os, time
from botcore.observability import LoggingConfig, RemoteSyslog, TextFormatter
reader, writer = os.pipe()
os.set_blocking(writer, False)
try:
    while True:
        os.write(writer, b'x' * 4096)
except BlockingIOError:
    pass
os.set_blocking(writer, True)
stream = os.fdopen(writer, 'w')
fallback = logging.StreamHandler(stream)
fallback.setFormatter(TextFormatter())
remote = RemoteSyslog(LoggingConfig(output='rsyslog', rsyslog_host='loghost'), fallback)
remote._notice('test_blocked_pipe', force=True)
deadline = time.monotonic() + 1
while remote.notices.qsize() and time.monotonic() < deadline:
    time.sleep(.002)
assert remote.notices.unfinished_tasks == 1
remote.close()
print('closed', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, cwd=ROOT, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'closed')


class RequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream = io.StringIO()
        self.session = LoggingSession(LoggingConfig(level='DEBUG'), self.stream)
        self.sent = []

        async def send(*args):
            self.sent.append(args)
            return 12345

        self.runtime = Runtime(SimpleNamespace(send=send), ':memory:', send_delay=0)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.session.close()

    async def test_trace_ids_isolate_concurrent_requests_and_content_is_off_at_debug(self):
        async def handler(context):
            await asyncio.sleep(0)
            context.services.logger.info('plugin_processing', name='lookup', level='application',
                                         content=context.arguments, password='API_SECRET')
            return Response(context.arguments)

        await self.runtime.activate('echo', Registration(commands=(Command('echo', handler),)))
        messages = [Message('echo FIRST_PRIVATE', 1, 2, direct=True, packet_id=111),
                    Message('echo SECOND_PRIVATE', 2, 3, direct=True, packet_id=222)]
        await asyncio.gather(*(self.runtime.dispatch(message) for message in messages))
        value = self.stream.getvalue()
        for secret in ('FIRST_PRIVATE', 'SECOND_PRIVATE', 'API_SECRET'):
            self.assertNotIn(secret, value)
        for message in messages:
            lines = [line for line in value.splitlines() if f'trace={message.trace_id}' in line]
            self.assertTrue(any('message_received' in line for line in lines))
            self.assertTrue(any('command_started' in line for line in lines))
            self.assertTrue(any('plugin_processing' in line for line in lines))
            self.assertTrue(any('response_submitted' in line and 'tx_packet=12345' in line for line in lines))
            self.assertTrue(all(f'sender=!{message.sender:08x}' in line for line in lines))
        self.assertNotIn('delivered', value)
        self.assertIn('encryption=unknown', value)
        self.assertIn('encryption=firmware_decides', value)

    async def test_opt_in_logs_raw_input_and_formatted_reply(self):
        self.session.close()
        self.session = LoggingSession(LoggingConfig(message_content=True), self.stream)

        async def handler(context):
            return Response('OUTBOUND_PRIVATE')

        await self.runtime.activate('echo', Registration(commands=(Command('echo', handler),)))
        await self.runtime.dispatch(Message('echo INBOUND_PRIVATE', 1, direct=True, pki_encrypted=True))
        self.assertIn('echo INBOUND_PRIVATE', self.stream.getvalue())
        self.assertIn('OUTBOUND_PRIVATE', self.stream.getvalue())
        self.assertIn('encryption=pki', self.stream.getvalue())

    async def test_unknown_and_filtered_commands_have_debug_reasons_without_arguments(self):
        async def handler(context):
            pass

        await self.runtime.activate('admin', Registration(commands=(Command('secret', handler, admin_only=True),)))
        self.assertFalse(await self.runtime.dispatch(Message('secret HIDDEN_ARGS', 1)))
        self.assertFalse(await self.runtime.dispatch(Message('UNKNOWN_PRIVATE_MESSAGE', 1)))
        value = self.stream.getvalue()
        self.assertIn('reason=admin_required', value)
        self.assertIn('reason=unknown_command', value)
        self.assertNotIn('HIDDEN_ARGS', value)
        self.assertNotIn('UNKNOWN_PRIVATE_MESSAGE', value)

    async def test_routing_ack_nak_reports_and_bounded_correlation_without_raw_packets(self):
        radio = MeshtasticTransport({'type': 'tcp', 'hostname': 'radio'})
        radio.runtime = self.runtime
        radio.record_submission(17, {'trace': 'request17', 'plugin': 'ping'})
        await radio._handle({'from': 1, 'to': 42, 'publicKey': 'KEY_SECRET', 'decoded': {
            'portnum': 'ROUTING_APP', 'requestId': 17, 'routing': {'errorReason': 'PKI_SEND_FAIL_PUBLIC_KEY'}}})
        value = self.stream.getvalue()
        self.assertIn('radio_nak', value)
        self.assertIn('trace=request17', value)
        self.assertIn('ack_for=17', value)
        self.assertIn('guidance=destination_public_key_missing', value)
        self.assertNotIn('KEY_SECRET', value)
        radio.record_submission(18, {'trace': 'request18', 'plugin': 'ping'})
        await radio._handle({'from': 1, 'decoded': {'portnum': 'ROUTING_APP', 'requestId': 18, 'routing': {}}})
        self.assertIn('radio_ack trace=request18', self.stream.getvalue())
        for index in range(1100):
            radio.record_submission(index, {})
        self.assertEqual(len(radio._submissions), 1024)
        with patch('botcore.transport.monotonic', return_value=time.monotonic() + 121):
            radio._expire_submissions(time.monotonic() + 121)
        self.assertEqual(len(radio._submissions), 0)

    async def test_distinct_nak_reasons_keep_correlation_and_independent_suppression_counts(self):
        radio = MeshtasticTransport({'type': 'tcp', 'hostname': 'radio'})
        radio.runtime = self.runtime

        async def nak(packet_id, reason):
            radio.record_submission(packet_id, {'trace': f'request{packet_id}', 'plugin': 'ping'})
            await radio._handle({'from': 1, 'decoded': {'portnum': 'ROUTING_APP',
                'requestId': packet_id, 'routing': {'errorReason': reason}}})

        with patch('botcore.observability.time.monotonic', return_value=time.monotonic()) as now:
            await nak(1, 'NO_RESPONSE')
            await nak(2, 'PKI_SEND_FAIL_PUBLIC_KEY')
            await nak(3, 'NO_RESPONSE')
            await nak(4, 'PKI_SEND_FAIL_PUBLIC_KEY')
            await nak(5, 'PKI_SEND_FAIL_PUBLIC_KEY')
            lines = [line for line in self.stream.getvalue().splitlines() if ' radio_nak ' in line]
            self.assertEqual(len(lines), 2)
            self.assertIn('trace=request1', lines[0])
            self.assertIn('reason=NO_RESPONSE', lines[0])
            self.assertIn('trace=request2', lines[1])
            self.assertIn('guidance=destination_public_key_missing', lines[1])
            now.return_value += 31
            await nak(6, 'NO_RESPONSE')
            await nak(7, 'PKI_SEND_FAIL_PUBLIC_KEY')
        lines = [line for line in self.stream.getvalue().splitlines() if ' radio_nak ' in line]
        self.assertEqual(len(lines), 4)
        self.assertIn('suppressed=1', lines[2])
        self.assertIn('trace=request6', lines[2])
        self.assertIn('suppressed=2', lines[3])
        self.assertIn('trace=request7', lines[3])
        self.assertEqual(len(radio._submissions), 0)

    def test_nak_limiter_memory_is_bounded_even_for_unknown_firmware_reasons(self):
        radio = MeshtasticTransport({'type': 'tcp', 'hostname': 'radio'})
        for index in range(200):
            radio._log_routing({'decoded': {'routing': {'errorReason': f'ERROR_{index}'}}})
        self.assertEqual(len(radio._routing_warnings), 128)
        self.assertNotIn('ERROR_0', radio._routing_warnings)
        self.assertIn('ERROR_199', radio._routing_warnings)

    async def test_radio_event_overflow_uses_core_warning_without_per_packet_errors(self):
        radio = MeshtasticTransport({'type': 'tcp', 'hostname': 'radio'})
        radio.runtime = self.runtime
        for _ in range(self.runtime.events.maxsize):
            self.runtime.enqueue_event(Event('existing'))
        packet = {'from': 1, 'decoded': {'portnum': 'TELEMETRY_APP'}}
        with patch('botcore.observability.time.monotonic', return_value=time.monotonic()) as now:
            for _ in range(20):
                await radio._handle(packet)
            value = self.stream.getvalue()
            self.assertEqual(value.count(' event_queue_full '), 1)
            self.assertNotIn('radio_packet_failed', value)
            self.assertEqual(self.runtime.events.qsize(), self.runtime.events.maxsize)
            now.return_value += 31
            await radio._handle(packet)
            self.assertIn('event_queue_full suppressed=19', self.stream.getvalue())
        # Plugin callers still receive explicit backpressure.
        with self.assertRaises(EventQueueFull):
            self.runtime.enqueue_event(Event('plugin.event'))
        self.runtime.events.get_nowait()
        self.runtime.events.task_done()
        await radio._handle(packet)
        self.assertEqual(self.runtime.events.qsize(), self.runtime.events.maxsize)
        # Unexpected ingress failures remain visible.
        await radio._handle({'decoded': None})
        self.assertEqual(self.stream.getvalue().count(' radio_packet_failed '), 1)
        self.assertIn('error_type=AttributeError', self.stream.getvalue())

    async def test_invalid_logging_is_rejected_before_plugin_or_database_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'bot.toml'
            config.write_text('[logging]\noutput="both"\n')
            with patch.object(controller, 'load_plugins') as load:
                with self.assertRaisesRegex(ValueError, 'logging.rsyslog_host'):
                    await controller.run(config, check=True)
                load.assert_not_called()
            self.assertFalse((Path(directory) / 'data').exists())

    def test_radio_encryption_is_reported_only_when_explicitly_available(self):
        packet = {'from': 123, 'to': 42, 'id': 17, 'decoded': {'payload': b'ping'}}
        self.assertIsNone(radio_message(packet, 42).pki_encrypted)
        self.assertTrue(radio_message(packet | {'pkiEncrypted': True}, 42).pki_encrypted)
        self.assertFalse(radio_message(packet | {'pki_encrypted': False}, 42).pki_encrypted)
        self.assertIsNone(radio_message(packet | {'pkiEncrypted': 'true'}, 42).pki_encrypted)


class LoggingCLITests(unittest.TestCase):
    def test_check_and_console_output_have_readable_lifecycle_logs_without_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'bot.toml'
            config.write_text(f'[core]\nplugins="{ROOT / "plugins.d"}"\nsend_delay=0\n'
                              '[logging]\nlevel="DEBUG"\n')
            result = subprocess.run([sys.executable, 'mesh_bot.py', '--config', str(config)],
                input='ping #LOGGING_PRIVATE\n', capture_output=True, text=True, cwd=ROOT, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Pong.', result.stdout)  # Interactive console output is not a log.
            self.assertNotIn('LOGGING_PRIVATE', result.stderr)
            self.assertIn('bot_ready', result.stderr)
            self.assertIn('message_received', result.stderr)
            self.assertIn('bot_stopped cleanup_failures=0', result.stderr)
            result = subprocess.run([sys.executable, 'mesh_bot.py', '--config', str(config), '--check'],
                                    capture_output=True, text=True, cwd=ROOT, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertIn('mode=check', result.stderr)
            self.assertNotIn('radio_connecting', result.stderr)
