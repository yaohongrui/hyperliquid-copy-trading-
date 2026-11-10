"""Hyperliquid execution adapter.

Executes orders on the same venue the leaders trade on, so coin names need no
venue bridging.  The official SDK is synchronous, so every network call is run
in a worker thread to keep the engine's event loop responsive.

HIP-3 markets live on builder-deployed perp dexes.  Their assets are named
``DEX:COIN`` (e.g. ``XYZ:AMD``) and are only registered by the SDK when the
list of dex names is passed in; ``_connect`` discovers that list from
``perpDexs`` and enables every one of them.
"""

import asyncio
import logging
import time

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from core.models import Order
from core.symbols import SymbolMap
from .base import OrderQuantityTooSmall

log = logging.getLogger(__name__)

_SMALL_ORDER_TOKENS = (
    "minimum", "too small", "notional", "minimum value", "below the minimum",
)
_ORDER_GONE_TOKENS = (
    "ORDER_NOT_FOUND", "ORDER NOT FOUND", "NOT_FOUND", "NOT FOUND",
    "UNKNOWN_ORDER", "UNKNOWN ORDER", "ALREADY FILLED", "ALREADY CLOSED",
    "ALREADY CANCELED", "ALREADY CANCELLED", "NEVER PLACED",
    "NOT OPEN", "DOES NOT EXIST", "NO SUCH ORDER",
)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _address(value):
    # PyYAML may parse an unquoted 0x address as an int.
    return hex(value) if isinstance(value, int) else (str(value) if value else None)


def _small_order(message):
    lowered = str(message).lower()
    return any(token in lowered for token in _SMALL_ORDER_TOKENS)


def _order_gone(exc):
    message = str(exc).upper()
    return any(token in message for token in _ORDER_GONE_TOKENS)


class HyperliquidExchange:
    """Minimal order/position adapter backed by the official Hyperliquid SDK."""

    def __init__(self, config, symbol_map=None):
        self.config = config or {}
        self.symbol_map = symbol_map if isinstance(symbol_map, SymbolMap) else SymbolMap(symbol_map)
        self.base_url = TESTNET_API_URL if self.config.get("testnet") else MAINNET_API_URL
        self.account_address = _address(self.config.get("account_address"))
        self.private_key = self.config.get("api_private_key") or self.config.get("secret_key")
        self.slippage = float(self.config.get("slippage", 0.05) or 0.05)
        self.info = None
        self.client = None
        self.address = None
        self.dexes = [""]
        self._coins = {}
        self._max_leverage = {}
        # Unified accounts treat spot USDC as perp collateral, so equity must
        # include the free spot balance in addition to the perp account value.
        self.unified = False
        # update_leverage is a signed request; only resend when the cache misses.
        self._leverage_set = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self):
        await asyncio.to_thread(self._connect)

    async def stop(self):
        # The SDK uses plain HTTP requests (no background websocket) when
        # ``skip_ws=True``, so there is nothing to tear down.
        return None

    def _connect(self):
        if not self.private_key:
            raise RuntimeError("api_private_key is required for Hyperliquid execution")
        wallet = Account.from_key(self.private_key)
        self.address = self.account_address or wallet.address

        # The default Info only knows the original dex; ask for the full list
        # of builder-deployed dexes so their assets are registered too.
        bootstrap = Info(self.base_url, skip_ws=True)
        raw = bootstrap.perp_dexs() or []
        hip3 = [item["name"] for item in raw[1:] if item and item.get("name")]
        self.dexes = [""] + hip3

        self.info = Info(self.base_url, skip_ws=True, perp_dexs=self.dexes)
        self.client = Exchange(
            wallet,
            base_url=self.base_url,
            account_address=self.account_address,
            perp_dexs=self.dexes,
        )
        self._coins = {name.upper(): name for name in self.info.coin_to_asset}
        try:
            abstraction = self.info.query_user_abstraction_state(self.address)
            self.unified = abstraction in ("unifiedAccount", "portfolioMargin")
        except Exception:
            log.warning("Failed to read Hyperliquid account abstraction", exc_info=True)
        self._max_leverage = {}
        for dex in self.dexes:
            try:
                meta = self.info.meta(dex=dex) if dex else self.info.meta()
            except Exception:
                log.warning("Failed to read Hyperliquid meta for dex %s", dex or "default", exc_info=True)
                continue
            for item in meta.get("universe") or []:
                if item.get("name"):
                    self._max_leverage[item["name"].upper()] = int(item.get("maxLeverage") or 0)
        log.info(
            "Hyperliquid execution account=%s dexes=%d coins=%d",
            self.address, len(self.dexes), len(self._coins),
        )

    # -- symbol / precision helpers ---------------------------------------
    def _coin(self, symbol):
        raw = str(symbol or "").strip()
        if raw.upper() in self._coins:
            return self._coins[raw.upper()]
        if raw in self.info.coin_to_asset:
            return raw
        target = self.symbol_map.target(symbol)
        if target:
            if target.upper() in self._coins:
                return self._coins[target.upper()]
            if target in self.info.coin_to_asset:
                return target
        raise ValueError(f"No Hyperliquid market for {symbol}")

    def _canonical(self, coin):
        """Return the exchange's exact coin spelling for a position/order row."""
        return self._coins.get(str(coin or "").upper(), coin)

    def _asset(self, coin):
        return self.info.coin_to_asset[coin]

    def _sz_decimals(self, coin):
        return int(self.info.asset_to_sz_decimals[self._asset(coin)])

    def _round_size(self, coin, amount):
        decimals = self._sz_decimals(coin)
        return float(f"{float(amount):.{decimals}f}")

    def _round_price(self, coin, price):
        # Perp prices keep at most 5 significant figures and 6 - szDecimals
        # decimal places (same rule the SDK uses for slippage prices).
        decimals = max(0, 6 - self._sz_decimals(coin))
        return round(float(f"{float(price):.5g}"), decimals)

    @staticmethod
    def _dex(coin):
        return coin.split(":")[0] if ":" in coin else ""

    def _mark_price(self, coin):
        mids = self.info.all_mids(self._dex(coin)) or {}
        value = mids.get(coin)
        if value is None:
            raise ValueError(f"No Hyperliquid price for {coin}")
        return float(value)

    def validate_symbol_map(self):
        """Fail at startup when a mapping points at an unknown Hyperliquid coin."""
        errors = []
        for name, target in self.symbol_map.entries().items():
            try:
                self._coin(target or name)
            except ValueError as exc:
                errors.append(f"{name} -> {target}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))

    # -- account reads -----------------------------------------------------
    def _positions_for_dex(self, dex):
        state = self.info.user_state(self.address, dex)
        rows = []
        for item in state.get("assetPositions") or []:
            position = item.get("position") or item
            size = _float(position.get("szi"))
            if not size:
                continue
            entry = _float(position.get("entryPx"))
            notional = abs(_float(position.get("positionValue"))) or abs(size * entry)
            leverage = position.get("leverage") or {}
            leverage = _float(leverage.get("value")) if isinstance(leverage, dict) else _float(leverage)
            rows.append({
                "symbol": self._canonical(position.get("coin")),
                "contracts": abs(size),
                "contractSize": 1,
                "entryPrice": entry,
                "markPrice": notional / abs(size) if size else 0.0,
                "unrealizedPnl": _float(position.get("unrealizedPnl")),
                "side": "long" if size > 0 else "short",
                "notional": notional,
                "leverage": leverage or 1,
                "info": position,
            })
        return rows

    def _fetch_positions(self):
        rows = self._positions_for_dex("")
        for dex in self.dexes[1:]:
            try:
                rows.extend(self._positions_for_dex(dex))
            except Exception:
                log.warning("Failed to read Hyperliquid positions for dex %s", dex, exc_info=True)
        return rows

    async def fetch_positions(self):
        return await asyncio.to_thread(self._fetch_positions)

    def _fetch_position_detail(self, symbol):
        coin = self._coin(symbol)
        for row in self._positions_for_dex(self._dex(coin)):
            if row["symbol"] == coin:
                return row
        return None

    async def fetch_position_detail(self, symbol):
        return await asyncio.to_thread(self._fetch_position_detail, symbol)

    async def position_size(self, symbol):
        detail = await self.fetch_position_detail(symbol)
        if not detail:
            return 0.0
        size = float(detail["contracts"])
        return -size if detail["side"] == "short" else size

    async def entry_quote(self, symbol, side):
        return await asyncio.to_thread(self._mark_price, self._coin(symbol))

    def _fetch_open_orders(self):
        rows = []
        for dex in self.dexes:
            try:
                # frontendOpenOrders carries isTrigger/triggerPx, which the plain
                # openOrders feed omits; protection cleanup needs them.
                orders = self.info.frontend_open_orders(self.address, dex) or []
            except Exception:
                try:
                    orders = self.info.open_orders(self.address, dex) or []
                except Exception:
                    log.warning("Failed to read Hyperliquid open orders for dex %s", dex or "default", exc_info=True)
                    continue
            for order in orders:
                rows.append({
                    "id": str(order.get("oid")),
                    "symbol": self._canonical(order.get("coin")),
                    "side": "buy" if str(order.get("side")).upper() == "B" else "sell",
                    "amount": _float(order.get("sz")),
                    "price": _float(order.get("limitPx")),
                    "type": "trigger" if (order.get("isTrigger") or order.get("triggerPx")) else "limit",
                    "reduceOnly": bool(order.get("reduceOnly")),
                    "triggerPrice": _float(order.get("triggerPx")),
                    "info": order,
                })
        return rows

    async def fetch_open_orders(self):
        return await asyncio.to_thread(self._fetch_open_orders)

    def _equity(self):
        # The default dex is authoritative; HIP-3 dexes keep separate margin,
        # so sum them for a whole-account view.
        state = self.info.user_state(self.address, "")
        total = _float((state.get("marginSummary") or {}).get("accountValue"))
        for dex in self.dexes[1:]:
            try:
                state = self.info.user_state(self.address, dex)
            except Exception:
                log.warning("Failed to read Hyperliquid account value for dex %s", dex, exc_info=True)
                continue
            total += _float((state.get("marginSummary") or {}).get("accountValue"))
        if self.unified:
            # In a unified account, perp margin is drawn from spot USDC; the
            # perp accountValue only covers the held portion, so add the free
            # (unheld) spot balance to get the usable account value.
            try:
                spot = self.info.spot_user_state(self.address)
            except Exception:
                log.warning("Failed to read Hyperliquid spot balance", exc_info=True)
                spot = {}
            for balance in spot.get("balances") or []:
                if str(balance.get("coin")).upper() == "USDC":
                    free = _float(balance.get("total")) - _float(balance.get("hold"))
                    total += max(0.0, free)
        return total

    async def equity(self):
        return await asyncio.to_thread(self._equity)

    # -- orders ------------------------------------------------------------
    def _ensure_leverage(self, coin, leverage):
        maximum = self._max_leverage.get(coin.upper()) or 0
        effective = min(int(leverage), maximum) if maximum else int(leverage)
        if effective <= 0 or self._leverage_set.get(coin) == effective:
            return
        try:
            self.client.update_leverage(effective, coin, is_cross=True)
            self._leverage_set[coin] = effective
        except Exception:
            log.warning("Failed to set Hyperliquid leverage %sx for %s", effective, coin, exc_info=True)

    @staticmethod
    def _parse_order(result):
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected Hyperliquid order response: {result!r}")
        if result.get("status") == "err":
            message = str(result.get("response") or result)
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(message)
        response = result.get("response") or {}
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not statuses:
            message = str(response or result)
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(f"Hyperliquid order rejected: {message}")
        status = statuses[0]
        if "error" in status:
            message = str(status["error"])
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(message)
        if "filled" in status:
            filled = status["filled"] or {}
            return {"oid": filled.get("oid"), "filled": _float(filled.get("totalSz")),
                    "avgPx": _float(filled.get("avgPx")), "resting": False}
        if "resting" in status:
            return {"oid": (status["resting"] or {}).get("oid"),
                    "filled": 0.0, "avgPx": 0.0, "resting": True}
        return {"oid": None, "filled": 0.0, "avgPx": 0.0, "resting": False}

    def _create_order(self, symbol, side, amount, order_type, price, reduce_only):
        coin = self._coin(symbol)
        size = self._round_size(coin, amount)
        if size <= 0:
            raise OrderQuantityTooSmall(f"quantity {amount:g} rounds to zero")
        if order_type == "limit" and (price is None or float(price) <= 0):
            raise ValueError("limit price must be positive")
        is_buy = side == "buy"
        leverage = self.config.get("leverage")
        if leverage and not reduce_only:
            self._ensure_leverage(coin, int(leverage))
        if order_type == "limit":
            limit_px = self._round_price(coin, float(price))
            result = self.client.order(
                coin, is_buy, size, limit_px, {"limit": {"tif": "Gtc"}}, reduce_only=reduce_only,
            )
        else:
            mid = self._mark_price(coin)
            limit_px = mid * (1 + self.slippage) if is_buy else mid * (1 - self.slippage)
            limit_px = self._round_price(coin, limit_px)
            result = self.client.order(
                coin, is_buy, size, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,
            )
        status = self._parse_order(result)
        filled = status["filled"] if not status["resting"] else size
        return Order(
            str(status["oid"] or ""), symbol, side, filled or size,
            status["avgPx"] or (float(price) if price else limit_px),
            "open" if status["resting"] else "closed", reduce_only,
        )

    async def create_order(self, symbol, side, amount, order_type="market", price=None, reduce_only=False):
        return await asyncio.to_thread(
            self._create_order, symbol, side, amount, order_type, price, reduce_only,
        )

    @staticmethod
    def _check_cancel(result):
        """Raise when a cancel response reports a non-"already gone" error."""
        if not isinstance(result, dict):
            return
        if result.get("status") == "err":
            message = str(result.get("response") or result)
            if not _order_gone(message):
                raise RuntimeError(message)
            return
        response = result.get("response") or {}
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        errors = [str(item) for item in statuses or []
                  if isinstance(item, str) and item.lower() not in ("success", "waitingforfill")]
        if errors and not all(_order_gone(message) for message in errors):
            raise RuntimeError("; ".join(errors))

    def _cancel_order(self, order_id, symbol=None):
        coin = self._coin(symbol) if symbol else None
        if not coin:
            for order in self._fetch_open_orders():
                if order["id"] == str(order_id):
                    coin = order["symbol"]
                    break
        if not coin:
            raise ValueError(f"cannot resolve Hyperliquid coin for order {order_id}")
        self._check_cancel(self.client.cancel(coin, int(order_id)))

    async def cancel_order(self, order_id, symbol=None):
        await asyncio.to_thread(self._cancel_order, order_id, symbol)

    def _set_protection(self, symbol, side, amount, trigger_price):
        coin = self._coin(symbol)
        trigger = self._round_price(coin, trigger_price)
        # Use a limit beyond the trigger so the market stop still fills.
        limit_px = trigger * (1 - self.slippage) if side == "sell" else trigger * (1 + self.slippage)
        result = self.client.order(
            coin, side == "buy", self._round_size(coin, amount), self._round_price(coin, limit_px),
            {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        return str(self._parse_order(result)["oid"] or "")

    async def set_protection(self, symbol, side, amount, trigger_price):
        return await asyncio.to_thread(self._set_protection, symbol, side, amount, trigger_price)

    async def cancel_protection(self, order_id, symbol):
        await self.cancel_order(order_id, symbol)

    def _cancel_protection_orders(self, symbol, keep_id=None):
        coin = self._coin(symbol)
        for order in self._fetch_open_orders():
            if order["symbol"] != coin:
                continue
            if keep_id and order["id"] == str(keep_id):
                continue
            if order["type"] == "trigger" and order["reduceOnly"]:
                try:
                    self._check_cancel(self.client.cancel(coin, int(order["id"])))
                except Exception as exc:
                    if not _order_gone(exc):
                        log.warning("Failed to cancel protection order %s", order["id"], exc_info=True)

    async def cancel_protection_orders(self, symbol, keep_id=None):
        await asyncio.to_thread(self._cancel_protection_orders, symbol, keep_id)

    def _cancel_all_orders(self):
        canceled = 0
        for order in self._fetch_open_orders():
            try:
                self._check_cancel(self.client.cancel(order["symbol"], int(order["id"])))
                canceled += 1
            except Exception as exc:
                if not _order_gone(exc):
                    log.warning("Failed to cancel Hyperliquid order %s", order["id"], exc_info=True)
        return canceled

    async def cancel_all_orders(self):
        return await asyncio.to_thread(self._cancel_all_orders)

    def _has_open_positions(self):
        return any(position["contracts"] > 0 for position in self._fetch_positions())

    async def has_open_positions(self):
        return await asyncio.to_thread(self._has_open_positions)

    def _close_all_positions(self, attempts):
        closed = 0
        last_error = None
        for attempt in range(attempts):
            try:
                self._cancel_all_orders()
            except Exception:
                log.warning("Failed to cancel orders before flattening", exc_info=True)
            positions = self._fetch_positions()
            if not positions:
                return closed
            for position in positions:
                coin = position["symbol"]
                size = float(position["contracts"])
                side = "sell" if position["side"] == "long" else "buy"
                try:
                    self._create_order(coin, side, size, "market", None, True)
                    closed += 1
                except Exception as exc:
                    last_error = exc
                    log.warning("Failed to close %s (%g)", coin, size, exc_info=True)
            if attempt < attempts - 1:
                time.sleep(1)
        remaining = self._fetch_positions()
        if remaining:
            raise RuntimeError(
                "failed to flatten position(s): "
                + ", ".join(str(position["symbol"]) for position in remaining)
            ) from last_error
        return closed

    async def close_all_positions(self, attempts=3):
        return await asyncio.to_thread(self._close_all_positions, attempts)
