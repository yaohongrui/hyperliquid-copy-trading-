import ccxt.async_support as ccxt
from decimal import Decimal

from core.models import Order
from .base import OrderQuantityTooSmall


class CcxtExchange:
    """Small adapter for the configured USDT perpetual account."""

    def __init__(self, config):
        self.config = config
        name = str(config.get("exchange", "")).lower()
        if name == "gate":
            cls = ccxt.gate
            secret = config.get("secret_key")
        elif name == "binance":
            cls = ccxt.binanceusdm
            secret = config.get("api_secret")
        else:
            raise ValueError(f"Unsupported execution exchange: {name}")
        self.name = name
        self.client = cls({
            "apiKey": config.get("api_key"),
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        })
        self._markets_loaded = False

    async def start(self):
        await self.client.load_markets()
        self._markets_loaded = True

    async def stop(self):
        await self.client.close()

    def _symbol(self, symbol):
        raw = str(symbol).upper().replace("_", "/")
        if raw in self.client.markets:
            return raw
        # Hyperliquid HIP-3 symbols are namespaced (e.g. XYZ:AMD), while
        # Binance/Gate list the underlying perpetual as AMD/USDT:USDT.
        base = raw.split("/")[0].split(":")[-1]
        for candidate, market in self.client.markets.items():
            if market.get("swap") and market.get("base", "").upper() == base and market.get("quote", "").upper() == "USDT":
                return candidate
        raise ValueError(f"No USDT perpetual market for {symbol}")

    async def create_order(self, symbol, side, amount, order_type="market", price=None, reduce_only=False):
        market = self._symbol(symbol)
        contract_size = float(self.client.markets[market].get('contractSize') or 1)
        try:
            contracts = float(self.client.amount_to_precision(market, amount / contract_size))
        except ccxt.InvalidOrder as exc:
            raise OrderQuantityTooSmall(str(exc)) from exc
        minimum = self.client.markets[market].get('limits', {}).get('amount', {}).get('min') or 0
        if contracts <= 0 or Decimal(str(contracts)) < Decimal(str(minimum)):
            raise OrderQuantityTooSmall(f'quantity {contracts:g} is below exchange minimum {minimum}')
        if order_type == 'limit' and (price is None or price <= 0):
            raise ValueError('limit price must be positive')
        params = {"reduceOnly": True} if reduce_only else {}
        # Gate perpetual market orders require an explicit IOC/FOK policy.
        if self.name == "gate" and order_type == "market":
            # ccxt versions differ in which alias they forward to Gate.
            params["timeInForce"] = "IOC"
            params["tif"] = "ioc"
        leverage = self.config.get("leverage")
        if leverage and not reduce_only:
            maximum = self.client.markets[market].get('limits', {}).get('leverage', {}).get('max')
            effective = min(int(leverage), int(maximum)) if maximum else int(leverage)
            if self.name == 'gate' and self.config.get('margin_mode') in ('cross', 'crossed'):
                await self.client.set_leverage(0, market, {'cross_leverage_limit': effective})
            else:
                await self.client.set_leverage(effective, market)
        try:
            result = await self.client.create_order(market, order_type, side, contracts, price, params)
        except ccxt.InvalidOrder as exc:
            # Binance enforces a separate minimum notional (currently often
            # 5 USDT) at order submission. Treat it like a small quantity so
            # the engine can accumulate this increment for a later retry.
            message = str(exc)
            lowered = message.lower()
            small_order_error = (
                '-4164' in lowered
                or 'notional must be no smaller than' in lowered
                or 'minimum notional' in lowered
                or 'minimum order size' in lowered
                or 'minimum order amount' in lowered
                or 'order size is too small' in lowered
                or 'amount is less than the minimum' in lowered
            )
            if small_order_error:
                raise OrderQuantityTooSmall(message) from exc
            raise
        return Order(
            str(result.get("id", "")), symbol, side, float(result.get('filled') or result.get('amount') or contracts) * contract_size, result.get('average') or result.get('price'),
            result.get("status", "open"), reduce_only,
        )

    async def fetch_positions(self):
        return await self.client.fetch_positions()

    async def fetch_position_detail(self, symbol):
        market = self._symbol(symbol)
        rows = await self.client.fetch_positions([market])
        for row in rows:
            if row.get('symbol') == market and float(row.get('contracts') or 0):
                return row
        return None

    async def set_protection(self, symbol, side, amount, trigger_price):
        """Place an exchange-native stop that closes the whole position.

        ``closePosition=true`` is important here: a stop order with a fixed
        quantity can remain too large after a manual partial close. Binance
        rejects ``quantity``/``reduceOnly`` together with closePosition and
        resolves the quantity when the trigger fires.
        """
        market = self._symbol(symbol)
        trigger = float(self.client.price_to_precision(market, trigger_price))
        if self.name == 'binance':
            params = {'stopPrice': trigger, 'closePosition': True}
            result = await self.client.create_order(market, 'STOP_MARKET', side, None, None, params)
        elif self.name == 'gate':
            # Gate's futures API exposes trigger orders through CCXT's
            # unified triggerPrice parameter.  reduceOnly keeps the order
            # from opening a reverse position when it fires.
            params = {'triggerPrice': trigger, 'reduceOnly': True}
            result = await self.client.create_order(market, 'market', side, amount, None, params)
        else:
            params = {'triggerPrice': trigger, 'reduceOnly': True}
            result = await self.client.create_order(market, 'market', side, amount, None, params)
        return str(result.get('id', ''))

    async def cancel_protection(self, order_id, symbol):
        if self.name == 'gate':
            return await self.client.privateFuturesDeleteSettlePriceOrdersOrderId({
                'settle': self.config.get('settle', 'usdt'),
                'order_id': str(order_id),
            })
        return await self.cancel_order(order_id, symbol)

    async def cancel_protection_orders(self, symbol, keep_id=None):
        """Remove duplicate conditional reduce-only orders for one market."""
        if self.name == 'gate':
            market = self._symbol(symbol)
            contract = self.client.markets[market]['id']
            orders = await self.client.privateFuturesGetSettlePriceOrders({
                'settle': self.config.get('settle', 'usdt'), 'status': 'open',
            })
            for order in orders or []:
                initial = order.get('initial') or {}
                oid = str(order.get('id_string') or order.get('id') or '')
                if (oid and oid != str(keep_id or '') and
                        str(initial.get('contract', '')).upper() == contract.upper() and
                        bool(initial.get('is_reduce_only') or initial.get('reduce_only'))):
                    await self.cancel_protection(oid, symbol)
            return
        market = self._symbol(symbol)
        for order in await self.client.fetch_open_orders(market):
            oid = str(order.get('id') or '')
            if keep_id and oid == str(keep_id):
                continue
            info = order.get('info') or {}
            conditional = order.get('triggerPrice') is not None or order.get('stopPrice') is not None
            conditional = conditional or bool(info.get('trigger') or info.get('stopPrice') or info.get('stop_price'))
            reduce_only = order.get('reduceOnly')
            if reduce_only is None:
                reduce_only = info.get('reduceOnly', info.get('reduce_only', False))
            if oid and conditional and reduce_only:
                await self.cancel_order(oid, symbol)

    async def fetch_open_orders(self):
        return await self.client.fetch_open_orders()

    async def position_size(self, symbol):
        market = self._symbol(symbol)
        rows = await self.client.fetch_positions([market])
        return sum(float(p.get('contracts') or 0) * float(p.get('contractSize') or self.client.markets[market].get('contractSize') or 1) * (-1 if p.get('side') == 'short' else 1) for p in rows if p.get('symbol') == market)

    async def entry_quote(self, symbol, side):
        book = await self.client.fetch_order_book(self._symbol(symbol), 5)
        levels = book['asks' if side == 'buy' else 'bids']
        if not levels:
            raise ValueError('empty execution order book')
        return float(levels[0][0])

    async def equity(self):
        balance = await self.client.fetch_balance({'type': 'swap'})
        if self.name == 'gate':
            rows = balance.get('info') or []
            row = next(x for x in rows if str(x.get('currency')).upper() == 'USDT')
            return float(row['total']) + float(row.get('unrealised_pnl') or 0)
        account = balance.get('USDT') or {}
        wallet_equity = float(account.get('total') or account.get('free') or 0)
        positions = await self.client.fetch_positions()
        unrealized = sum(float(position.get('unrealizedPnl') or 0) for position in positions)
        return wallet_equity + unrealized

    async def close_all_positions(self):
        """Market-close every open USDT perpetual position reduce-only."""
        closed = 0
        for position in await self.client.fetch_positions():
            contracts = abs(float(position.get('contracts') or 0))
            symbol = position.get('symbol')
            if not symbol or contracts <= 0:
                continue
            side = 'sell' if str(position.get('side')).lower() == 'long' else 'buy'
            await self.create_order(symbol, side, contracts, 'market', reduce_only=True)
            closed += 1
        return closed

    async def cancel_order(self, order_id, symbol=None):
        return await self.client.cancel_order(order_id, self._symbol(symbol) if symbol else None)
