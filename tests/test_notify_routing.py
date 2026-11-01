import unittest

from telegram.bot import RoutingNotifier, TelegramManager


class Recorder:
    """Notifier double that records every call and can simulate delivery failure."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def _record(self, name, *args):
        self.calls.append((name,) + args)
        return self.result

    async def notify_filled(self, *args):
        return await self._record("filled", *args)

    async def notify_accepted(self, *args):
        return await self._record("accepted", *args)

    async def notify_cancelled(self, *args):
        return await self._record("cancelled", *args)

    async def notify_limit_filled(self, *args):
        return await self._record("limit_filled", *args)

    async def notify_skipped(self, *args):
        return await self._record("skipped", *args)

    async def notify_breakeven(self, *args):
        return await self._record("breakeven", *args)

    async def notify_failed(self, *args):
        return await self._record("failed", *args)

    async def notify_source_failed(self, *args):
        return await self._record("source_failed", *args)

    async def notify_hard_stop(self, *args):
        return await self._record("hard_stop", *args)

    async def notify_hard_stop_error(self, *args):
        return await self._record("hard_stop_error", *args)

    async def notify_hard_stop_recovered(self, *args):
        return await self._record("hard_stop_recovered", *args)


class RoutingNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_trading_noise_stays_on_the_normal_channel(self):
        normal, alerts = Recorder(), Recorder()
        router = RoutingNotifier(normal, alerts)

        await router.notify_filled("L", "signal", "order")
        await router.notify_accepted("L", "signal", "order")
        await router.notify_cancelled("L", "S", {"id": "o1"}, "leader is flat")
        await router.notify_limit_filled("L", "S", {"id": "o1"}, 100.0, 1.0, 0.0)
        await router.notify_skipped("L", "signal", "reason")
        await router.notify_breakeven("L", "BTC", 12.0, 100.0)

        self.assertEqual(
            [call[0] for call in normal.calls],
            ["filled", "accepted", "cancelled", "limit_filled", "skipped", "breakeven"],
        )
        self.assertEqual(alerts.calls, [])

    async def test_critical_events_go_to_the_alert_channel(self):
        normal, alerts = Recorder(), Recorder()
        router = RoutingNotifier(normal, alerts)

        await router.notify_failed("L", "signal", "boom")
        await router.notify_source_failed("L", "down")
        await router.notify_hard_stop(100.0, 120.0, 20.0)
        await router.notify_hard_stop_error(100.0, "close failed")
        await router.notify_hard_stop_recovered(100.0)

        self.assertEqual(
            [call[0] for call in alerts.calls],
            ["failed", "source_failed", "hard_stop", "hard_stop_error", "hard_stop_recovered"],
        )
        self.assertEqual(normal.calls, [])

    async def test_critical_event_falls_back_when_the_alert_channel_fails(self):
        normal, alerts = Recorder(), Recorder(result=False)
        router = RoutingNotifier(normal, alerts)

        delivered = await router.notify_hard_stop(100.0, 120.0, 20.0)

        self.assertTrue(delivered)
        self.assertEqual(alerts.calls, [("hard_stop", 100.0, 120.0, 20.0)])
        self.assertEqual(normal.calls, [("hard_stop", 100.0, 120.0, 20.0)])

    async def test_alerts_can_run_without_a_normal_channel(self):
        alerts = Recorder()
        router = RoutingNotifier(None, alerts)

        self.assertTrue(await router.notify_failed("L", "signal", "boom"))
        self.assertFalse(await router.notify_filled("L", "signal", "order"))
        self.assertEqual(alerts.calls, [("failed", "L", "signal", "boom")])

    async def test_normal_only_configuration_keeps_working(self):
        normal = Recorder()
        router = RoutingNotifier(normal, None)

        await router.notify_failed("L", "signal", "boom")

        self.assertEqual(normal.calls, [("failed", "L", "signal", "boom")])

    async def test_missing_notifiers_do_not_raise(self):
        router = RoutingNotifier(None, None)

        self.assertFalse(await router.notify_filled("L", "signal", "order"))
        self.assertFalse(await router.notify_hard_stop(1.0, 2.0, 3.0))


class FakeResponse:
    def raise_for_status(self):
        return None

    async def json(self):
        return {"ok": True, "result": []}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse()


class TelegramRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_updates_timeout_is_sent_to_the_api_not_the_http_client(self):
        manager = TelegramManager(None, "token", "chat")
        session = FakeSession()

        await manager._request(session, "getUpdates", timeout=25, limit=100)

        self.assertEqual(session.calls[0]["data"], {"timeout": 25, "limit": 100})
        self.assertIsNone(session.calls[0]["timeout"])

    async def test_send_uses_a_short_http_deadline(self):
        manager = TelegramManager(None, "token", "chat")
        session = FakeSession()
        manager.session = session

        self.assertTrue(await manager.send("hello"))

        self.assertIsNotNone(session.calls[0]["timeout"])


if __name__ == "__main__":
    unittest.main()
