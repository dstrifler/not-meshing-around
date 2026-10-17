"""Composition root: configuration, startup, health, and orderly shutdown."""

import argparse
import asyncio
import logging
import signal
import sys
import tomllib
from pathlib import Path

from . import API_VERSION
from .loader import load_plugins
from .observability import LoggingConfig, LoggingSession, event as log_event
from .runtime import CleanupError, Policy, Runtime
from .transport import ConsoleTransport, MeshtasticTransport, validate_radio
from .workers import positive_seconds

log = logging.getLogger(__name__)


def validate_configuration(config):
    LoggingConfig.from_config(config.get('logging', {}))
    core, radio = config.get('core', {}), config.get('radio', {})
    if not isinstance(core, dict) or not isinstance(radio, dict):
        raise ValueError('core and radio must be TOML tables')
    policy = Policy.from_config(config.get('policy', {}))
    radio = validate_radio(radio)
    startup = positive_seconds(core.get('startup_timeout', 30), 'core.startup_timeout')
    shutdown = positive_seconds(core.get('shutdown_timeout', 15), 'core.shutdown_timeout')
    return core, radio, policy, startup, shutdown


async def run(config_path, check=False, *, configuration=None):
    if configuration is None:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    else:
        config = configuration
    root = config_path.resolve().parent
    core, radio, policy, startup_timeout, shutdown_timeout = validate_configuration(config)
    kind = radio["type"]
    transport = ConsoleTransport() if kind == "console" else MeshtasticTransport(radio)
    database = root / core.get("database", "data/core.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(transport, database, policy=policy,
        byte_limit=core.get("message_bytes", 160), send_delay=core.get("send_delay", 0.7),
        send_timeout=core.get("send_timeout", 10))
    log_event(log, logging.INFO, 'bot_starting', api_version=API_VERSION,
              python=sys.version.split()[0], mode='check' if check else 'live', transport=kind)
    log_event(log, logging.INFO, 'configuration_loaded', path=str(config_path.resolve()))
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []

    async def initialize():
        report = await load_plugins(runtime, root / core.get("plugins", "plugins.d"))
        log_event(log, logging.INFO, 'plugins_loaded',
                  active=sum(item['status'] == 'active' for item in report.values()),
                  rejected=sum(item['status'] == 'rejected' for item in report.values()),
                  disabled=sum(item['status'] == 'disabled' for item in report.values()))
        if not any(item["status"] == "active" for item in report.values()):
            raise RuntimeError("no active plugins")
        if not check:
            await transport.start(runtime)

    async def bounded_cleanup(awaitable):
        async with asyncio.timeout(shutdown_timeout):
            await awaitable

    cleanup_failures = {}

    async def cleanup_stage(name, awaitable, *, bounded=True):
        try:
            if bounded:
                await bounded_cleanup(awaitable)
            else:
                await awaitable
        except Exception as error:
            cleanup_failures[name] = error
            log_event(log, logging.ERROR, 'cleanup_failed', stage=name, error=error)

    def request_stop(sig):
        log_event(log, logging.INFO, 'shutdown_requested', reason=sig.name)
        stop.set()

    try:
        # Handle termination while loading plugins or connecting the radio,
        # and keep handlers installed until cleanup has completed.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop, sig)
            installed_signals.append(sig)
        startup = asyncio.create_task(initialize())
        stopping = asyncio.create_task(stop.wait())
        tasks = [startup, stopping]
        done, _ = await asyncio.wait(tasks, timeout=startup_timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stopping in done:
            return
        if startup not in done:
            raise TimeoutError("bot startup timed out")
        startup.result()
        if check or stop.is_set():
            return
        runtime.start()
        log_event(log, logging.INFO, 'bot_ready', transport=kind)
        tasks.extend([asyncio.create_task(transport.run()), asyncio.create_task(runtime.event_loop())])
        done, _ = await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        execution_error = sys.exception()
        log_event(log, logging.INFO, 'bot_stopping',
                  reason='failure' if execution_error else 'check_complete' if check else 'stop_or_eof')
        runtime.quiesce()
        for task in tasks:
            task.cancel()
        try:
            try:
                await cleanup_stage("ingress", transport.stop_receiving())
            finally:
                try:
                    await cleanup_stage("tasks", asyncio.gather(*tasks, return_exceptions=True))
                finally:
                    try:
                        # Runtime bounds job cancellation and each concurrent
                        # hook; an outer deadline would prematurely skip hooks.
                        await cleanup_stage("plugins", runtime.close(timeout=shutdown_timeout), bounded=False)
                    finally:
                        await cleanup_stage("transport", transport.close())
        finally:
            for sig in installed_signals:
                loop.remove_signal_handler(sig)
            log_event(log, logging.INFO, 'transport_final_state', status=transport.health()['status'])
            for name, state in runtime.health().items():
                log_event(log, logging.INFO, 'plugin_final_state', plugin=name,
                          status=state['status'], errors=state['errors'])
            log_event(log, logging.INFO, 'bot_stopped', cleanup_failures=len(cleanup_failures))
        if cleanup_failures:
            if isinstance(execution_error, Exception):
                cleanup_failures["execution"] = execution_error
            raise CleanupError(cleanup_failures)


def main():
    parser = argparse.ArgumentParser(description="Plugin-driven Meshtastic bot")
    parser.add_argument("--config", type=Path, default=Path("bot.toml"))
    parser.add_argument("--check", action="store_true", help="load, initialize, report health, then shut down")
    args = parser.parse_args()
    session = None
    try:
        with args.config.open('rb') as stream:
            config = tomllib.load(stream)
        validate_configuration(config)
        logging_config = LoggingConfig.from_config(config.get('logging', {}))
        session = LoggingSession(logging_config)
        log_event(log, logging.INFO, 'logging_configured', log_level=logging_config.level,
                  output=logging_config.output, message_content=logging_config.message_content,
                  rsyslog_host=logging_config.rsyslog_host if logging_config.output != 'journald' else 'unused',
                  rsyslog_protocol=logging_config.rsyslog_protocol, rsyslog_port=logging_config.rsyslog_port)
        asyncio.run(run(args.config, args.check, configuration=config))
    except Exception as error:
        if session:
            log_event(log, logging.CRITICAL, 'bot_failed', error=error)
        else:
            # Configuration errors occur before the selected destination is
            # usable. A local, readable diagnostic is always available.
            from .observability import TextFormatter
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(TextFormatter())
            record = logging.LogRecord('botcore.controller', logging.ERROR, '', 0,
                                       'configuration_failed', (), None)
            record.fields = {'error_type': type(error).__name__}
            if isinstance(error, (ValueError, OSError)):
                record.fields['reason'] = str(error)
            handler.handle(record)
        raise SystemExit(1)
    finally:
        if session:
            session.close()
