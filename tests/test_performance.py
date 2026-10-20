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


def test_stop_loss_close_attributes_to_opener_without_reduce_fill(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": "gate", "leader": "B", "order_id": "o1", "symbol": "ETH/USDT:USDT",
        "side": "buy", "quantity": 2, "price": 100, "reduce_only": False, "timestamp": now - 3600,
    }])
    # Native stop fires on the exchange: no local reduce-only fill exists.
    store.insert_trades("gate", [{"id": "t1", "symbol": "ETH/USDT:USDT", "side": "sell",
                                  "amount": 2, "price": 90, "realizedPnl": -20, "fee": 1,
                                  "timestamp": now}])
    store.attribute_trades("gate")
    result = store.performance("gate")
    assert result["leaders"][0]["leader"] == "B"
    assert result["leaders"][0]["net_pnl"] == -21
    row = sqlite3.connect(store.path).execute("SELECT attribution FROM trades").fetchone()
    assert row[0] == "stop_close"


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


def test_gate_ledger_rows_group_pnl_and_fee_per_order():
    from exchanges.ccxt_exchange import CcxtExchange

    exchange = CcxtExchange.__new__(CcxtExchange)
    exchange.config = {"settle": "usdt"}

    class Client:
        async def privateFuturesGetSettleAccountBook(self, params):
            if params.get("to"):
                return []  # single page
            return [
                # A native stop-close: pnl arrives as two legs on one order,
                # fees as negative changes sharing the same text.
                {"type": "pnl", "change": "-3.883905882353", "text": "SKHY_USDT:388998418491372149", "time": 1792272473.98},
                {"type": "fee", "change": "-0.0566852", "text": "SKHY_USDT:388998418491372149", "time": 1792272473.98},
                {"type": "pnl", "change": "-2.113694117647", "text": "SKHY_USDT:388998418491372149", "time": 1792272473.98},
                {"type": "fee", "change": "-0.0309216", "text": "SKHY_USDT:388998418491372149", "time": 1792272473.98},
                # Opening fill: fee only, no closing leg -> must be dropped.
                {"type": "fee", "change": "-0.0071071", "text": "SKHY_USDT:388998418491369420", "time": 1792272468.77},
                # Funding and deposits carry no order id -> ignored here.
                {"type": "fund", "change": "-0.5", "text": "SKHY_USDT", "time": 1792272468.77},
            ]

        markets = {"SKHY/USDT:USDT": {"id": "SKHY_USDT", "swap": True}}

    exchange.client = Client()

    import asyncio
    rows = asyncio.run(exchange._gate_ledger_rows())
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "ledger:SKHY_USDT:388998418491372149"
    assert row["symbol"] == "SKHY/USDT:USDT"
    assert abs(row["realizedPnl"] - (-5.9976)) < 1e-6
    assert abs(row["fee"] - 0.0876068) < 1e-6
    assert row["timestamp"] == 1792272473.98


def test_gate_ledger_rows_fall_back_when_unsupported():
    import ccxt.async_support as ccxt
    from exchanges.ccxt_exchange import CcxtExchange

    exchange = CcxtExchange.__new__(CcxtExchange)
    exchange.config = {"settle": "usdt"}

    class Client:
        markets = {}

        async def privateFuturesGetSettleAccountBook(self, params):
            raise ccxt.NotSupported("nope")

    exchange.client = Client()

    import asyncio
    assert asyncio.run(exchange._gate_ledger_rows()) is None


def test_position_close_rows_are_purged_in_favor_of_ledger(tmp_path):
    path = tmp_path / "console.sqlite3"
    store = ConsoleStore(path)
    store.insert_trades("gate", [{"id": "old", "source": "position_close", "symbol": "SKHY/USDT:USDT",
                                  "realizedPnl": -6, "fee": 0.2}])
    store.insert_trades("gate", [{"id": "new", "source": "ledger", "symbol": "SKHY/USDT:USDT",
                                  "realizedPnl": -6, "fee": 0.09}])
    ConsoleStore(path)  # re-open runs the purge
    remaining = [row[0] for row in sqlite3.connect(path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == ["gate:new"]


def test_ledger_insert_retires_fallback_rows_without_restart(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.insert_trades("gate", [{"id": "fallback", "source": "position_close",
                                  "symbol": "SKHY/USDT:USDT", "realizedPnl": -6, "fee": 0.2}])
    store.insert_trades("gate", [{"id": "ledger", "source": "ledger",
                                  "symbol": "SKHY/USDT:USDT", "realizedPnl": -6, "fee": 0.09}])
    remaining = [row[0] for row in sqlite3.connect(store.path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == ["gate:ledger"]


def test_stop_close_attributes_to_latest_opening_fill(tmp_path):
    """A stop-out closes the current episode; a reopened position must not
    steal the attribution of the earlier close."""
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([
        {"account": "gate", "leader": "A", "order_id": "o1", "symbol": "SKHY/USDT:USDT",
         "side": "buy", "quantity": 136, "price": 136, "reduce_only": False, "timestamp": now - 100},
        # The account reopened after the stop fired (leader still long).
        {"account": "gate", "leader": "A", "order_id": "o2", "symbol": "SKHY/USDT:USDT",
         "side": "buy", "quantity": 11, "price": 128, "reduce_only": False, "timestamp": now + 100},
    ])
    # The ledger row for the stop-close sits between the two episodes.
    store.insert_trades("gate", [{"id": "ledger:SKHY_USDT:x", "source": "ledger", "symbol": "SKHY/USDT:USDT",
                                  "side": "sell", "amount": 136, "price": 128.84,
                                  "realizedPnl": -6, "fee": 0.09, "timestamp": now}])
    store.attribute_trades("gate")
    result = store.performance("gate")
    assert result["leaders"][0]["leader"] == "A"
    assert abs(result["leaders"][0]["net_pnl"] - (-6 - 0.09)) < 1e-9
    row = sqlite3.connect(store.path).execute("SELECT attribution FROM trades").fetchone()
    assert row[0] == "stop_close"


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
    store.insert_trades("gate", [{"id": "real", "source": "ledger", "symbol": "BTC/USDT:USDT",
                                  "realizedPnl": 5, "fee": 0.1}])
    ConsoleStore(path)  # re-open runs the purge
    remaining = [row[0] for row in sqlite3.connect(path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == ["gate:real"]


def test_snapshot_retention_prunes_only_old_rows(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                   ("gate", 1, 0, now - 100 * 86400))
    store.snapshot("gate", 2, 0)
    assert store.prune(retention_days=90, now=now) == 1
    assert [row["equity"] for row in store.snapshots("gate")] == [2]


def test_gate_ledger_incremental_uses_watermark():
    import asyncio
    from exchanges.ccxt_exchange import CcxtExchange

    exchange = CcxtExchange.__new__(CcxtExchange)
    exchange.config = {"settle": "usdt"}
    calls = []

    class Client:
        markets = {"SKHY/USDT:USDT": {"id": "SKHY_USDT", "swap": True}}

        async def privateFuturesGetSettleAccountBook(self, params):
            calls.append(dict(params))
            return [{"type": "pnl", "change": "1", "text": "SKHY_USDT:1", "time": 1792272473.98}]

    exchange.client = Client()
    asyncio.run(exchange._gate_ledger_rows())
    asyncio.run(exchange._gate_ledger_rows())
    assert "from" not in calls[0]
    # Second call only asks for the recent overlap, not the full history.
    assert calls[1]["from"] == 1792272473 - 300
