import copy
import os
import tempfile
import unittest
from unittest import mock

import yaml

from validate import (
    _validate_auto_breakeven_effective,
    _validate_telegram,
    validate,
)

_KEY_ENV = "HYPER_TEST_HL_KEY"
_MISSING_ENV = "HYPER_TEST_DEFINITELY_UNSET_KEY"
_API_KEY = "0x" + "ab" * 32
_ADDRESS = "0xd38272Da1648a212559a09FB7083097f872A3Dd5"


def base_config():
    return {
        "hyperliquid": {
            "ws_url": "wss://api.hyperliquid.xyz/ws",
            "reconnect_seconds": 3,
        },
        "execution_accounts": {
            "hl_primary": {
                "exchange": "hyperliquid",
                "account_address": _ADDRESS,
                "api_private_key": f"${{{_KEY_ENV}}}",
                "testnet": False,
                "leverage": 20,
                "dry_run": False,
                "hard_stop": {"max_daily_drawdown_pct": 30},
                "fixed_balance_usdt": 80,
                "request_timeout_seconds": 10,
            }
        },
        "stop_out_cooldown_seconds": 600,
        "console": {
            "database": "data/console.sqlite3",
            "domain": "yhr.lol",
            "leader_rest_seconds": 60,
            "performance_seconds": 60,
            "performance_lookback_days": 30,
            "performance_overlap_seconds": 300,
            "performance_page_size": 2000,
            "snapshot_retention_days": 90,
        },
        "auto_breakeven": {"enabled": False},
        "leaders": [
            {
                "name": "A",
                "address": "0x15baf1cefcced43e971da85b7b4de5a0391f7b29",
                "source": "hyperliquid",
                "execution_account": "hl_primary",
                "coefficient": 5,
                "enabled": True,
                "symbol_blacklist": [],
                "late_entry_enabled": True,
                "late_entry_offset_pct": 0.2,
                "position_stop_loss": {"enabled": False, "max_loss_usdt": 10},
                "total_margin": 400000,
            }
        ],
        "state": {"path": "data/state.json"},
        "telegram": {
            "enabled": False,
            "bot_token": "${TELEGRAM_GATE_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_GATE_CHAT_ID}",
            "polling_timeout": 25,
            "report_format": "image",
        },
    }


class ValidateConfigTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop(_MISSING_ENV, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, config):
        path = os.path.join(self.tmp.name, "config.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
        return path

    def check(self, config):
        with mock.patch.dict(os.environ, {_KEY_ENV: _API_KEY}):
            return validate(self.write(config))

    def test_valid_config_passes(self):
        self.assertTrue(self.check(base_config()))

    def test_non_hyperliquid_exchange_is_rejected(self):
        config = base_config()
        config["execution_accounts"]["hl_primary"]["exchange"] = "gate"
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_runtime_account_requires_private_key(self):
        config = base_config()
        del config["execution_accounts"]["hl_primary"]["api_private_key"]
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_unexpanded_private_key_is_rejected(self):
        config = base_config()
        config["execution_accounts"]["hl_primary"]["api_private_key"] = f"${{{_MISSING_ENV}}}"
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_runtime_account_requires_valid_address(self):
        for address in ("", "0x1234", "1234", "0x" + "z" * 40):
            config = base_config()
            config["execution_accounts"]["hl_primary"]["account_address"] = address
            with self.subTest(address=address):
                with self.assertRaises(AssertionError):
                    self.check(config)

    def test_missing_address_is_rejected_for_runtime_account(self):
        config = base_config()
        del config["execution_accounts"]["hl_primary"]["account_address"]
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_non_positive_leverage_is_rejected(self):
        for leverage in (0, -1, True, "20", None):
            config = base_config()
            config["execution_accounts"]["hl_primary"]["leverage"] = leverage
            with self.subTest(leverage=leverage):
                with self.assertRaises(AssertionError):
                    self.check(config)

    def test_non_positive_fixed_balance_is_rejected(self):
        for balance in (0, -5, "80", True):
            config = base_config()
            config["execution_accounts"]["hl_primary"]["fixed_balance_usdt"] = balance
            with self.subTest(balance=balance):
                with self.assertRaises(AssertionError):
                    self.check(config)

    def test_hard_stop_must_be_a_mapping(self):
        config = base_config()
        config["execution_accounts"]["hl_primary"]["hard_stop"] = 30
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_unknown_leader_account_is_rejected(self):
        config = base_config()
        config["leaders"][0]["execution_account"] = "other_primary"
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_unknown_leader_source_is_rejected(self):
        config = base_config()
        config["leaders"][0]["source"] = "other"
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_bad_leader_address_is_rejected(self):
        config = base_config()
        config["leaders"][0]["address"] = "0x1234"
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_dry_run_account_may_omit_key_and_address(self):
        config = base_config()
        account = config["execution_accounts"]["hl_primary"]
        account["dry_run"] = True
        del account["api_private_key"]
        del account["account_address"]
        self.assertTrue(self.check(config))

    def test_enabled_leaders_must_share_one_account(self):
        config = base_config()
        config["execution_accounts"]["hl_secondary"] = copy.deepcopy(
            config["execution_accounts"]["hl_primary"])
        second = copy.deepcopy(config["leaders"][0])
        second["name"] = "B"
        second["execution_account"] = "hl_secondary"
        config["leaders"].append(second)
        with self.assertRaises(AssertionError):
            self.check(config)

    def test_mirror_orders_valid_shape_passes(self):
        config = base_config()
        config["mirror_orders"] = {
            "enabled": True, "max_orders": 2, "credit_grace_ms": 300,
            "orphan_ttl_seconds": 300, "max_orphans": 2,
        }
        self.assertTrue(self.check(config))

    def test_mirror_orders_max_orders_is_one_or_two(self):
        for count in (1, 2):
            config = base_config()
            config["mirror_orders"] = {"max_orders": count}
            self.assertTrue(self.check(config))
        for count in (0, 3, True, "2"):
            config = base_config()
            config["mirror_orders"] = {"max_orders": count}
            with self.assertRaises(AssertionError):
                self.check(config)

    def test_mirror_orders_orphan_settings_are_validated(self):
        for value in (0, -1, "300", True):
            config = base_config()
            config["mirror_orders"] = {"orphan_ttl_seconds": value}
            with self.assertRaises(AssertionError):
                self.check(config)
        for value in (-1, "2", True):
            config = base_config()
            config["mirror_orders"] = {"max_orphans": value}
            with self.assertRaises(AssertionError):
                self.check(config)
        config = base_config()
        config["mirror_orders"] = {"orphan_ttl_seconds": 300, "max_orphans": 0}
        self.assertTrue(self.check(config))


class ValidateAutoBreakevenTests(unittest.TestCase):
    def config(self, global_value=None, leader_value=None):
        leader = {"name": "L", "execution_account": "acc"}
        if leader_value is not None:
            leader["auto_breakeven"] = leader_value
        return {"auto_breakeven": global_value, "leaders": [leader]}

    def test_absent_is_valid(self):
        _validate_auto_breakeven_effective(self.config())

    def test_global_default_with_trigger_is_valid(self):
        _validate_auto_breakeven_effective(
            self.config({"enabled": True, "trigger_usdt": 15}))

    def test_enabled_without_trigger_is_rejected(self):
        with self.assertRaises(AssertionError):
            _validate_auto_breakeven_effective(self.config({"enabled": True}))
        with self.assertRaises(AssertionError):
            _validate_auto_breakeven_effective(
                self.config(None, {"enabled": True}))

    def test_leader_override_can_supply_the_trigger(self):
        _validate_auto_breakeven_effective(
            self.config({"enabled": True}, {"trigger_usdt": 5}))

    def test_leader_can_disable_the_global_default(self):
        _validate_auto_breakeven_effective(
            self.config({"enabled": True, "trigger_usdt": 15}, {"enabled": False}))

    def test_invalid_shapes_are_rejected(self):
        for value in ([], "bad", 1):
            with self.assertRaises(AssertionError):
                _validate_auto_breakeven_effective(self.config(value))
        for trigger in (0, -1, "15", None, True):
            with self.assertRaises(AssertionError):
                _validate_auto_breakeven_effective(
                    self.config({"enabled": True, "trigger_usdt": trigger}))
        with self.assertRaises(AssertionError):
            _validate_auto_breakeven_effective(
                self.config({"enabled": "yes", "trigger_usdt": 15}))


class ValidateTelegramTests(unittest.TestCase):
    def test_absent_is_valid(self):
        _validate_telegram(None)

    def test_enabled_requires_resolved_credentials(self):
        _validate_telegram({
            "enabled": True,
            "bot_token": "123:abc",
            "chat_id": "-1001",
        })
        with self.assertRaises(AssertionError):
            _validate_telegram({
                "enabled": True,
                "bot_token": "${TELEGRAM_GATE_BOT_TOKEN}",
                "chat_id": "-1001",
            })
        with self.assertRaises(AssertionError):
            _validate_telegram({"enabled": True, "bot_token": "123:abc"})

    def test_disabled_placeholders_are_allowed(self):
        _validate_telegram({
            "enabled": False,
            "bot_token": "${TELEGRAM_GATE_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_GATE_CHAT_ID}",
        })

    def test_alerts_block_is_validated(self):
        _validate_telegram({"alerts": {"enabled": False,
                                       "bot_token": "${X}", "chat_id": "${Y}"}})
        _validate_telegram({"alerts": {"enabled": True,
                                       "bot_token": "9:z", "chat_id": "-1002"}})
        with self.assertRaises(AssertionError):
            _validate_telegram({"alerts": {"enabled": True,
                                           "bot_token": "${X}", "chat_id": "${Y}"}})
        with self.assertRaises(AssertionError):
            _validate_telegram({"alerts": "bad"})


if __name__ == "__main__":
    unittest.main()
