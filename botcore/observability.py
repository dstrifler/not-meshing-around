"""One text log format, private payload opt-in, and bounded remote delivery."""

import contextlib
import contextvars
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone


CONTEXT = contextvars.ContextVar('meshbot_log_context', default={})
LEVELS = {name: getattr(logging, name) for name in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')}
SECRET = re.compile(r'key|password|passwd|credential|secret|token|psk|authorization', re.I)
SECRET_VALUE = re.compile(
    r'\b((?:(?:private|public|api|channel|encryption|access)[_ -]?)?key|password|passwd|credential|secret|token|psk|authorization)'
    r'\s*[:=]\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)', re.I)
SAFE_ERRORS = frozenset({
    'shutdown hook timed out', 'shutdown hook cancelled', 'job cancellation timed out',
    'startup cleanup cancelled', 'startup cleanup timed out', 'plugin handler cancelled',
    'plugin startup cancelled', 'transport send timed out', 'delivery queue wait timed out',
    'event queue is full', 'bot startup timed out', 'no active plugins',
})
SAFE_VALIDATION = {
    ('api.py', '__post_init__'), ('api.py', 'from_config'),
    ('loader.py', 'load_plugins'), ('runtime.py', 'validate'),
    ('runtime.py', '_deliver'), ('runtime.py', 'chunks'),
    ('runtime.py', '_payload_chunks'), ('workers.py', 'positive_seconds'),
    ('transport.py', 'validate_radio'), ('runtime.py', 'from_config'),
}


@dataclass(frozen=True)
class LoggingConfig:
    level: str = 'INFO'
    message_content: bool = False
    output: str = 'journald'
    rsyslog_host: str = ''
    rsyslog_protocol: str = 'tcp'
    rsyslog_port: int = 514

    @classmethod
    def from_config(cls, config):
        if not isinstance(config, dict):
            raise ValueError('logging must be a TOML table')
        if set(config) - set(cls.__dataclass_fields__):
            raise ValueError('unknown logging setting')
        result = cls(**config)
        if not isinstance(result.level, str) or result.level not in LEVELS:
            raise ValueError('logging.level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL')
        if type(result.message_content) is not bool:
            raise ValueError('logging.message_content must be a boolean')
        if result.output not in ('journald', 'rsyslog', 'both'):
            raise ValueError('logging.output must be journald, rsyslog, or both')
        if (not isinstance(result.rsyslog_host, str) or
                any(c.isspace() or ord(c) < 32 for c in result.rsyslog_host) or
                any(c in result.rsyslog_host for c in '/@')):
            raise ValueError('logging.rsyslog_host must be a hostname or IP address without credentials')
        if result.output != 'journald' and not result.rsyslog_host:
            raise ValueError('logging.rsyslog_host is required for rsyslog output')
        if result.rsyslog_protocol not in ('tcp', 'udp'):
            raise ValueError('logging.rsyslog_protocol must be tcp or udp')
        if type(result.rsyslog_port) is not int or not 1 <= result.rsyslog_port <= 65535:
            raise ValueError('logging.rsyslog_port must be an integer from 1 to 65535')
        return result


def text(value):
    """Escape control characters so a packet cannot forge another log line."""
    value = SECRET_VALUE.sub(lambda match: match[1] + '=<redacted>', str(value))
    return ''.join(f'\\u{ord(c):04x}' if ord(c) < 32 or 127 <= ord(c) < 160 or c in '\u2028\u2029'
                   else c for c in value)


def error_fields(error):
    # Exception messages and traceback source lines can contain payloads or keys.
    result = {'error_type': type(error).__name__}
    for attribute in ('errno', 'sqlite_errorcode'):
        value = getattr(error, attribute, None)
        if type(value) is int:
            result[attribute] = value
    sqlite_name = getattr(error, 'sqlite_errorname', None)
    if isinstance(sqlite_name, str) and re.fullmatch(r'SQLITE_[A-Z_]+', sqlite_name):
        result['sqlite_errorname'] = sqlite_name
    frames = traceback.extract_tb(error.__traceback__)
    last = frames[-1] if frames else None
    trusted_validation = (type(error) in (ValueError, TypeError) and last is not None and
                          os.path.dirname(last.filename) == os.path.dirname(__file__) and
                          (os.path.basename(last.filename), last.name) in SAFE_VALIDATION)
    if str(error) in SAFE_ERRORS or trusted_validation:
        result['reason'] = str(error)
    result['stack'] = ' > '.join(f'{frame.filename}:{frame.lineno}:{frame.name}'
                                 for frame in frames)
    return result


@contextlib.contextmanager
def log_context(**fields):
    token = CONTEXT.set(CONTEXT.get() | fields)
    try:
        yield
    finally:
        CONTEXT.reset(token)


def event(logger, level, name, /, **fields):
    error = fields.pop('error', None)
    if error is not None:
        fields.update(error_fields(error))
    logger.log(level, name, extra={'fields': CONTEXT.get() | fields})


def node_id(value):
    return f'!{value:08x}' if type(value) is int and 0 <= value <= 0xffffffff else 'unknown'


def message_fields(message):
    return {'trace': message.trace_id, 'rx_packet': message.packet_id,
            'sender': node_id(message.sender), 'channel': message.channel,
            'interface': message.interface, 'audience': 'dm' if message.direct else 'channel'}


class PluginLogger:
    """Structured fields share core formatting, destinations, and content gating."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = logging.getLogger(f'botcore.plugins.{plugin}')

    def log(self, level, name, /, **fields):
        event(self.logger, level, name, **(fields | {'plugin': self.plugin}))

    def debug(self, name, /, **fields):
        self.log(logging.DEBUG, name, **fields)

    def info(self, name, /, **fields):
        self.log(logging.INFO, name, **fields)

    def warning(self, name, /, **fields):
        self.log(logging.WARNING, name, **fields)

    def error(self, name, /, **fields):
        self.log(logging.ERROR, name, **fields)


class TextFormatter(logging.Formatter):
    def __init__(self, message_content=False):
        super().__init__()
        self.message_content = message_content

    def format(self, record):
        stamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds')
        fields = dict(getattr(record, 'fields', {}))
        if record.exc_info and record.exc_info[1]:
            fields.update(error_fields(record.exc_info[1]))
        parts = [stamp, record.levelname, text(record.name), text(record.getMessage())]
        for key, value in fields.items():
            if SECRET.search(key) or (key == 'content' and not self.message_content):
                continue
            if isinstance(value, (dict, list, tuple, set, bytes, bytearray)):
                value = '<omitted>'  # No configuration/packet dumps, even at DEBUG.
            rendered = text(value)
            if ' ' in rendered or '"' in rendered or rendered == '':
                rendered = '"' + rendered.replace('"', '\\"') + '"'
            parts.append(f'{text(key)}={rendered}')
        return ' '.join(parts)


def syslog_severity(level):
    if level >= logging.CRITICAL:
        return 2
    if level >= logging.ERROR:
        return 3
    if level >= logging.WARNING:
        return 4
    return 6 if level >= logging.INFO else 7


class JournalStream(logging.StreamHandler):
    """Systemd consumes priority prefixes; ordinary terminals stay plain text."""

    def __init__(self, stream):
        super().__init__(stream)
        try:
            info = os.fstat(stream.fileno())
            self.is_journal = os.environ.get('JOURNAL_STREAM') == f'{info.st_dev}:{info.st_ino}'
        except (AttributeError, OSError, ValueError):
            self.is_journal = False

    def format(self, record):
        rendered = super().format(record)
        return f'<{syslog_severity(record.levelno)}>{rendered}' if self.is_journal else rendered


class RateLimit:
    """Bound repetitive queue/routing diagnostics, reporting suppressed counts."""

    def __init__(self, interval=30):
        self.interval, self.next_due, self.suppressed = interval, 0, 0

    def allow(self):
        now = time.monotonic()
        if now < self.next_due:
            self.suppressed += 1
            return None
        suppressed, self.suppressed = self.suppressed, 0
        self.next_due = now + self.interval
        return suppressed


class RemoteSyslog(logging.Handler):
    """DNS/connect/send only in a daemon worker; application emit never waits."""

    def __init__(self, config, fallback, *, capacity=1024):
        super().__init__()
        self.config, self.fallback = config, fallback
        self.records = queue.Queue(maxsize=capacity)
        self.notices = queue.Queue(maxsize=16)
        self.closing = threading.Event()
        self._notices_closing = threading.Event()
        self.deadline = float('inf')
        self.dropped = 0
        self.failures = 0
        self._last_notice = float('-inf')
        self._notice_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._socket = None
        self._retry_at = 0
        self._closed_once = False
        self._unavailable = False
        self._hostname = re.sub(r'[^\w.-]', '_', socket.gethostname(), flags=re.ASCII)[:255] or '-'
        self.worker = threading.Thread(target=self._run, name='meshbot-syslog', daemon=True)
        self.notice_worker = threading.Thread(target=self._run_notices,
                                              name='meshbot-syslog-notices', daemon=True)
        self.notice_worker.start()
        self.worker.start()

    def _notice(self, name, *, level=logging.WARNING, force=False, **fields):
        # Bypass the remote logger to avoid recursion and keep failures visible
        # even when rsyslog is the only selected application destination.
        with self._notice_lock:
            now = time.monotonic()
            if self.closing.is_set() and now >= self.deadline and not force:
                return
            if not force and now - self._last_notice < 30:
                return
            self._last_notice = now
        with self._stats_lock:
            dropped, failures = self.dropped, self.failures
        record = logging.LogRecord('botcore.logging', level, '', 0, name, (), None)
        record.fields = fields | {'host': self.config.rsyslog_host, 'port': self.config.rsyslog_port,
                                  'protocol': self.config.rsyslog_protocol,
                                  'dropped': dropped, 'failures': failures}
        try:
            self.notices.put_nowait(record)
        except queue.Full:
            pass  # Local diagnostics are best effort too; never block a producer.

    def _run_notices(self):
        while time.monotonic() < self.deadline:
            try:
                record = self.notices.get(timeout=.05)
            except queue.Empty:
                if self._notices_closing.is_set():
                    return
                continue
            try:
                stream = getattr(self.fallback, 'stream', None)
                try:
                    descriptor = stream.fileno()
                except (AttributeError, OSError, ValueError):
                    # In-memory/custom sinks, without acquiring a Handler lock.
                    self.fallback.emit(record)
                else:
                    # Bypass buffered stream/Handler locks: a blocked pipe must
                    # not trap logging.shutdown() flushing the local handler.
                    rendered = self.fallback.format(record) + self.fallback.terminator
                    data = rendered.encode(getattr(stream, 'encoding', None) or 'utf-8',
                                           errors='backslashreplace')
                    while data:
                        written = os.write(descriptor, data)
                        if not written:
                            break
                        data = data[written:]
            except Exception:
                pass  # Reporting a broken local sink must not recurse into logging.
            finally:
                self.notices.task_done()

    def _drop(self, *, failure=False):
        with self._stats_lock:
            self.dropped += 1
            self.failures += int(failure)

    def emit(self, record):
        if self.closing.is_set():
            return
        severity = syslog_severity(record.levelno)
        stamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds')
        # RFC5424 wire envelope; both destinations contain the same readable body.
        header = f'<{8 + severity}>1 {stamp} {self._hostname} meshbot {record.process} - - '
        data = (header + self.format(record)).encode('utf-8')
        if len(data) > 65000:
            data = data[:64950].decode('utf-8', errors='ignore').encode('utf-8') + b' [log record truncated]'
        try:
            self.records.put_nowait(data)
        except queue.Full:
            self._drop()
            self._notice('syslog_queue_full')

    def _connect(self):
        address = (self.config.rsyslog_host, self.config.rsyslog_port)
        if self.config.rsyslog_protocol == 'tcp':
            self._socket = socket.create_connection(address, timeout=.5)
        else:
            addresses = socket.getaddrinfo(*address, type=socket.SOCK_DGRAM)
            family, kind, protocol, _, target = addresses[0]
            self._socket = socket.socket(family, kind, protocol)
            self._socket.settimeout(.5)
            self._socket.connect(target)

    def _disconnect(self):
        if self._socket is not None:
            stream = self._socket
            self._socket = None
            try:
                stream.close()
            except OSError:
                pass

    def _run(self):
        try:
            while time.monotonic() < self.deadline:
                try:
                    data = self.records.get(timeout=.05)
                except queue.Empty:
                    if self.closing.is_set():
                        return
                    continue
                try:
                    if time.monotonic() < self._retry_at:
                        self._drop()
                        continue
                    if self._socket is None:
                        self._connect()
                    if time.monotonic() >= self.deadline:
                        self._drop()
                        return
                    if self.config.rsyslog_protocol == 'tcp':
                        # RFC6587 octet counting: no newline/NUL ambiguity.
                        self._socket.sendall(str(len(data)).encode('ascii') + b' ' + data)
                    else:
                        self._socket.send(data)
                    if self._unavailable:
                        self._unavailable = False
                        self._notice('syslog_recovered', level=logging.INFO, force=True)
                except (OSError, ValueError) as error:
                    self._drop(failure=True)
                    self._unavailable = True
                    self._disconnect()
                    self._retry_at = time.monotonic() + 5
                    self._notice('syslog_unavailable', error_type=type(error).__name__)
                finally:
                    self.records.task_done()
        finally:
            self._disconnect()

    def close(self):
        if self._closed_once:
            return
        self._closed_once = True
        self.deadline = time.monotonic() + 1
        self.closing.set()
        # Reserve a small part of the shared deadline for the final local notice.
        self.worker.join(timeout=max(0, self.deadline - time.monotonic() - .1))
        if self.worker.is_alive() or not self.records.empty():
            self._notice('syslog_shutdown_incomplete', pending=self.records.qsize(), force=True)
        elif self.dropped:
            self._notice('syslog_summary', force=True)
        self._notices_closing.set()
        self.notice_worker.join(timeout=max(0, self.deadline - time.monotonic()))
        super().close()


class LoggingSession:
    def __init__(self, config, stream=None):
        self.closed = False
        self.logger = logging.getLogger('botcore')
        self.previous = (self.logger.handlers[:], self.logger.level, self.logger.propagate)
        self.handlers = []
        self.quiet = []
        formatter = TextFormatter(config.message_content)
        fallback = JournalStream(stream if stream is not None else sys.stderr)
        fallback.setFormatter(formatter)
        if config.output in ('journald', 'both'):
            self.handlers.append(fallback)
        if config.output in ('rsyslog', 'both'):
            remote = RemoteSyslog(config, fallback)
            remote.setFormatter(formatter)
            self.handlers.append(remote)
        self.logger.handlers = self.handlers
        self.logger.setLevel(LEVELS[config.level])
        self.logger.propagate = False
        # SDK DEBUG logs include raw packets and key material. Core emits its
        # own normalized radio diagnostics; do not expose those library dumps.
        for name in ('meshtastic', 'pubsub'):
            logger = logging.getLogger(name)
            self.quiet.append((logger, logger.handlers[:], logger.propagate))
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for handler in self.handlers:
            handler.close()
        self.logger.handlers, level, self.logger.propagate = self.previous
        self.logger.setLevel(level)
        for logger, handlers, propagate in self.quiet:
            logger.handlers, logger.propagate = handlers, propagate
