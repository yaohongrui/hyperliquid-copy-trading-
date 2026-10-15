import aiohttp
import ccxt.async_support as ccxt
from core.models import Position


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _address(value):
    # YAML parses unquoted 0x... values as integers.
    return hex(value) if isinstance(value, int) else str(value)


def _leverage(position):
    info = position.get("info") or {}
    value = (position.get("leverage") or info.get("leverage") or
             info.get("leverage_value") or info.get("leverageValue") or 1)
    if isinstance(value, dict):
        value = value.get("value") or value.get("amount") or value.get("leverage") or 1
    return _number(value)


def _field(position, *names):
    info = position.get("info") or {}
    for name in names:
        value = position.get(name)
        if value is None or value == "":
            value = info.get(name)
        if value is not None and value != "":
            number = _number(value)
            if number:
                return abs(number)
    return 0.0


def _roi(pnl, size, entry, leverage, notional=0.0, margin=0.0):
    # Prefer exchange supplied margin/notional; contract sizes differ by venue.
    margin = margin or (notional / (leverage or 1))
    margin = margin or (abs(size) * abs(entry) / (leverage or 1))
    return (pnl / margin * 100) if margin else 0.0


def _fmt(value):
    return f"{_number(value):g}"


def _symbol(value):
    return str(value or "?").split("/", 1)[0].split(":", 1)[-1]


def _table(rows, include_leverage=True):
    headers = (("Symbol", "Direction", "Leverage", "ROI", "PnL", "Entry", "Mark", "Size")
               if include_leverage else
               ("Symbol", "Direction", "ROI", "PnL", "Entry", "Mark", "Size"))
    lines = [" | ".join(headers), " | ".join("---" for _ in headers)]
    lines.extend(" | ".join(row) for row in rows)
    return "\n".join(lines)

async def leader_snapshot(config):
    address = _address(config.get("address"))
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        states = await _leader_states(session, address)
    state = {"assetPositions": [row for item in states for row in item.get("assetPositions", [])]}
    result = {}
    for row in state.get("assetPositions") or []:
        p = row.get("position") or row
        size = _number(p.get("szi"))
        if size:
            lev = p.get("leverage") or {}
            result[str(p.get("coin", "")).upper()] = Position(str(p.get("coin", "")).upper(), size, _number(p.get("entryPx")), _number(p.get("markPx")), _number(p.get("unrealizedPnl")), _number(lev.get("value", 1) if isinstance(lev, dict) else lev))
    return result


async def _leader_states(session, address):
    """Fetch the default perp dex and all HIP-3 perp dexes."""
    dexes = [None]
    try:
        async with session.post("https://api.hyperliquid.xyz/info", json={"type": "perpDexs"}) as response:
            response.raise_for_status()
            for item in await response.json() or []:
                if isinstance(item, dict):
                    dex = item.get("name") or item.get("dex") or item.get("fullName")
                    if dex:
                        dexes.append(dex)
    except Exception:
        pass
    states = []
    for dex in dict.fromkeys(dexes):
        payload = {"type": "clearinghouseState", "user": address}
        if dex:
            payload["dex"] = dex
        async with session.post("https://api.hyperliquid.xyz/info", json=payload) as response:
            response.raise_for_status()
            states.append(await response.json())
    return states


async def leader_positions(leaders):
    lines = []
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, config in leaders.items():
            address = _address(config.get("address"))
            if not address:
                continue
            states = await _leader_states(session, address)
            lines.append(f"{name} ({address})")
            positions = [row for state in states for row in (state.get("assetPositions") or [])]
            if not positions:
                lines.append("  No open positions")
            shown = 0
            rows = []
            for item in positions:
                position = item.get("position") or item
                size = _number(position.get("szi"))
                if not size:
                    continue
                shown += 1
                entry = _number(position.get("entryPx"))
                mark = _number(position.get("markPx"))
                if not mark and size:
                    mark = abs(_number(position.get("positionValue"))) / abs(size)
                pnl = _number(position.get("unrealizedPnl"))
                notional = _field(position, "positionValue", "notional")
                margin = _field(position, "marginUsed", "initialMargin", "collateral")
                leverage = _leverage(position) or (notional / margin if margin else 1.0)
                rows.append((_symbol(position.get("coin", "?")), "LONG" if size > 0 else "SHORT",
                             f"{leverage:g}x", f"{_roi(pnl, size, entry, leverage, notional, margin):g}%",
                             _fmt(pnl), _fmt(entry), _fmt(mark), _fmt(abs(size))))
            if positions and not shown:
                lines.append("  No open positions")
            elif rows:
                lines.append(_table(rows))
    return "\n".join(lines) if lines else "No leaders configured."


async def account_positions(accounts, leaders):
    names = {item.get("execution_account") for item in leaders.values()}
    lines = []
    for name in sorted(names):
        config = accounts.get(name)
        if not config:
            continue
        exchange_type = config.get("exchange")
        if exchange_type == "binance":
            exchange_class = ccxt.binanceusdm
            credentials = {"apiKey": config.get("api_key"), "secret": config.get("api_secret")}
        elif exchange_type == "gate":
            exchange_class = ccxt.gate
            credentials = {"apiKey": config.get("api_key"), "secret": config.get("secret_key")}
        else:
            lines.append(f"{name}: unsupported exchange {exchange_type}")
            continue
        exchange = exchange_class({
            **credentials,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        })
        try:
            positions = await exchange.fetch_positions()
            orders = await exchange.fetch_open_orders()
            lines.append(f"{name} ({exchange_type})")
            open_positions = [p for p in positions if _number(p.get("contracts"))]
            if not open_positions:
                lines.append("  No open positions")
            rows = []
            for position in open_positions:
                contracts = _number(position.get("contracts"))
                contract_size = _number(position.get("contractSize")) or _number((position.get("info") or {}).get("contractSize")) or 1.0
                size = contracts * contract_size
                entry = _number(position.get("entryPrice"))
                mark = _number(position.get("markPrice"))
                pnl = _number(position.get("unrealizedPnl"))
                notional = _field(position, "notional", "positionValue")
                margin = _field(position, "initialMargin", "marginUsed", "collateral")
                leverage = _leverage(position) or (notional / margin if margin else 1.0)
                direction = str(position.get("side") or ("long" if size > 0 else "short")).upper()
                rows.append((_symbol(position.get("symbol", "?")), direction,
                             f"{_roi(pnl, size, entry, leverage, notional, margin):g}%", _fmt(pnl), _fmt(entry),
                             _fmt(mark), _fmt(abs(size))))
            if rows:
                lines.append(_table(rows, include_leverage=False))
            lines.append("  Open orders")
            if not orders:
                lines.append("    No open orders")
            for order in orders:
                info = order.get('info') or {}
                reduce_only = order.get('reduceOnly')
                if reduce_only is None:
                    reduce_only = info.get('reduceOnly', info.get('reduce_only', False))
                lines.append(
                    f"    {order.get('symbol', '?')} {str(order.get('side') or '?').upper()} "
                    f"{_number(order.get('amount') or order.get('remaining')):g} @ "
                    f"{_number(order.get('price')):g} | {str(order.get('type') or '?').upper()} "
                    f"| reduce-only {bool(reduce_only)} | id {order.get('id', '?')}"
                )
        finally:
            await exchange.close()
    return "\n".join(lines) if lines else "No execution accounts configured."
