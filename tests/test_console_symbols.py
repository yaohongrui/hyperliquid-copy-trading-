import sqlite3
import types

from api.console import ConsoleService, ConsoleStore
from core.symbols import SymbolMap


def test_store_maps_symbols_at_read_time(tmp_path):
    mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
    store = ConsoleStore(tmp_path / "console.sqlite3", mapping)
    store.insert_trades("gate", [{
        "id": "1", "symbol": "AMD/USDT:USDT", "side": "buy", "amount": 1,
        "price": 2, "realizedPnl": 0.5, "fee": 0.1, "timestamp": 1,
    }])
    assert store.trades("gate")[0]["symbol"] == "XYZ:AMD"
    assert store.performance("gate")["items"][0]["symbol"] == "XYZ:AMD"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT symbol FROM trades").fetchone()[0] == "AMD/USDT:USDT"


def test_console_service_shares_engine_symbol_map(tmp_path):
    mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
    engine = types.SimpleNamespace(symbol_map=mapping)
    service = ConsoleService(engine, {}, str(tmp_path / "console.sqlite3"))
    assert service.symbol_map is mapping
    assert service.store.symbol_map is mapping


def test_attribute_trades_matches_hl_and_venue_symbols(tmp_path):
    mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
    store = ConsoleStore(tmp_path / "console.sqlite3", mapping)
    store.insert_local_fills([{
        "account": "gate", "leader": "L", "order_id": "1", "symbol": "XYZ:AMD",
        "side": "sell", "quantity": 1, "price": 2, "reduce_only": True, "timestamp": 100,
    }])
    store.insert_trades("gate", [{
        "id": "2", "symbol": "AMD/USDT:USDT", "side": "sell", "amount": 1,
        "price": 2, "realizedPnl": 0.5, "fee": 0.1, "timestamp": 101,
    }])
    store.attribute_trades("gate")
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT leader FROM trades").fetchone()[0] == "L"
