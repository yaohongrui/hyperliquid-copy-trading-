import unittest

from validate import (
    _validate_auto_breakeven_effective,
    _validate_symbol_map,
    _validate_telegram,
)


class ValidateSymbolMapTests(unittest.TestCase):
    def test_absent_or_empty_is_valid(self):
        _validate_symbol_map(None)
        _validate_symbol_map({})

    def test_valid_forms(self):
        _validate_symbol_map({
            "AMD": "AMD/USDT:USDT",
            "SKHX": None,
            "FOO": "skip",
            "BAR": "BAR_USDT",
        })

    def test_non_mapping_is_rejected(self):
        for value in ([], "bad", 1):
            with self.assertRaises(AssertionError):
                _validate_symbol_map(value)

    def test_invalid_keys_are_rejected(self):
        for key in (1, "", "   ", ":"):
            with self.assertRaises(AssertionError):
                _validate_symbol_map({key: "AMD"})

    def test_invalid_values_are_rejected(self):
        for value in (1, [], {}):
            with self.assertRaises(AssertionError):
                _validate_symbol_map({"AMD": value})


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
