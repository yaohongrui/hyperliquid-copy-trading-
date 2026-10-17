import sqlite3
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


def test_millisecond_trade_timestamps_are_normalized_and_attributed(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": "gate", "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    # Exchanges report milliseconds; local fills use seconds.
    store.insert_trades("gate", [{"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell",
                                  "amount": 1, "price": 110, "realizedPnl": 12, "fee": 1,
                                  "timestamp": now * 1000}])
    store.attribute_trades("gate")
    result = store.performance("gate")
    assert result["leaders"][0]["leader"] == "A"
    assert result["leaders"][0]["net_pnl"] == 11
    assert store.performance("gate", since=now - 60, until=now + 60)["trades"] == 1


def test_existing_millisecond_rows_are_backfilled_on_open(tmp_path):
    path = tmp_path / "console.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, account TEXT, exchange_trade_id TEXT UNIQUE,"
               " symbol TEXT, side TEXT, quantity REAL, price REAL, realized_pnl REAL, fee REAL, timestamp REAL)")
    db.execute("INSERT INTO trades(account,symbol,timestamp) VALUES('gate','BTC/USDT:USDT',1792221486709)")
    db.commit()
    db.close()
    ConsoleStore(path)  # constructor runs the in-place migration
    got = sqlite3.connect(path).execute("SELECT timestamp FROM trades").fetchone()[0]
    assert got < 1e10
    assert abs(got - 1792221486.709) < 0.001


def test_gate_closed_position_row_keeps_pnl_legs_separate():
    from exchanges.ccxt_exchange import CcxtExchange

    row = CcxtExchange._closed_position_row({
        "symbol": "PUMP/USDT:USDT", "side": "long", "timestamp": 1711279263000,
        "info": {"contract": "PUMP_USDT", "time": 1711279263, "accum_size": "100",
                 "long_price": "0.766306", "pnl": "-23.41702352", "pnl_pnl": "-22.7187",
                 "pnl_fee": "-0.06527125", "pnl_fund": "-0.63305227"},
    })
    assert row["source"] == "position_close"
    assert row["id"] == "PUMP_USDT:1711279263"
    assert row["fee"] == 0.06527125  # cost stored positive
    assert row["funding"] == -0.63305227
    # net = gross - fees + funding must reconcile with the endpoint's total pnl
    assert abs((row["realizedPnl"] - row["fee"] + row["funding"]) - (-23.41702352)) < 1e-6


def test_closed_position_row_ignores_non_gate_payloads():
    from exchanges.ccxt_exchange import CcxtExchange

    # Other venues also expose position history; those must not parse as Gate.
    assert CcxtExchange._closed_position_row({"symbol": "BTC/USDT:USDT", "info": {"symbol": "BTCUSDT"}}) is None


def test_legacy_fee_only_rows_are_purged_on_open(tmp_path):
    path = tmp_path / "console.sqlite3"
    store = ConsoleStore(path)
    store.insert_trades("gate", [{"id": "junk", "symbol": "BTC/USDT:USDT", "realizedPnl": 0, "fee": 1}])
    store.insert_trades("gate", [{"id": "real", "source": "position_close", "symbol": "BTC/USDT:USDT",
                                  "realizedPnl": 5, "fee": 0.1}])
    ConsoleStore(path)  # re-open runs the purge
    remaining = [row[0] for row in sqlite3.connect(path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == ["gate:real"]
