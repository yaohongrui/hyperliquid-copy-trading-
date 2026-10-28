"""Typed configuration for the Binance position source."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BinanceWebConfig:
    cookie: str = ""
    csrf_token: str = ""
    fvideo_id: str = ""
    fvideo_token: str = ""
    bnc_uuid: str = ""
    device_info: str = ""
    user_agent: str = "Mozilla/5.0"

    def __post_init__(self):
        # An unset ${ENV} expands to an empty YAML value, which parses as
        # None.  aiohttp refuses to serialize None header values, so keep the
        # in-memory config uniformly string-typed.
        for name in ("cookie", "csrf_token", "fvideo_id", "fvideo_token",
                     "bnc_uuid", "device_info", "user_agent"):
            value = getattr(self, name)
            setattr(self, name, "" if value is None else str(value))
        if not self.user_agent:
            self.user_agent = "Mozilla/5.0"


@dataclass
class BinanceBrowserConfig:
    profile_dir: str = "data/binance-browser-profile"
    headless: bool = True
    timeout_ms: int = 30000


@dataclass
class BinanceSessionConfig:
    refresh_interval_hours: float = 4.0
    auth_probe_portfolio_id: str = ""


@dataclass
class PositionSourceConfig:
    type: str
    mode: str = "hybrid"
    web: BinanceWebConfig = field(default_factory=BinanceWebConfig)
    browser: BinanceBrowserConfig = field(default_factory=BinanceBrowserConfig)
    session: BinanceSessionConfig = field(default_factory=BinanceSessionConfig)


def build_sources(raw: dict) -> dict[str, PositionSourceConfig]:
    """Turn position_sources YAML into the adapters' typed configuration."""
    sources: dict[str, PositionSourceConfig] = {}
    for name, value in (raw.get("position_sources") or {}).items():
        item = dict(value or {})
        item["type"] = str(item.get("type", "")).lower()
        if item["type"] != "binance":
            raise ValueError(f"unsupported position source type for {name}: {item['type']}")
        item["mode"] = str(item.get("mode", "hybrid")).lower()
        item["web"] = BinanceWebConfig(**(item.get("web") or {}))
        item["browser"] = BinanceBrowserConfig(**(item.get("browser") or {}))
        item["session"] = BinanceSessionConfig(**(item.get("session") or {}))
        sources[str(name)] = PositionSourceConfig(**item)
    return sources
