import asyncio
import contextlib
import unittest
from types import SimpleNamespace

from botcore.api import Command, Event, Job, Message, Registration, Response, Subscription
from botcore.runtime import Runtime


async def cancel_handler(mode):
    if mode == "child":
        child = asyncio.create_task(asyncio.sleep(3600))
        child.cancel()
        await child
    elif mode == "self":
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
    else:
        raise asyncio.CancelledError("handler cancelled")


class HandlerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []

        async def send(*args):
            self.sent.append(args[0])

        self.runtime = Runtime(SimpleNamespace(send=send), ":memory:", send_delay=0)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_command_cancellation_is_reported_and_later_commands_reply(self):
        async def command(context):
            if context.arguments == "ok":
                return Response("ok")
            await cancel_handler(context.arguments)

        await self.runtime.activate("demo", Registration(commands=(Command("demo", command),)))
        for index, mode in enumerate(("child", "self", "raise"), 1):
            with self.subTest(mode=mode), self.assertLogs("botcore.runtime", level="ERROR"):
                dispatch = asyncio.create_task(self.runtime.dispatch(Message(f"demo {mode}", 1, direct=True)))
                try:
                    self.assertTrue(await asyncio.wait_for(dispatch, 10))
                finally:
                    dispatch.cancel()
                    await asyncio.gather(dispatch, return_exceptions=True)
                self.assertEqual(self.runtime.health()["demo"]["errors"], index)
                self.assertEqual(self.runtime.health()["demo"]["last_error"], "plugin handler cancelled")
            self.assertTrue(await self.runtime.dispatch(Message("demo ok", 1, direct=True)))
        self.assertEqual(self.sent, ["ok", "ok", "ok"])

    async def test_event_cancellation_keeps_other_subscribers_and_dispatcher_running(self):
        observed = asyncio.Queue()

        async def broken(event, services):
            await cancel_handler(event.data["mode"])

        async def observer(event, services):
            observed.put_nowait(event.data["mode"])

        await self.runtime.activate("broken", Registration(events=(Subscription("demo", broken),)))
        await self.runtime.activate("observer", Registration(events=(Subscription("demo", observer),)))
        consumer = asyncio.create_task(self.runtime.event_loop())
        try:
            for index, mode in enumerate(("child", "self", "raise"), 1):
                with self.subTest(mode=mode), self.assertLogs("botcore.runtime", level="ERROR"):
                    received = asyncio.create_task(observed.get())
                    try:
                        self.runtime.enqueue_event(Event("demo", {"mode": mode}))
                        done, _ = await asyncio.wait([consumer, received], timeout=10,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        self.assertFalse(consumer.done(), "handler cancelled the core event dispatcher")
                        self.assertIn(received, done)
                        self.assertEqual(received.result(), mode)
                        self.assertEqual(self.runtime.health()["broken"]["errors"], index)
                    finally:
                        received.cancel()
                        await asyncio.gather(received, return_exceptions=True)
            self.assertEqual(self.runtime.health()["observer"]["errors"], 0)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_job_cancellation_is_reported_and_later_intervals_still_run(self):
        modes = iter(("child", "self", "raise"))
        recovered = asyncio.Event()

        async def job(services):
            mode = next(modes, None)
            if mode:
                await cancel_handler(mode)
            else:
                recovered.set()
                await asyncio.Event().wait()

        await self.runtime.activate("timer", Registration(jobs=(Job("tick", .001, job),)))
        with self.assertLogs("botcore.runtime", level="ERROR"):
            self.runtime.start()
            worker = self.runtime.plugins["timer"].tasks[0]
            ready = asyncio.create_task(recovered.wait())
            try:
                done, _ = await asyncio.wait([worker, ready], timeout=10,
                                             return_when=asyncio.FIRST_COMPLETED)
                self.assertFalse(worker.done(), "handler cancelled the core job runner")
                self.assertIn(ready, done)
                self.assertEqual(self.runtime.health()["timer"]["errors"], 3)
                self.assertEqual(self.runtime.health()["timer"]["status"], "active")
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
        await self.runtime.close()
        self.assertTrue(worker.cancelled())
        self.assertEqual(self.runtime.health()["timer"]["errors"], 3)

    async def assert_management_cancellation(self, kind):
        self.runtime.start()
        for finish in ("propagate", "return", "raise"):
            with self.subTest(kind=kind, finish=finish):
                entered, cleaned = asyncio.Event(), asyncio.Event()
                cancellations = []

                async def handler(*args):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancellations.append(True)
                        await asyncio.sleep(0)
                        if len(cancellations) == 1:
                            if finish == "return":
                                return Response("must not send") if kind == "command" else None
                            if finish == "raise":
                                raise ValueError("handler cleanup failed")
                        raise
                    finally:
                        cleaned.set()

                name = f"{kind}_{finish}"
                registration = {
                    "command": Registration(commands=(Command(name, handler),)),
                    "event": Registration(events=(Subscription(name, handler),)),
                    "job": Registration(jobs=(Job(name, .001, handler),)),
                }[kind]
                await self.runtime.activate(name, registration)
                if kind == "command":
                    task = asyncio.create_task(self.runtime.dispatch(Message(name, 1, direct=True)))
                elif kind == "event":
                    task = asyncio.create_task(self.runtime.event_loop())
                    self.runtime.enqueue_event(Event(name))
                else:
                    task = self.runtime.plugins[name].tasks[0]
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    logs = self.assertLogs("botcore.runtime", level="ERROR") if finish == "raise" else contextlib.nullcontext()
                    with logs:
                        task.cancel()
                        done, _ = await asyncio.wait([task], timeout=10)
                    self.assertIn(task, done, "management cancellation did not stop core work")
                    self.assertTrue(task.cancelled())
                    self.assertTrue(cleaned.is_set())
                    self.assertEqual(cancellations, [True])
                    self.assertEqual(self.runtime.health()[name]["errors"], 1 if finish == "raise" else 0)
                    if finish == "raise":
                        self.assertEqual(self.runtime.health()[name]["last_error"], "handler cleanup failed")
                    self.assertEqual(self.sent, [])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_management_can_cancel_a_command(self):
        await self.assert_management_cancellation("command")

    async def test_management_can_cancel_the_event_dispatcher(self):
        await self.assert_management_cancellation("event")

    async def test_management_can_cancel_a_job_runner(self):
        await self.assert_management_cancellation("job")
