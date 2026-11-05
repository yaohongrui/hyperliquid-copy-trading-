import argparse
import re

from config.loader import load
from core.symbols import normalize

_PLACEHOLDER = re.compile(r"\$\{[^}]+\}")
_DEFAULT_SOURCE = "hyperliquid"


def _validate_symbol_map(value):
    if value is None:
        return
    assert isinstance(value, dict), "symbol_map must be a mapping"
    for key, target in value.items():
        assert isinstance(key, str) and key.strip(), "symbol_map keys must be non-empty strings"
        assert normalize(key), f"symbol_map key has no base asset: {key!r}"
        assert target is None or isinstance(target, str), f"symbol_map[{key!r}] must be a string or null"


def _validate_telegram(value):
    if value is None:
        return
    assert isinstance(value, dict), "telegram must be a mapping"
    enabled = value.get("enabled", False)
    if enabled:
        for key in ("bot_token", "chat_id"):
            item = str(value.get(key) or "").strip()
            assert item and not _PLACEHOLDER.search(item), (
                f"telegram.{key} is required when telegram is enabled"
            )
    alerts = value.get("alerts")
    if alerts is None:
        return
    assert isinstance(alerts, dict), "telegram.alerts must be a mapping"
    if alerts.get("enabled", False):
        for key in ("bot_token", "chat_id"):
            item = str(alerts.get(key) or "").strip()
            assert item and not _PLACEHOLDER.search(item), (
                f"telegram.alerts.{key} is required when alerts are enabled"
            )


def _validate_auto_breakeven(value, label):
    if value is None:
        return
    assert isinstance(value, dict), f"{label} must be a mapping"
    if "enabled" in value:
        assert isinstance(value["enabled"], bool), f"{label}.enabled must be a boolean"
    if "trigger_usdt" in value:
        trigger = value["trigger_usdt"]
        assert isinstance(trigger, (int, float)) and not isinstance(trigger, bool), (
            f"{label}.trigger_usdt must be a number"
        )
        assert trigger > 0, f"{label}.trigger_usdt must be positive"


def _validate_auto_breakeven_effective(c):
    base = c.get("auto_breakeven")
    _validate_auto_breakeven(base, "auto_breakeven")
    base = base or {}
    for index, leader in enumerate(c.get("leaders") or []):
        override = leader.get("auto_breakeven")
        label = f"leaders[{index}].auto_breakeven"
        _validate_auto_breakeven(override, label)
        merged = {**base, **(override or {})}
        if merged.get("enabled", False):
            trigger = merged.get("trigger_usdt")
            assert isinstance(trigger, (int, float)) and not isinstance(trigger, bool) and trigger > 0, (
                f"{label}: trigger_usdt must be positive when auto breakeven is enabled"
            )


def _leader_address(leader, label):
    address = leader.get("address")
    if isinstance(address, int):
        address = hex(address)
    text = str(address or "").strip()
    assert text, f"{label}: address is required for hyperliquid leaders"
    assert text.lower().startswith("0x") and len(text) == 42, (
        f"{label}: address must be a 42-character 0x address"
    )


def _validate_leaders(c, accounts):
    leaders = c.get("leaders") or []
    names = set()
    enabled_accounts = set()
    for index, leader in enumerate(leaders):
        assert isinstance(leader, dict), f"leaders[{index}] must be a mapping"
        name = str(leader.get("name") or "").strip()
        label = name or f"leaders[{index}]"
        assert name, f"leaders[{index}].name is required"
        assert name not in names, f"duplicate leader name: {name}"
        names.add(name)

        source = str(leader.get("source") or _DEFAULT_SOURCE)
        assert source == _DEFAULT_SOURCE, f"{label}: unknown source {source}"
        _leader_address(leader, label)

        account = leader.get("execution_account")
        assert account in accounts, f"unknown execution account: {account}"
        if leader.get("enabled", True):
            enabled_accounts.add(account)

    assert len(enabled_accounts) <= 1, (
        "all enabled leaders must use the same execution_account"
    )


def validate(path):
    c = load(path)
    assert isinstance(c, dict)
    assert c.get('leaders') is not None
    assert c.get('execution_accounts') is not None
    _validate_symbol_map(c.get('symbol_map'))
    _validate_telegram(c.get('telegram'))
    _validate_leaders(c, set(c['execution_accounts']))
    _validate_auto_breakeven_effective(c)
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('config')
    validate(p.parse_args().config)
    print('valid')
