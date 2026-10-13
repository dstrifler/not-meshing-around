"""Await blocking SDK calls without tying process exit to stuck executor threads."""

import asyncio
import math
import threading


def positive_seconds(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return value


async def blocking_call(function, *args, **kwargs):
    """Cancellation abandons the await, never starts a replacement SDK call.

    The worker is a daemon because Python cannot forcibly cancel a thread.
    Transport code owns deadlines, I/O interruption, and late resource cleanup.
    """
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def finish(value, error):
        if result.done():
            return
        if error is None:
            result.set_result(value)
        else:
            result.set_exception(error)

    def work():
        value, error = None, None
        try:
            value = function(*args, **kwargs)
        except BaseException as caught:
            error = caught if isinstance(caught, Exception) else RuntimeError(str(caught))
        try:
            loop.call_soon_threadsafe(finish, value, error)
        except RuntimeError:
            # A late completion after the event loop closes has no awaiting user.
            pass

    threading.Thread(target=work, name="bot-radio-sdk", daemon=True).start()
    return await result
