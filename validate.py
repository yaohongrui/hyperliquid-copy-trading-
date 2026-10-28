import argparse
import re

from config.loader import load
from core.symbols import normalize
from sources.binance.config import build_sources
from sources.routing import DEFAULT_SOURCE, leader_source

_PLACEHOLDER = re.compile(r"\$\{[^}]+\}")
_MODES = {"http", "browser", "hybrid"}


def _validate_symbol_map(value):
    if value is None:
        return
    assert isinstance(value, dict), "symbol_map must be a mapping"
    for key, target in value.items():
        assert isinstance(key, str) and key.strip(), "symbol_map keys must be non-empty strings"
        assert normalize(key), f"symbol_map key has no base asset: {key!r}"
        assert target is None or isinstance(target, str), f"symbol_map[{key!r}] must be a string or null"


def _validate_position_sources(value):
    if value is None:
        return
    assert isinstance(value, dict), "position_sources must be a mapping"
    assert DEFAULT_SOURCE not in value, f"position_sources key {DEFAULT_SOURCE!r} is reserved"
    try:
        build_sources({"position_sources": value})
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"invalid position_sources: {exc}") from exc
    for name, cfg in value.items():
        assert isinstance(cfg, dict), f"position_sources.{name} must be a mapping"
        assert str(cfg.get("type", "")).lower() == "binance", (
            f"position_sources.{name}.type must be 'binance'"
        )
        mode = str(cfg.get("mode", "hybrid")).lower()
        assert mode in _MODES, f"position_sources.{name}.mode must be http, browser or hybrid"
        web = cfg.get("web") or {}
        browser = cfg.get("browser") or {}
        session = cfg.get("session") or {}
        assert isinstance(web, dict), f"position_sources.{name}.web must be a mapping"
        assert isinstance(browser, dict), f"position_sources.{name}.browser must be a mapping"
        assert isinstance(session, dict), f"position_sources.{name}.session must be a mapping"
        if mode in {"browser", "hybrid"}:
            assert str(browser.get("profile_dir") or "").strip(), (
                f"position_sources.{name}.browser.profile_dir is required"
            )
        if mode == "hybrid":
            probe = str(session.get("auth_probe_portfolio_id") or "").strip()
            assert probe and not _PLACEHOLDER.search(probe), (
                f"position_sources.{name}.session.auth_probe_portfolio_id is required for hybrid mode"
            )
        if mode == "http":
            cookie = str(web.get("cookie") or "").strip()
            assert cookie and not _PLACEHOLDER.search(cookie), (
                f"position_sources.{name}.web.cookie is required for http mode"
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


def _validate_leaders(c, source_names, accounts):
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

        source = leader_source(leader)
        if source == DEFAULT_SOURCE:
            _leader_address(leader, label)
        else:
            assert source in source_names, f"{label}: unknown source {source}"
            portfolio = str(leader.get("portfolio_id") or "").strip()
            assert portfolio and not _PLACEHOLDER.search(portfolio), (
                f"{label}: portfolio_id is required for source {source}"
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
    assert c.get('leaders') is not None
    assert c.get('execution_accounts') is not None
    _validate_symbol_map(c.get('symbol_map'))
    _validate_position_sources(c.get('position_sources'))
    _validate_leaders(c, set(c.get('position_sources') or {}), set(c['execution_accounts']))
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('config')
    validate(p.parse_args().config)
    print('valid')
