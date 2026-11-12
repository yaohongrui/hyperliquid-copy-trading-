import asyncio
import sqlite3
import time
import types

from api.console import ConsoleService, ConsoleStore
from core.symbols import SymbolMap

ACCOUNT = "hl_primary"


def test_local_reduce_fill_attributes_realized_trade(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    store.insert_trades(ACCOUNT, [{"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell",
                                   "amount": 1, "price": 110, "realizedPnl": 12, "fee": 1,
                                   "timestamp": now}])
    store.attribute_trades(ACCOUNT)
    result = store.performance(ACCOUNT)
    assert result["net_pnl"] == 11
    assert result["leaders"][0]["leader"] == "A"


def test_stop_loss_close_attributes_to_opener_without_reduce_fill(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "B", "order_id": "o1", "symbol": "ETH/USDT:USDT",
        "side": "buy", "quantity": 2, "price": 100, "reduce_only": False, "timestamp": now - 3600,
    }])
    # Native stop fires on the exchange: no local reduce-only fill exists.
    store.insert_trades(ACCOUNT, [{"id": "t1", "symbol": "ETH/USDT:USDT", "side": "sell",
                                   "amount": 2, "price": 90, "realizedPnl": -20, "fee": 1,
                                   "timestamp": now}])
    store.attribute_trades(ACCOUNT)
    result = store.performance(ACCOUNT)
    assert result["leaders"][0]["leader"] == "B"
    assert result["leaders"][0]["net_pnl"] == -21
    row = sqlite3.connect(store.path).execute("SELECT attribution FROM trades").fetchone()
    assert row[0] == "stop_close"


def test_cash_flow_adjusted_drawdown(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.snapshot(ACCOUNT, 100, 0)
    store.insert_cash_flows(ACCOUNT, [{"id": "d1", "kind": "deposit", "amount": 100, "timestamp": time.time()}])
    store.snapshot(ACCOUNT, 200, 0)
    result = store.performance(ACCOUNT)
    assert result["cash_flow"] == 100
    assert result["max_drawdown"] >= 0


def test_flow_adjusted_curve_removes_deposits(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    # Keep the snapshots well apart so the downsampler keeps both points.
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                   (ACCOUNT, 100, 0, now - 7200))
        db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                   (ACCOUNT, 220, 0, now))  # 100 start + 100 deposit + 20 trading profit
    store.insert_cash_flows(ACCOUNT, [{"id": "d1", "kind": "deposit", "amount": 100,
                                       "timestamp": now - 3600}])

    result = store.performance(ACCOUNT)
    curve = result["curve"]
    assert curve[0]["adjusted_equity"] == 100
    assert curve[-1]["adjusted_equity"] == 120
    assert result["return_pct"] == 20


def test_cash_flow_millisecond_timestamps_are_normalized(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.snapshot(ACCOUNT, 100, 0)
    now = time.time()
    store.insert_cash_flows(ACCOUNT, [{"id": "d1", "kind": "deposit", "amount": 50,
                                       "timestamp": (now - 5) * 1000}])
    stored = sqlite3.connect(store.path).execute("SELECT timestamp FROM cash_flows").fetchone()[0]
    assert stored < 1e10
    assert abs(stored - (now - 5)) < 0.01

    result = store.performance(ACCOUNT)
    assert result["cash_flow"] == 50
    # The deposit is dated before the snapshot, so the curve subtracts it.
    assert result["curve"][0]["adjusted_equity"] == 50


def test_millisecond_trade_timestamps_are_normalized_and_attributed(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    # Exchanges report milliseconds; local fills use seconds.
    store.insert_trades(ACCOUNT, [{"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell",
                                   "amount": 1, "price": 110, "realizedPnl": 12, "fee": 1,
                                   "timestamp": now * 1000}])
    store.attribute_trades(ACCOUNT)
    result = store.performance(ACCOUNT)
    assert result["leaders"][0]["leader"] == "A"
    assert result["leaders"][0]["net_pnl"] == 11
    assert store.performance(ACCOUNT, since=now - 60, until=now + 60)["trades"] == 1


def test_existing_millisecond_rows_are_backfilled_on_open(tmp_path):
    path = tmp_path / "console.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, account TEXT, exchange_trade_id TEXT UNIQUE,"
               " symbol TEXT, side TEXT, quantity REAL, price REAL, realized_pnl REAL, fee REAL, timestamp REAL)")
    db.execute("INSERT INTO trades(account,symbol,timestamp) VALUES(?,'BTC/USDT:USDT',1792221486709)", (ACCOUNT,))
    db.commit()
    db.close()
    ConsoleStore(path)  # constructor runs the in-place migration
    got = sqlite3.connect(path).execute("SELECT timestamp FROM trades").fetchone()[0]
    assert got < 1e10
    assert abs(got - 1792221486.709) < 0.001


def test_existing_database_gains_the_order_id_column(tmp_path):
    path = tmp_path / "console.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, account TEXT, exchange_trade_id TEXT UNIQUE,"
               " symbol TEXT, side TEXT, quantity REAL, price REAL, realized_pnl REAL, fee REAL, timestamp REAL)")
    db.execute("INSERT INTO trades(account,symbol,timestamp) VALUES(?,?,?)", (ACCOUNT, "BTC/USDT:USDT", 100))
    db.commit()
    db.close()

    store = ConsoleStore(path)  # migration adds the missing columns in place
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(trades)")}
    assert "order_id" in columns
    assert store.latest_timestamp(ACCOUNT, "trades") == 100


def test_funding_rows_do_not_count_as_trades_or_wins(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_trades(ACCOUNT, [
        {"id": "t1", "symbol": "BTC/USDT:USDT", "realizedPnl": 10, "fee": 1,
         "source": "hl_fill", "timestamp": now - 20},
        {"id": "f1", "symbol": "BTC/USDT:USDT", "realizedPnl": 0, "funding": 2,
         "source": "hl_funding", "timestamp": now - 10},
        {"id": "t2", "symbol": "ETH/USDT:USDT", "realizedPnl": -4, "fee": 1,
         "source": "hl_fill", "timestamp": now},
    ])
    result = store.performance(ACCOUNT)

    assert result["trades"] == 2  # funding row is not a trade
    assert result["funding"] == 2
    assert result["net_pnl"] == 10 - 1 + 2 - 4 - 1
    leader = result["leaders"][0]
    assert leader["leader"] == "unattributed"
    assert leader["trades"] == 2
    assert leader["wins"] == 1  # only t1 nets positive
    assert leader["win_rate"] == 0.5
    assert leader["funding"] == 2  # but funding still rolls into the aggregate


def test_zero_pnl_opening_fills_are_not_counted_as_trades(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_trades(ACCOUNT, [
        # HL returns every fill; the entry realizes no PnL but still pays a fee.
        {"id": "open", "symbol": "BTC/USDT:USDT", "realizedPnl": 0, "fee": 0.5,
         "source": "hl_fill", "timestamp": now - 10},
        {"id": "close", "symbol": "BTC/USDT:USDT", "realizedPnl": 10, "fee": 0.5,
         "source": "hl_fill", "timestamp": now},
    ])
    result = store.performance(ACCOUNT)

    assert result["trades"] == 1
    leader = result["leaders"][0]
    assert leader["trades"] == 1
    assert leader["wins"] == 1
    assert leader["win_rate"] == 1
    # Both rows still move the money totals: the open contributes its fee.
    assert result["gross_pnl"] == 10
    assert result["fees"] == 1
    assert result["net_pnl"] == 9
    assert leader["net_pnl"] == 9


def test_latest_timestamp_is_scoped_by_account_and_source(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.insert_trades(ACCOUNT, [
        {"id": "f1", "symbol": "BTC/USDT:USDT", "source": "hl_fill", "timestamp": 100},
        {"id": "fund", "symbol": "BTC/USDT:USDT", "source": "hl_funding", "timestamp": 900},
    ])
    store.insert_cash_flows(ACCOUNT, [{"id": "d1", "kind": "deposit", "amount": 5, "timestamp": 500}])

    assert store.latest_timestamp(ACCOUNT, "trades") == 900
    assert store.latest_timestamp(ACCOUNT, "trades", ("hl_fill",)) == 100
    assert store.latest_timestamp(ACCOUNT, "cash_flows") == 500
    assert store.latest_timestamp("other", "trades") is None
    assert store.latest_timestamp("other", "cash_flows") is None


def test_position_close_rows_are_purged_in_favor_of_ledger(tmp_path):
    path = tmp_path / "console.sqlite3"
    store = ConsoleStore(path)
    store.insert_trades(ACCOUNT, [{"id": "old", "source": "position_close", "symbol": "SKHY/USDT:USDT",
                                   "realizedPnl": -6, "fee": 0.2}])
    store.insert_trades(ACCOUNT, [{"id": "new", "source": "ledger", "symbol": "SKHY/USDT:USDT",
                                   "realizedPnl": -6, "fee": 0.09}])
    ConsoleStore(path)  # re-open runs the purge
    remaining = [row[0] for row in sqlite3.connect(path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == [f"{ACCOUNT}:new"]


def test_ledger_insert_retires_fallback_rows_without_restart(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    store.insert_trades(ACCOUNT, [{"id": "fallback", "source": "position_close",
                                   "symbol": "SKHY/USDT:USDT", "realizedPnl": -6, "fee": 0.2}])
    store.insert_trades(ACCOUNT, [{"id": "ledger", "source": "ledger",
                                   "symbol": "SKHY/USDT:USDT", "realizedPnl": -6, "fee": 0.09}])
    remaining = [row[0] for row in sqlite3.connect(store.path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == [f"{ACCOUNT}:ledger"]


def test_stop_close_attributes_to_latest_opening_fill(tmp_path):
    """A stop-out closes the current episode; a reopened position must not
    steal the attribution of the earlier close."""
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_local_fills([
        {"account": ACCOUNT, "leader": "A", "order_id": "o1", "symbol": "SKHY/USDT:USDT",
         "side": "buy", "quantity": 136, "price": 136, "reduce_only": False, "timestamp": now - 100},
        # The account reopened after the stop fired (leader still long).
        {"account": ACCOUNT, "leader": "A", "order_id": "o2", "symbol": "SKHY/USDT:USDT",
         "side": "buy", "quantity": 11, "price": 128, "reduce_only": False, "timestamp": now + 100},
    ])
    # The ledger row for the stop-close sits between the two episodes.
    store.insert_trades(ACCOUNT, [{"id": "ledger:SKHY_USDT:x", "source": "ledger", "symbol": "SKHY/USDT:USDT",
                                   "side": "sell", "amount": 136, "price": 128.84,
                                   "realizedPnl": -6, "fee": 0.09, "timestamp": now}])
    store.attribute_trades(ACCOUNT)
    result = store.performance(ACCOUNT)
    assert result["leaders"][0]["leader"] == "A"
    assert abs(result["leaders"][0]["net_pnl"] - (-6 - 0.09)) < 1e-9
    row = sqlite3.connect(store.path).execute("SELECT attribution FROM trades").fetchone()
    assert row[0] == "stop_close"


def test_legacy_fee_only_rows_are_purged_on_open(tmp_path):
    path = tmp_path / "console.sqlite3"
    store = ConsoleStore(path)
    store.insert_trades(ACCOUNT, [{"id": "junk", "symbol": "BTC/USDT:USDT", "realizedPnl": 0, "fee": 1}])
    store.insert_trades(ACCOUNT, [{"id": "real", "source": "ledger", "symbol": "BTC/USDT:USDT",
                                   "realizedPnl": 5, "fee": 0.1}])
    ConsoleStore(path)  # re-open runs the purge
    remaining = [row[0] for row in sqlite3.connect(path).execute("SELECT exchange_trade_id FROM trades")]
    assert remaining == [f"{ACCOUNT}:real"]


def test_snapshot_retention_prunes_only_old_rows(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                   (ACCOUNT, 1, 0, now - 100 * 86400))
    store.snapshot(ACCOUNT, 2, 0)
    assert store.prune(retention_days=90, now=now) == 1
    assert [row["equity"] for row in store.snapshots(ACCOUNT)] == [2]


def test_corrected_realized_pnl_updates_an_existing_row(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_trades(ACCOUNT, [{
        "id": "1", "symbol": "BTC/USDT:USDT", "realizedPnl": 2.5,
        "fee": 0.1, "source": "hl_fill", "timestamp": now,
    }])
    # A later refresh from the fixed parser reports the same trade as a loss.
    store.insert_trades(ACCOUNT, [{
        "id": "1", "symbol": "BTC/USDT:USDT", "realizedPnl": -2.5,
        "fee": 0.1, "source": "hl_fill", "timestamp": now,
    }])

    row = sqlite3.connect(store.path).execute(
        "SELECT realized_pnl FROM trades WHERE exchange_trade_id=?", (f"{ACCOUNT}:1",)).fetchone()
    assert row[0] == -2.5
    assert store.performance(ACCOUNT)["net_pnl"] == -2.6


def test_upsert_fills_in_a_late_order_id(tmp_path):
    store = ConsoleStore(tmp_path / "console.sqlite3")
    now = time.time()
    store.insert_trades(ACCOUNT, [{"id": "1", "symbol": "BTC/USDT:USDT",
                                   "realizedPnl": 2.5, "fee": 0.1, "source": "hl_fill",
                                   "timestamp": now}])
    # A later mapping supplies the order id; the existing row must pick it up.
    store.insert_trades(ACCOUNT, [{"id": "1", "symbol": "BTC/USDT:USDT",
                                   "realizedPnl": 2.5, "fee": 0.1, "source": "hl_fill",
                                   "order_id": "42", "timestamp": now}])
    row = sqlite3.connect(store.path).execute(
        "SELECT order_id FROM trades WHERE exchange_trade_id=?", (f"{ACCOUNT}:1",)).fetchone()
    assert row[0] == "42"


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.flow_calls = []

    async def fetch_realized_pnl(self, since=None, until=None, lookback_days=30, page_size=2000):
        self.calls.append({"since": since, "until": until,
                           "lookback_days": lookback_days, "page_size": page_size})
        return [{"id": f"t{len(self.calls)}", "symbol": "BTC/USDT:USDT",
                 "realizedPnl": 5, "fee": 0.1, "source": "hl_fill",
                 "order_id": str(len(self.calls)), "timestamp": time.time()}]

    async def fetch_cash_flows(self, since=None, until=None, lookback_days=30, page_size=2000):
        self.flow_calls.append(since)
        return []


class LegacyExchange:
    """Venue adapter from before the windowed signature existed."""

    async def fetch_realized_pnl(self):
        return [{"id": "legacy", "symbol": "BTC/USDT:USDT", "realizedPnl": 1,
                 "fee": 0, "source": "hl_fill", "timestamp": time.time()}]


def _service(tmp_path, exchange):
    engine = types.SimpleNamespace(
        symbol_map=SymbolMap(), exchange=exchange,
        leaders={"A": {"enabled": True, "execution_account": ACCOUNT}},
        fill_events=[],
    )
    return ConsoleService(engine, {"console": {
        "performance_lookback_days": 7,
        "performance_overlap_seconds": 120,
        "performance_page_size": 500,
    }}, str(tmp_path / "console.sqlite3"))


def test_performance_refresh_backfills_then_follows_the_watermark(tmp_path):
    exchange = FakeExchange()
    service = _service(tmp_path, exchange)
    now = time.time()

    async def run():
        await service._refresh_performance(1000, 0)
        first = exchange.calls[0]
        assert now - 8 * 86400 < first["since"] < now - 6 * 86400
        assert first["until"] is None
        assert first["lookback_days"] == 7
        assert first["page_size"] == 500

        watermark = service.store.latest_timestamp(ACCOUNT, "trades", ("hl_fill", "hl_funding"))
        await service._refresh_performance(1000, 0)
        assert abs(exchange.calls[1]["since"] - (watermark - 120)) < 1e-6

    asyncio.run(run())


def test_performance_refresh_runs_in_the_background_guarded_by_busy(tmp_path):
    exchange = FakeExchange()
    service = _service(tmp_path, exchange)

    async def run():
        service._start_performance(1000, 0)
        assert service._performance_busy
        await service._performance_task
        assert not service._performance_busy
        assert exchange.calls

    asyncio.run(run())


def test_performance_refresh_supports_a_legacy_no_arg_feed(tmp_path):
    service = _service(tmp_path, LegacyExchange())

    async def run():
        await service._refresh_performance(1000, 0)

    asyncio.run(run())
    assert service.store.latest_timestamp(ACCOUNT, "trades", ("hl_fill",)) is not None
