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
            "options": {"defaultType": "swap"},
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
        base = raw.split("/")[0]
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
        return float(balance['USDT']['total'])

    async def cancel_order(self, order_id, symbol=None):
        return await self.client.cancel_order(order_id, self._symbol(symbol) if symbol else None)
