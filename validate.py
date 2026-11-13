import argparse
import re

from config.loader import load

_PLACEHOLDER = re.compile(r"\$\{[^}]+\}")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_DEFAULT_SOURCE = "hyperliquid"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_number(value):
    return _is_number(value) and value > 0


def _address_text(value):
    """Normalize an address that PyYAML may have parsed as an integer."""
    if isinstance(value, int):
        return f"0x{value:040x}"
    return str(value or "").strip()


def _is_address(text):
    return bool(_ADDRESS.fullmatch(text))


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


def _validate_execution_accounts(accounts):
    """Validate the Hyperliquid-only execution accounts."""
    assert isinstance(accounts, dict) and accounts, (
        "execution_accounts must be a non-empty mapping"
    )
    for name, account in accounts.items():
        label = f"execution_accounts.{name}"
        assert isinstance(account, dict), f"{label} must be a mapping"
        assert account.get("exchange") == "hyperliquid", (
            f"{label}.exchange must be 'hyperliquid'"
        )
        assert _positive_number(account.get("leverage")), (
            f"{label}.leverage must be positive"
        )
        fixed = account.get("fixed_balance_usdt")
        if fixed is not None:
            assert _positive_number(fixed), (
                f"{label}.fixed_balance_usdt must be positive"
            )
        hard_stop = account.get("hard_stop")
        if hard_stop is not None:
            assert isinstance(hard_stop, dict), f"{label}.hard_stop must be a mapping"

        address = _address_text(account.get("account_address"))
        if address:
            assert _is_address(address), (
                f"{label}.account_address must be a 42-character 0x address"
            )
        if not account.get("dry_run", False):
            key = str(account.get("api_private_key") or "").strip()
            assert key and not _PLACEHOLDER.search(key), (
                f"{label}.api_private_key is required when dry_run is false"
            )
            assert address and _is_address(address), (
                f"{label}.account_address is required when dry_run is false"
            )


def _leader_address(leader, label):
    text = _address_text(leader.get("address"))
    assert text, f"{label}: address is required for hyperliquid leaders"
    assert _is_address(text), f"{label}: address must be a 42-character 0x address"


def _validate_mirror_orders(value):
    if value is None:
        return
    assert isinstance(value, dict), "mirror_orders must be a mapping"
    for key in ("enabled", "post_only", "shadow"):
        if key in value:
            assert isinstance(value[key], bool), f"mirror_orders.{key} must be a boolean"
    if "max_orders" in value:
        count = value["max_orders"]
        assert (isinstance(count, int) and not isinstance(count, bool) and count >= 1), (
            "mirror_orders.max_orders must be a positive integer"
        )
    for key in ("refresh_seconds", "leader_orders_seconds", "dex_probe_seconds",
                "failure_cooldown_seconds", "credit_ttl_seconds",
                "orphan_ttl_seconds"):
        if key in value:
            assert _positive_number(value[key]), f"mirror_orders.{key} must be positive"
    if "max_orphans" in value:
        count = value["max_orphans"]
        assert (isinstance(count, int) and not isinstance(count, bool) and count >= 0), (
            "mirror_orders.max_orphans must be a non-negative integer"
        )
    if "credit_grace_ms" in value:
        grace = value["credit_grace_ms"]
        assert _is_number(grace) and grace >= 0, (
            "mirror_orders.credit_grace_ms must be non-negative"
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

        if "mirror_orders" in leader:
            assert isinstance(leader["mirror_orders"], bool), (
                f"{label}.mirror_orders must be a boolean"
            )

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
    assert c.get("leaders") is not None, "leaders is required"
    assert c.get("execution_accounts") is not None, "execution_accounts is required"
    _validate_execution_accounts(c["execution_accounts"])
    _validate_telegram(c.get("telegram"))
    _validate_mirror_orders(c.get("mirror_orders"))
    _validate_leaders(c, set(c["execution_accounts"]))
    _validate_auto_breakeven_effective(c)
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("config")
    validate(p.parse_args().config)
    print("valid")
