import time

from api.console import ConsoleStore


def test_local_reduce_fill_attributes_realized_trade(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": "gate", "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    store.insert_trades("gate", [{"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell",
                                  "amount": 1, "price": 110, "realizedPnl": 12, "fee": 1,
                                  "timestamp": now}])
    store.attribute_trades("gate")
    result = store.performance("gate")
    assert result["net_pnl"] == 11
    assert result["leaders"][0]["leader"] == "A"


def test_cash_flow_adjusted_drawdown(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.snapshot("gate", 100, 0)
    store.insert_cash_flows("gate", [{"id": "d1", "kind": "deposit", "amount": 100, "timestamp": time.time()}])
    store.snapshot("gate", 200, 0)
    result = store.performance("gate")
    assert result["cash_flow"] == 100
    assert result["max_drawdown"] >= 0
