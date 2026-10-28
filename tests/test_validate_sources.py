import unittest

from validate import _validate_leaders, _validate_position_sources


class PositionSourceValidationTests(unittest.TestCase):
    def test_absent_is_valid(self):
        _validate_position_sources(None)
        _validate_position_sources({})

    def test_non_mapping_is_rejected(self):
        for value in ([], "bad", 1):
            with self.assertRaises(AssertionError):
                _validate_position_sources(value)

    def test_reserved_key_is_rejected(self):
        with self.assertRaises(AssertionError):
            _validate_position_sources({"hyperliquid": {"type": "binance"}})

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(AssertionError):
            _validate_position_sources({"b": {"type": "gate"}})

    def test_hybrid_requires_probe(self):
        with self.assertRaises(AssertionError):
            _validate_position_sources({
                "b": {"type": "binance", "mode": "hybrid", "browser": {"profile_dir": "p"}}
            })
        with self.assertRaises(AssertionError):
            _validate_position_sources({
                "b": {
                    "type": "binance", "mode": "hybrid",
                    "browser": {"profile_dir": "p"},
                    "session": {"auth_probe_portfolio_id": "${PROBE}"},
                }
            })

    def test_http_requires_cookie(self):
        with self.assertRaises(AssertionError):
            _validate_position_sources({"b": {"type": "binance", "mode": "http"}})

    def test_valid_hybrid(self):
        _validate_position_sources({
            "b": {
                "type": "binance",
                "mode": "hybrid",
                "browser": {"profile_dir": "data/profile"},
                "session": {"auth_probe_portfolio_id": "123"},
            }
        })


class LeaderValidationTests(unittest.TestCase):
    def _validate(self, leaders, sources=None, accounts=None):
        _validate_leaders(
            {"leaders": leaders},
            set(sources or {}),
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

    def test_binance_leader_requires_known_source_and_portfolio(self):
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary",
                             "source": "missing", "portfolio_id": "1"}])
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary",
                             "source": "live"}], sources={"live": {}})
        with self.assertRaises(AssertionError):
            self._validate([{"name": "a", "execution_account": "binance_primary",
                             "source": "live", "portfolio_id": "${ID}"}], sources={"live": {}})

    def test_valid_binance_leader(self):
        self._validate([{"name": "a", "execution_account": "binance_primary",
                         "source": "live", "portfolio_id": "123"}], sources={"live": {}})

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
