"""Build Binance position-source adapters."""

from .config import PositionSourceConfig
from .poller import Poller


def create_source(cfg: PositionSourceConfig):
    if cfg.mode == "http":
        return Poller(cfg.web)
    if cfg.mode == "browser":
        from .browser import BinanceBrowserPositionSource
        return BinanceBrowserPositionSource(cfg.browser)
    from .session import HybridBinancePositionSource
    return HybridBinancePositionSource(
        Poller(cfg.web), cfg.browser, cfg.session, cfg.session.auth_probe_portfolio_id
    )
