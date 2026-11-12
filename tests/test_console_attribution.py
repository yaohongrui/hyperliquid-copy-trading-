"""Trade attribution for Hyperliquid fills.

Three layers, tried in order: exact ``order_id`` match, reduce-only fill by
symbol/time, then the newest opening fill for exchange-side closes. Funding
bookkeeping rows must never consume a close fill.
"""

import sqlite3
import time

from api.console import ConsoleStore

ACCOUNT = "hl_primary"


def _store(tmp_path):
    return ConsoleStore(tmp_path / "console.sqlite3")


def _row(path, trade_id):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT leader, attribution FROM trades WHERE exchange_trade_id=?",
                     (f"{ACCOUNT}:{trade_id}",)).fetchone()
    db.close()
    return dict(row)


def test_order_id_match_beats_the_time_fallback(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([
        # Same symbol and timestamp as the trade; the old reduce-fill fallback
        # would pick whichever fill it saw first.
        {"account": ACCOUNT, "leader": "A", "order_id": "101", "symbol": "BTC/USDT:USDT",
         "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now},
        {"account": ACCOUNT, "leader": "B", "order_id": "202", "symbol": "BTC/USDT:USDT",
         "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now},
    ])
    store.insert_trades(ACCOUNT, [{
        "id": "t1", "order_id": "202", "symbol": "BTC/USDT:USDT", "side": "sell",
        "amount": 1, "price": 110, "realizedPnl": 5, "fee": 0, "source": "hl_fill",
        "timestamp": now,
    }])

    store.attribute_trades(ACCOUNT)

    row = _row(store.path, "t1")
    assert row == {"leader": "B", "attribution": "order_id"}


def test_order_id_is_normalized_from_oid(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "A", "order_id": "777", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    # Hyperliquid reports the order id as an integer ``oid``.
    store.insert_trades(ACCOUNT, [{
        "id": "t1", "oid": 777, "symbol": "BTC/USDT:USDT", "side": "sell",
        "amount": 1, "price": 110, "realizedPnl": 5, "fee": 0, "source": "hl_fill",
        "timestamp": now,
    }])

    store.attribute_trades(ACCOUNT)

    assert _row(store.path, "t1") == {"leader": "A", "attribution": "order_id"}


def test_reduce_fill_fallback_still_matches_symbol_and_time(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True,
        "timestamp": now - 30,
    }])
    # No order id (for example a row from the pre-order-id feed).
    store.insert_trades(ACCOUNT, [{
        "id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell", "amount": 1,
        "price": 110, "realizedPnl": 5, "fee": 0, "source": "hl_fill",
        "timestamp": now,
    }])

    store.attribute_trades(ACCOUNT)

    assert _row(store.path, "t1") == {"leader": "A", "attribution": "local_fill"}


def test_stop_close_fallback_uses_the_opening_fill(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "C", "order_id": "o1", "symbol": "ETH/USDT:USDT",
        "side": "buy", "quantity": 2, "price": 100, "reduce_only": False,
        "timestamp": now - 600,
    }])
    # Native stop: no local reduce-only fill and no order id.
    store.insert_trades(ACCOUNT, [{
        "id": "t1", "symbol": "ETH/USDT:USDT", "side": "sell", "amount": 2,
        "price": 90, "realizedPnl": -20, "fee": 1, "source": "hl_fill",
        "timestamp": now,
    }])

    store.attribute_trades(ACCOUNT)

    assert _row(store.path, "t1") == {"leader": "C", "attribution": "stop_close"}


def test_funding_rows_do_not_consume_close_fills(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "A", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "sell", "quantity": 1, "price": 110, "reduce_only": True, "timestamp": now,
    }])
    # The funding row is inserted first, so a symbol/time matcher that did not
    # skip it would claim the reduce fill and starve the real close.
    store.insert_trades(ACCOUNT, [
        {"id": "f1", "symbol": "BTC/USDT:USDT", "funding": 0.25, "source": "hl_funding",
         "timestamp": now},
        {"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell", "amount": 1,
         "price": 110, "realizedPnl": 5, "fee": 0, "source": "hl_fill", "timestamp": now},
    ])

    store.attribute_trades(ACCOUNT)

    assert _row(store.path, "f1") == {"leader": None, "attribution": "unattributed"}
    assert _row(store.path, "t1") == {"leader": "A", "attribution": "local_fill"}


def test_funding_rows_do_not_consume_the_stop_close_opener(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.insert_local_fills([{
        "account": ACCOUNT, "leader": "B", "order_id": "o1", "symbol": "BTC/USDT:USDT",
        "side": "buy", "quantity": 1, "price": 100, "reduce_only": False,
        "timestamp": now - 60,
    }])
    store.insert_trades(ACCOUNT, [
        {"id": "f1", "symbol": "BTC/USDT:USDT", "funding": -0.1, "source": "hl_funding",
         "timestamp": now - 120},
        {"id": "t1", "symbol": "BTC/USDT:USDT", "side": "sell", "amount": 1,
         "price": 95, "realizedPnl": -5, "fee": 0, "source": "hl_fill", "timestamp": now},
    ])

    store.attribute_trades(ACCOUNT)

    assert _row(store.path, "f1")["leader"] is None
    assert _row(store.path, "t1") == {"leader": "B", "attribution": "stop_close"}
