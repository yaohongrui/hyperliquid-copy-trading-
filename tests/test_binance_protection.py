"""Binance protection orders live on the algo-order endpoints.

Conditional STOP_MARKET orders are invisible to ``/openOrders`` and cannot be
canceled through the regular order endpoint.  Missing that distinction left a
stale close-position stop on the book: every replacement failed with -4130, so
the stop price never followed the position.
"""

import asyncio

import ccxt

from exchanges.ccxt_exchange import CcxtExchange


def make_exchange(client, name="binance"):
    exchange = CcxtExchange.__new__(CcxtExchange)
    exchange.name = name
    exchange.config = {}
    exchange.client = client
    exchange._symbol = lambda symbol: "BTC/USDT:USDT"
    return exchange


class ConditionalClient:
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.calls = []

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append(("fetch", symbol, dict(params or {})))
        return self.orders

    async def cancel_order(self, order_id, symbol=None, params=None):
        self.calls.append(("cancel", order_id, symbol, dict(params or {})))
        return {"id": order_id}


def stop_order(order_id, close_position=True, reduce_only=True):
    return {
        "id": order_id, "symbol": "BTC/USDT:USDT", "side": "buy",
        "triggerPrice": 100.0, "reduceOnly": reduce_only,
        "info": {"closePosition": close_position, "reduceOnly": reduce_only},
    }


def test_cancel_protection_uses_the_algo_endpoint():
    client = ConditionalClient()
    exchange = make_exchange(client)

    asyncio.run(exchange.cancel_protection("11", "BTC"))

    assert client.calls == [("cancel", "11", "BTC/USDT:USDT", {"stop": True})]


def test_cancel_protection_orders_lists_and_cancels_algo_orders():
    client = ConditionalClient([stop_order("11"), stop_order("12")])
    exchange = make_exchange(client)

    asyncio.run(exchange.cancel_protection_orders("BTC", keep_id="12"))

    assert ("fetch", "BTC/USDT:USDT", {"stop": True}) in client.calls
    assert ("cancel", "11", "BTC/USDT:USDT", {"stop": True}) in client.calls
    assert not any(call[1] == "12" for call in client.calls if call[0] == "cancel")


def test_cancel_protection_orders_leaves_plain_limit_orders_alone():
    client = ConditionalClient([
        stop_order("11"),
        {"id": "99", "symbol": "BTC/USDT:USDT", "triggerPrice": 100.0,
         "reduceOnly": False, "info": {"closePosition": "false", "reduceOnly": "false"}},
    ])
    exchange = make_exchange(client)

    asyncio.run(exchange.cancel_protection_orders("BTC"))

    canceled = [call[1] for call in client.calls if call[0] == "cancel"]
    assert canceled == ["11"]


class RetryClient:
    def __init__(self):
        self.attempts = 0

    def price_to_precision(self, market, price):
        return str(price)

    async def create_order(self, market, order_type, side, amount, price, params):
        self.attempts += 1
        if self.attempts == 1:
            raise ccxt.ExchangeError(
                'binanceusdm {"code":-4130,"msg":"An open stop or take profit order '
                'with GTE and closePosition in the direction is existing."}')
        return {"id": "new"}


def test_set_protection_clears_a_stale_stop_before_retrying():
    client = RetryClient()
    exchange = make_exchange(client)
    cleaned = []

    async def cancel_protection_orders(symbol, keep_id=None):
        cleaned.append(symbol)

    exchange.cancel_protection_orders = cancel_protection_orders

    order_id = asyncio.run(exchange.set_protection("BTC", "buy", 0, 100.0))

    assert order_id == "new"
    assert cleaned == ["BTC"]
    assert client.attempts == 2


class MergeClient(ConditionalClient):
    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        if symbol is None and (params or {}).get("stop"):
            return [stop_order("11")]
        return [{"id": "99", "symbol": "BTC/USDT:USDT", "type": "limit",
                 "reduceOnly": False, "info": {}}]


def test_fetch_open_orders_includes_conditional_stops():
    exchange = make_exchange(MergeClient())

    orders = asyncio.run(exchange.fetch_open_orders())

    assert [order["id"] for order in orders] == ["99", "11"]
