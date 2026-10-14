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

async def leader_snapshot(config):
    address = _address(config.get("address"))
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.hyperliquid.xyz/info", json={"type":"clearinghouseState", "user":address}) as response:
            response.raise_for_status(); state = await response.json()
    result = {}
    for row in state.get("assetPositions") or []:
        p = row.get("position") or row
        size = _number(p.get("szi"))
        if size:
            lev = p.get("leverage") or {}
            result[str(p.get("coin", "")).upper()] = Position(str(p.get("coin", "")).upper(), size, _number(p.get("entryPx")), _number(p.get("markPx")), _number(p.get("unrealizedPnl")), _number(lev.get("value", 1) if isinstance(lev, dict) else lev))
    return result


async def leader_positions(leaders):
    lines = []
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, config in leaders.items():
            address = _address(config.get("address"))
            if not address:
                continue
            async with session.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "clearinghouseState", "user": address},
            ) as response:
                response.raise_for_status()
                state = await response.json()
            lines.append(f"{name} ({address})")
            positions = state.get("assetPositions") or []
            if not positions:
                lines.append("  No open positions")
            for item in positions:
                position = item.get("position") or item
                size = _number(position.get("szi"))
                if not size:
                    continue
                leverage = position.get("leverage") or {}
                leverage = leverage.get("value", 1) if isinstance(leverage, dict) else leverage
                lines.append(
                    f"  {position.get('coin', '?')} {'LONG' if size > 0 else 'SHORT'} "
                    f"{abs(size):g} | entry {_number(position.get('entryPx')):g} | "
                    f"PnL {_number(position.get('unrealizedPnl')):g} | {leverage}x"
                )
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
            for position in open_positions:
                lines.append(
                    f"  {position.get('symbol', '?')} {str(position.get('side') or '?').upper()} "
                    f"{_number(position.get('contracts')):g} | entry {_number(position.get('entryPrice')):g} | "
                    f"mark {_number(position.get('markPrice')):g} | "
                    f"PnL {_number(position.get('unrealizedPnl')):g}"
                )
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
