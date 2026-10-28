import asyncio
import unittest

from main import _hyperliquid_addresses, _retry_sources, _start_sources


class FakeSource:
    def __init__(self, failures=0):
        self.failures = failures
        self.started = 0

    async def start(self):
        self.started += 1
        if self.started <= self.failures:
            raise RuntimeError("unavailable")


class HyperliquidAddressTests(unittest.TestCase):
    def test_only_enabled_leaders_and_integer_addresses(self):
        leaders = {
            "a": {"enabled": True, "address": 123},
            "b": {"enabled": False, "address": "0xbeef"},
            "c": {"enabled": True, "address": "0xcafe"},
        }
        self.assertEqual(_hyperliquid_addresses(leaders), ["0x7b", "0xcafe"])


class StartSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_failures_are_reported_not_raised(self):
        ok, bad = FakeSource(), FakeSource(failures=1)
        failed = await _start_sources({"ok": ok, "bad": bad})
        self.assertEqual(failed, {"bad"})
        self.assertEqual(ok.started, 1)
        self.assertEqual(bad.started, 1)

    async def test_retry_stops_after_success(self):
        source = FakeSource(failures=1)
        stop = asyncio.Event()
        await asyncio.wait_for(
            _retry_sources({"b": source}, {"b"}, stop, interval=0.01), timeout=1
        )
        self.assertEqual(source.started, 2)

    async def test_retry_exits_when_stopped(self):
        source = FakeSource(failures=1000)
        stop = asyncio.Event()
        task = asyncio.create_task(
            _retry_sources({"b": source}, {"b"}, stop, interval=0.01)
        )
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertLess(source.started, 1000)


if __name__ == "__main__":
    unittest.main()
