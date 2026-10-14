import ccxt.async_support as ccxt

from core.models import Order


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
        contracts = float(self.client.amount_to_precision(market, amount / contract_size))
        if contracts <= 0:
            raise ValueError('quantity below one exchange lot')
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
        result = await self.client.create_order(market, order_type, side, contracts, price, params)
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
        """Place a Binance stop that closes the whole remaining position.

        ``closePosition=true`` is important here: a stop order with a fixed
        quantity can remain too large after a manual partial close. Binance
        rejects ``quantity``/``reduceOnly`` together with closePosition and
        resolves the quantity when the trigger fires.
        """
        market = self._symbol(symbol)
        params = {
            'stopPrice': float(self.client.price_to_precision(market, trigger_price)),
            'closePosition': True,
        }
        if self.name == 'binance':
            result = await self.client.create_order(market, 'STOP_MARKET', side, None, None, params)
        else:
            raise NotImplementedError('position protection demo currently supports Binance only')
        return str(result.get('id', ''))

    async def cancel_protection(self, order_id, symbol):
        return await self.cancel_order(order_id, symbol)

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

    async def cancel_order(self, order_id, symbol=None):
        return await self.client.cancel_order(order_id, self._symbol(symbol) if symbol else None)
