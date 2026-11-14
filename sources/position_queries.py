import asyncio
import time

import aiohttp
from core.models import Position

# The dex list only changes when a HIP-3 deployment is added, so a per-process
# cache keeps the 20-weight ``perpDexs`` probe off the hot path.  The REST
# fallback and the websocket resync share one IP quota, so every avoided call
# matters.
_PERP_DEX_TTL = 300.0
_perp_dex_cache = {"at": 0.0, "values": [None]}


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


def _display(value, symbol_map=None):
    """Show a venue instrument under its Hyperliquid name when mapped."""
    if symbol_map:
        return symbol_map.display(value) or _symbol(value)
    return _symbol(value)


def _table(rows, include_leverage=True):
    headers = (("Symbol", "Direction", "Leverage", "ROI", "PnL", "Entry", "Mark", "Size")
               if include_leverage else
               ("Symbol", "Direction", "ROI", "PnL", "Entry", "Mark", "Size"))
    lines = [" | ".join(headers), " | ".join("---" for _ in headers)]
    lines.extend(" | ".join(row) for row in rows)
    return "\n".join(lines)

async def leader_snapshot(config, session=None):
    if session is None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as owned:
            return await leader_snapshot(config, owned)
    address = _address(config.get("address"))
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


async def _perp_dexes(session):
    """Return the default dex plus every HIP-3 dex, cached for five minutes."""
    now = time.monotonic()
    if _perp_dex_cache["at"] and now - _perp_dex_cache["at"] < _PERP_DEX_TTL:
        return _perp_dex_cache["values"]
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
        # A transient failure must not hide the last known dex list; retry on
        # the next call rather than caching the degraded result.
        return _perp_dex_cache["values"]
    values = list(dict.fromkeys(dexes))
    _perp_dex_cache["values"] = values
    _perp_dex_cache["at"] = now
    return values


async def _leader_states(session, address):
    """Fetch the default perp dex and all HIP-3 perp dexes."""
    dexes = await _perp_dexes(session)

    async def fetch(dex):
        payload = {"type": "clearinghouseState", "user": address}
        if dex:
            payload["dex"] = dex
        async with session.post("https://api.hyperliquid.xyz/info", json=payload) as response:
            response.raise_for_status()
            return await response.json()

    return await asyncio.gather(*(fetch(dex) for dex in dexes))


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
                rows.append((str(position.get("coin") or "?").strip().upper(), "LONG" if size > 0 else "SHORT",
                             f"{leverage:g}x", f"{_roi(pnl, size, entry, leverage, notional, margin):g}%",
                             _fmt(pnl), _fmt(entry), _fmt(mark), _fmt(abs(size))))
            if positions and not shown:
                lines.append("  No open positions")
            elif rows:
                lines.append(_table(rows))
    return "\n".join(lines) if lines else "No leaders configured."


def _hyperliquid_info(config):
    """Build a read-only Info client and resolve the account address."""
    from eth_account import Account
    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

    base_url = TESTNET_API_URL if config.get("testnet") else MAINNET_API_URL
    bootstrap = Info(base_url, skip_ws=True)
    raw = bootstrap.perp_dexs() or []
    dexes = [""] + [item["name"] for item in raw[1:] if item and item.get("name")]
    info = Info(base_url, skip_ws=True, perp_dexs=dexes)
    address = config.get("account_address")
    if isinstance(address, int):
        address = hex(address)
    if not address:
        key = config.get("api_private_key") or config.get("secret_key")
        if not key:
            raise ValueError("account_address or api_private_key is required")
        address = Account.from_key(key).address
    return info, address, dexes


def _hyperliquid_positions(info, address, dexes):
    rows = []
    for dex in dexes:
        try:
            state = info.user_state(address, dex)
        except Exception:
            continue
        for item in state.get("assetPositions") or []:
            position = item.get("position") or item
            if _number(position.get("szi")):
                rows.append(position)
    return rows


def _hyperliquid_open_orders(info, address, dexes):
    rows = []
    for dex in dexes:
        try:
            rows.extend(info.open_orders(address, dex) or [])
        except Exception:
            continue
    return rows


def _ccxt_client(config, exchange_type):
    """Build a short-lived ccxt client for a Gate/Binance execution account."""
    import ccxt.async_support as ccxt

    if exchange_type == "gate":
        cls = ccxt.gate
        secret = config.get("secret_key")
    elif exchange_type == "binance":
        cls = ccxt.binanceusdm
        secret = config.get("api_secret")
    else:
        raise ValueError(f"unsupported exchange {exchange_type}")
    return cls({
        "apiKey": config.get("api_key"),
        "secret": secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "fetchOpenOrders": {"warnWithoutSymbol": False},
        },
    })


async def _ccxt_account_lines(config, exchange_type, symbol_map=None):
    """Positions/orders report for a ccxt execution account (Gate)."""
    exchange = _ccxt_client(config, exchange_type)
    try:
        positions = await exchange.fetch_positions()
        orders = await exchange.fetch_open_orders()
    finally:
        await exchange.close()
    lines = []
    open_positions = [p for p in positions if _number(p.get("contracts"))]
    if not open_positions:
        lines.append("  No open positions")
    rows = []
    for position in open_positions:
        contracts = _number(position.get("contracts"))
        contract_size = (_number(position.get("contractSize"))
                         or _number((position.get("info") or {}).get("contractSize")) or 1.0)
        size = contracts * contract_size
        entry = _number(position.get("entryPrice"))
        mark = _number(position.get("markPrice"))
        pnl = _number(position.get("unrealizedPnl"))
        notional = _field(position, "notional", "positionValue")
        margin = _field(position, "initialMargin", "marginUsed", "collateral")
        leverage = _leverage(position) or (notional / margin if margin else 1.0)
        direction = str(position.get("side") or ("long" if size > 0 else "short")).upper()
        rows.append((_display(position.get("symbol", "?"), symbol_map), direction,
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
            f"    {_display(order.get('symbol', '?'), symbol_map)} {str(order.get('side') or '?').upper()} "
            f"{_number(order.get('amount') or order.get('remaining')):g} @ "
            f"{_number(order.get('triggerPrice') or order.get('stopPrice') or order.get('price')):g} | "
            f"{str((info.get('orderType') or order.get('type')) or '?').upper()} "
            f"| reduce-only {bool(reduce_only)} | id {order.get('id', '?')}"
        )
    return lines


async def account_positions(accounts, leaders, symbol_map=None):
    names = {item.get("execution_account") for item in leaders.values()}
    lines = []
    for name in sorted(names):
        config = accounts.get(name)
        if not config:
            continue
        exchange_type = str(config.get("exchange") or "").lower()
        if exchange_type != "hyperliquid":
            lines.append(f"{name} ({exchange_type or 'unknown'})")
            try:
                lines.extend(await _ccxt_account_lines(config, exchange_type, symbol_map))
            except Exception as exc:
                lines.append(f"  query failed ({exc})")
            continue
        try:
            info, address, dexes = await asyncio.to_thread(_hyperliquid_info, config)
            positions = await asyncio.to_thread(_hyperliquid_positions, info, address, dexes)
            orders = await asyncio.to_thread(_hyperliquid_open_orders, info, address, dexes)
        except Exception as exc:
            lines.append(f"{name}: Hyperliquid query failed ({exc})")
            continue
        lines.append(f"{name} (hyperliquid)")
        if not positions:
            lines.append("  No open positions")
        rows = []
        for position in positions:
            size = _number(position.get("szi"))
            entry = _number(position.get("entryPx"))
            notional = _field(position, "positionValue", "notional")
            leverage = _leverage(position) or (notional / abs(size) if size else 1.0)
            mark = notional / abs(size) if size else 0.0
            pnl = _number(position.get("unrealizedPnl"))
            rows.append((_display(position.get("coin", "?"), symbol_map),
                         "LONG" if size > 0 else "SHORT",
                         f"{_roi(pnl, size, entry, leverage, notional):g}%", _fmt(pnl), _fmt(entry),
                         _fmt(mark), _fmt(abs(size))))
        if rows:
            lines.append(_table(rows, include_leverage=False))
        lines.append("  Open orders")
        if not orders:
            lines.append("    No open orders")
        for order in orders:
            trigger = _number(order.get("triggerPx"))
            kind = "TRIGGER" if order.get("isTrigger") else str(order.get("orderType") or "LIMIT").upper()
            suffix = f" trigger {trigger:g}" if trigger else ""
            lines.append(
                f"    {_display(order.get('coin', '?'), symbol_map)} "
                f"{'BUY' if str(order.get('side')).upper() == 'B' else 'SELL'} "
                f"{_number(order.get('sz')):g} @ {_number(order.get('limitPx')):g} "
                f"| {kind}{suffix} | reduce-only {bool(order.get('reduceOnly'))} | id {order.get('oid', '?')}"
            )
    return "\n".join(lines) if lines else "No execution accounts configured."
