import asyncio
import time
import unittest

from main import _ws_watchdog


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_warns_once_when_the_stream_goes_silent(self):
        watch = {"last_event": time.monotonic() - 10}
        task = asyncio.create_task(_ws_watchdog(watch, silence_seconds=1, interval=0.01))
        try:
            with self.assertLogs("hyper", level="WARNING") as captured:
                await asyncio.sleep(0.08)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        warnings = [line for line in captured.output if "no events" in line]
        self.assertEqual(len(warnings), 1)

    async def test_does_not_warn_while_events_keep_arriving(self):
        watch = {"last_event": time.monotonic()}
        task = asyncio.create_task(_ws_watchdog(watch, silence_seconds=5, interval=0.01))
        try:
            with self.assertNoLogs("hyper", level="WARNING"):
                for _ in range(8):
                    watch["last_event"] = time.monotonic()
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
