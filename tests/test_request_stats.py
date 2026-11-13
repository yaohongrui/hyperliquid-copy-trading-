import unittest

from core import request_stats


class RequestStatsTests(unittest.TestCase):
    def tearDown(self):
        request_stats.WINDOW = 60.0
        request_stats.reset()

    def test_logs_counts_by_kind(self):
        request_stats.WINDOW = 0.0
        request_stats.reset()
        with self.assertLogs("core.request_stats", "INFO") as captured:
            request_stats.record("user_state")
            request_stats.record("frontendOpenOrders")
        text = "\n".join(captured.output)
        self.assertIn("REQUESTS", text)
        self.assertIn("user_state=1", text)
        self.assertIn("frontendOpenOrders=1", text)


if __name__ == "__main__":
    unittest.main()
