"""Composition root: configuration, startup, health, and orderly shutdown."""

import argparse
import asyncio
import json
import logging
import signal
import tomllib
from pathlib import Path

from .loader import load_plugins
from .runtime import Policy, Runtime
from .transport import ConsoleTransport, MeshtasticTransport
from .workers import positive_seconds


async def run(config_path, check=False):
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    root = config_path.resolve().parent
    core, radio = config.get("core", {}), config.get("radio", {})
    if not isinstance(core, dict) or not isinstance(radio, dict):
        raise ValueError("core and radio must be TOML tables")
    policy = Policy.from_config(config.get("policy", {}))
    startup_timeout = positive_seconds(core.get("startup_timeout", 30), "core.startup_timeout")
    shutdown_timeout = positive_seconds(core.get("shutdown_timeout", 15), "core.shutdown_timeout")
    kind = radio.get("type", "console")
    if kind not in ("console", "tcp", "serial"):
        raise ValueError("radio.type must be console, tcp, or serial")
    transport = ConsoleTransport() if kind == "console" else MeshtasticTransport(radio)
    database = root / core.get("database", "data/core.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(transport, database, policy=policy,
        byte_limit=core.get("message_bytes", 160), send_delay=core.get("send_delay", 0.7),
        send_timeout=core.get("send_timeout", 10))
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []

    async def initialize():
        report = await load_plugins(runtime, root / core.get("plugins", "plugins.d"))
        print(json.dumps({"plugins": report}, indent=2), flush=True)
        if not any(item["status"] == "active" for item in report.values()):
            raise RuntimeError("no active plugins")
        if not check:
            await transport.start(runtime)

    async def bounded_cleanup(awaitable):
        async with asyncio.timeout(shutdown_timeout):
            await awaitable

    try:
        # Handle termination while loading plugins or connecting the radio,
        # and keep handlers installed until cleanup has completed.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
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
        print(json.dumps({"transport": transport.health()}), flush=True)
        tasks.extend([asyncio.create_task(transport.run()), asyncio.create_task(runtime.event_loop())])
        done, _ = await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        runtime.quiesce()
        for task in tasks:
            task.cancel()
        try:
            try:
                await bounded_cleanup(transport.stop_receiving())
            finally:
                try:
                    await bounded_cleanup(asyncio.gather(*tasks, return_exceptions=True))
                finally:
                    try:
                        await bounded_cleanup(runtime.close())
                    finally:
                        await bounded_cleanup(transport.close())
        finally:
            for sig in installed_signals:
                loop.remove_signal_handler(sig)
            print(json.dumps({"transport": transport.health(), "plugins": runtime.health()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Plugin-driven Meshtastic bot")
    parser.add_argument("--config", type=Path, default=Path("bot.toml"))
    parser.add_argument("--check", action="store_true", help="load, initialize, report health, then shut down")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(args.config, args.check))
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as error:
        parser.exit(1, f"Bot failed: {error}\n")
