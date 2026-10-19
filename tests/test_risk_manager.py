import unittest
from datetime import datetime, timezone

from risk.manager import RiskManager


class RiskManagerTests(unittest.TestCase):
    def test_baseline_resets_at_utc_day_boundary(self):
        first = datetime(2026, 10, 15, 0, 0, 1, tzinfo=timezone.utc)
        next_day = datetime(2026, 10, 16, 0, 0, 1, tzinfo=timezone.utc)
        risk = RiskManager({"max_daily_loss_usdt": 10})
        self.assertFalse(risk.evaluate(100, first))
        self.assertTrue(risk.evaluate(89, first))
        self.assertFalse(risk.evaluate(89, next_day))
        self.assertEqual(risk.baseline, 89.0)

    def test_state_round_trip_keeps_baseline(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        risk = RiskManager({"max_daily_drawdown_pct": 30})
        risk.evaluate(100, now)
        restored = RiskManager(risk.config, risk.state())
        self.assertTrue(restored.evaluate(69, now))

    def test_unlock_rebaselines_and_clears_lock(self):
        now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        risk = RiskManager({"max_daily_loss_usdt": 10})
        risk.evaluate(100, now)
        self.assertTrue(risk.evaluate(85, now))
        risk.unlock(85, now)
        self.assertFalse(risk.locked)
        self.assertEqual(risk.baseline, 85.0)
        self.assertFalse(risk.evaluate(85, now))


if __name__ == "__main__":
    unittest.main()
