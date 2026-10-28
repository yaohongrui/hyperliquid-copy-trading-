"""Route leader configurations to their position source."""

DEFAULT_SOURCE = "hyperliquid"


def leader_source(cfg) -> str:
    """Return the configured source name, defaulting to hyperliquid.

    Custom source names are preserved verbatim; only a missing or empty
    source falls back to the Hyperliquid sentinel.
    """
    source = (cfg or {}).get("source")
    return str(source) if source else DEFAULT_SOURCE


def is_hyperliquid(cfg) -> bool:
    """Return True when the leader uses the built-in Hyperliquid feed."""
    return leader_source(cfg) == DEFAULT_SOURCE


def hyperliquid_leaders(leaders: dict) -> dict:
    """Return the leaders served by the built-in Hyperliquid feed."""
    return {name: cfg for name, cfg in (leaders or {}).items() if is_hyperliquid(cfg)}


def binance_leaders(leaders: dict) -> dict:
    """Return the leaders served by a configured ``position_sources`` entry.

    Every returned leader's ``source`` must name an entry under
    ``position_sources``; the Binance adapters resolve that name at runtime.
    """
    return {name: cfg for name, cfg in (leaders or {}).items() if not is_hyperliquid(cfg)}
