import unittest

from validate import _validate_leaders


class LeaderValidationTests(unittest.TestCase):
    def _validate(self, leaders, accounts=None):
        _validate_leaders(
            {"leaders": leaders},
            set(accounts or {"binance_primary"}),
        )

    def test_hyperliquid_leader_accepts_hex_and_integer_addresses(self):
        self._validate([
            {"name": "a", "execution_account": "binance_primary",
             "address": "0x" + "1" * 40},
            {"name": "b", "execution_account": "binance_primary",
             "address": 0x15baf1cefcced43e971da85b7b4de5a0391f7b29},
        ])

    def test_hyperliquid_leader_requires_address(self):
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary"}])
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary",
                             "address": "0x123"}])

    def test_non_hyperliquid_source_is_rejected(self):
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary",
                             "source": "live", "address": "0x" + "1" * 40}])

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(AssertionError):
            self._validate([
                {"name": "a", "execution_account": "binance_primary",
                 "address": "0x" + "1" * 40},
                {"name": "a", "execution_account": "binance_primary",
                 "address": "0x" + "2" * 40},
            ])

    def test_mixed_enabled_accounts_are_rejected(self):
        with self.assertRaises(AssertionError):
            self._validate([
                {"name": "a", "execution_account": "binance_primary",
                 "address": "0x" + "1" * 40},
                {"name": "b", "execution_account": "gate_primary",
                 "address": "0x" + "2" * 40},
            ], accounts={"binance_primary", "gate_primary"})

    def test_disabled_leaders_may_use_another_account(self):
        self._validate([
            {"name": "a", "execution_account": "binance_primary",
             "address": "0x" + "1" * 40},
            {"name": "b", "execution_account": "gate_primary",
             "address": "0x" + "2" * 40, "enabled": False},
        ], accounts={"binance_primary", "gate_primary"})


if __name__ == "__main__":
    unittest.main()
