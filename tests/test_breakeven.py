import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.console import ConsoleService
from core.engine import Engine
from core.symbols import SymbolMap
from state.store import StateStore


class BreakevenExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self, entry=100, mark=110, side="long", contracts=2, contract_size=1):
        self.entry = entry
        self.mark = mark
        self.side = side
        self.contracts = contracts
        self.contract_size = contract_size
        self.set_calls = []
        self.cancel_calls = []
        self.cancel_orders_calls = []

    async def fetch_position_detail(self, symbol):
        return {"contracts": self.contracts, "contractSize": self.contract_size,
                "entryPrice": self.entry, "side": self.side, "markPrice": self.mark}

    async def set_protection(self, symbol, side, amount, trigger):
        self.set_calls.append((symbol, side, amount, trigger))
        return "p1"

    async def cancel_protection(self, order_id, symbol):
        self.cancel_calls.append((order_id, symbol))

    async def cancel_protection_orders(self, symbol, keep_id=None):
        self.cancel_orders_calls.append((symbol, keep_id))


class BreakevenTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, exchange, stop=None, state_store=None):
        cfg = {"total_margin": 100}
        if stop is not None:
            cfg["position_stop_loss"] = stop
        engine = Engine(exchange, {"L": cfg}, state_store=state_store)
        engine.owners = {"__default__:BTC": "L"}
        return engine

    async def test_moves_stop_to_entry_without_configured_stop_loss(self):
        exchange = BreakevenExchange()
        engine = self.engine(exchange)

        trigger = await engine.set_breakeven("L", "BTC")

        self.assertEqual(trigger, 100)
        self.assertEqual(exchange.set_calls, [("BTC", "sell", 2.0, 100.0)])
        self.assertTrue(engine.breakeven["__default__:BTC"])

    async def test_short_stop_is_a_buy_at_entry(self):
        exchange = BreakevenExchange(entry=100, mark=90, side="short")
        engine = self.engine(exchange)

        await engine.set_breakeven("L", "BTC")

        self.assertEqual(exchange.set_calls, [("BTC", "buy", 2.0, 100.0)])

    async def test_stop_quantity_is_contracts_not_base_units(self):
        exchange = BreakevenExchange(entry=100, mark=110, contracts=300, contract_size=0.01)
        engine = self.engine(exchange)

        await engine.set_breakeven("L", "BTC")

        self.assertEqual(exchange.set_calls, [("BTC", "sell", 300.0, 100.0)])

    async def test_rejects_position_that_is_not_in_profit(self):
        exchange = BreakevenExchange(entry=100, mark=95)
        engine = self.engine(exchange)

        with self.assertRaises(ValueError):
            await engine.set_breakeven("L", "BTC")

        self.assertEqual(exchange.set_calls, [])
        self.assertNotIn("__default__:BTC", engine.breakeven)

    async def test_reconcile_keeps_the_breakeven_stop(self):
        exchange = BreakevenExchange()
        engine = self.engine(exchange)
        await engine.set_breakeven("L", "BTC")

        await engine.reconcile_protections()

        self.assertEqual(exchange.set_calls, [("BTC", "sell", 2.0, 100.0)])

    async def test_partial_close_rebuilds_breakeven_at_new_size(self):
        exchange = BreakevenExchange()
        engine = self.engine(exchange)
        await engine.set_breakeven("L", "BTC")

        exchange.contracts = 1
        await engine.reconcile_protections()

        self.assertEqual(exchange.set_calls[-1], ("BTC", "sell", 1.0, 100.0))

    async def test_clear_restores_the_configured_stop_loss(self):
        exchange = BreakevenExchange()
        engine = self.engine(exchange, {"enabled": True, "max_loss_usdt": 10})
        await engine.set_breakeven("L", "BTC")

        await engine.clear_breakeven("L", "BTC")

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertEqual(exchange.set_calls[-1], ("BTC", "sell", 2.0, 95.0))

    async def test_clear_without_configured_stop_cancels_the_order(self):
        exchange = BreakevenExchange()
        engine = self.engine(exchange)
        await engine.set_breakeven("L", "BTC")

        await engine.clear_breakeven("L", "BTC")

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertEqual(exchange.cancel_calls, [("p1", "BTC")])

    async def test_breakeven_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            exchange = BreakevenExchange()
            engine = self.engine(exchange, state_store=store)
            await engine.set_breakeven("L", "BTC")

            restarted = Engine(exchange, {"L": {"total_margin": 100}}, state_store=store)

            self.assertTrue(restarted.breakeven["__default__:BTC"])


class ConsoleEngine:
    def __init__(self):
        self.symbol_map = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
        self.owners = {"gate:XYZ:AMD": "L"}
        self.breakeven = {}
        self.calls = []

    async def set_breakeven(self, leader, symbol):
        self.calls.append(("set", leader, symbol))
        self.breakeven[f"gate:{symbol}"] = True
        return 2.5

    async def clear_breakeven(self, leader, symbol):
        self.calls.append(("clear", leader, symbol))
        self.breakeven.pop(f"gate:{symbol}", None)
        return True


class ConsoleBreakevenTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_maps_the_displayed_symbol_back_to_the_engine(self):
        engine = ConsoleEngine()
        with tempfile.TemporaryDirectory() as directory:
            service = ConsoleService(engine, {}, str(Path(directory) / "console.sqlite3"))
            app = web.Application()
            app.router.add_post("/api/v1/positions/breakeven", service.breakeven)
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    "/api/v1/positions/breakeven", json={"symbol": "XYZ:AMD", "enabled": True})
                body = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["trigger"], 2.5)
        self.assertEqual(engine.calls, [("set", "L", "XYZ:AMD")])

    async def test_endpoint_returns_400_when_the_engine_rejects_the_request(self):
        class RejectingEngine(ConsoleEngine):
            async def set_breakeven(self, leader, symbol):
                raise ValueError("position is not in profit")

        with tempfile.TemporaryDirectory() as directory:
            service = ConsoleService(RejectingEngine(), {}, str(Path(directory) / "console.sqlite3"))
            app = web.Application()
            app.router.add_post("/api/v1/positions/breakeven", service.breakeven)
            async with TestClient(TestServer(app)) as client:
                response = await client.post(
                    "/api/v1/positions/breakeven", json={"symbol": "XYZ:AMD", "enabled": True})
                text = await response.text()

        self.assertEqual(response.status, 400)
        self.assertEqual(text, "position is not in profit")


if __name__ == "__main__":
    unittest.main()
