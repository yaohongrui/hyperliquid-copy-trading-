import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from aiohttp import web

from sources.position_queries import _number, _roi, _symbol, leader_snapshot

log = logging.getLogger(__name__)


class ConsoleStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
              id INTEGER PRIMARY KEY, account TEXT, equity REAL, unrealized_pnl REAL, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS trades (
              id INTEGER PRIMARY KEY, account TEXT, exchange_trade_id TEXT UNIQUE,
              symbol TEXT, side TEXT, quantity REAL, price REAL, realized_pnl REAL,
              fee REAL, funding REAL DEFAULT 0, leader TEXT, attribution TEXT DEFAULT 'unattributed',
              source TEXT, timestamp REAL
            );
            CREATE TABLE IF NOT EXISTS cash_flows (
              id INTEGER PRIMARY KEY, account TEXT, kind TEXT, amount REAL, timestamp REAL, external_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS local_fills (
              id INTEGER PRIMARY KEY, account TEXT, leader TEXT, order_id TEXT, symbol TEXT, side TEXT,
              quantity REAL, price REAL, reduce_only INTEGER DEFAULT 0, timestamp REAL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
              id INTEGER PRIMARY KEY, action TEXT, detail TEXT, created_at REAL
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            for name, definition in (("funding", "REAL DEFAULT 0"), ("leader", "TEXT"), ("attribution", "TEXT DEFAULT 'unattributed'"), ("source", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE trades ADD COLUMN {name} {definition}")
            # Exchange trade rows arrived with millisecond timestamps while local
            # fills and query windows use seconds, so attribution and time filters
            # never matched. Normalize in place; idempotent because only clearly
            # millisecond values are rescaled.
            db.execute("UPDATE trades SET timestamp = timestamp / 1000.0 WHERE timestamp > 10000000000")
            # Rows written before realized PnL was sourced correctly come from a
            # feed that reports fee only (no PnL); keeping them would double-count
            # fees once the real source starts writing.
            db.execute("DELETE FROM trades WHERE source IS NULL AND realized_pnl = 0")

    def snapshot(self, account, equity, unrealized):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                       (account, equity, unrealized, time.time()))

    def trades(self, account, limit=100):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM trades WHERE account=? ORDER BY timestamp DESC LIMIT ?", (account, limit)
            )]

    def insert_trades(self, account, rows):
        with sqlite3.connect(self.path) as db:
            for row in rows:
                external_id = row.get("id") or row.get("tradeId") or row.get("timestamp")
                if external_id is None:
                    continue
                timestamp = row.get("timestamp") or time.time()
                if timestamp > 1e10:
                    timestamp = timestamp / 1000.0
                db.execute("""INSERT OR IGNORE INTO trades
                    (account,exchange_trade_id,symbol,side,quantity,price,realized_pnl,fee,funding,source,timestamp)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (account, f"{account}:{external_id}", row.get("symbol"), row.get("side"),
                    row.get("amount"), row.get("price"), row.get("realizedPnl", row.get("realized_pnl", 0)),
                    row.get("fee", 0), row.get("funding", 0), row.get("source"), timestamp))

    def performance(self, account=None, since=None, until=None):
        """Return flow-adjusted account and trader aggregates for the console."""
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            where, args = [], []
            if account:
                where.append("account=?"); args.append(account)
            if since is not None:
                where.append("timestamp>=?"); args.append(float(since))
            if until is not None:
                where.append("timestamp<=?"); args.append(float(until))
            clause = " WHERE " + " AND ".join(where) if where else ""
            rows = [dict(x) for x in db.execute("SELECT * FROM trades" + clause + " ORDER BY timestamp", args)]
            flows = [dict(x) for x in db.execute("SELECT * FROM cash_flows" + clause, args)] if clause else [dict(x) for x in db.execute("SELECT * FROM cash_flows")]
            snaps = self._downsampled_snapshots(db, clause, args)
        gross = sum(_number(x.get("realized_pnl")) for x in rows)
        fees = sum(_number(x.get("fee")) for x in rows)
        funding = sum(_number(x.get("funding")) for x in rows)
        net = gross - fees + funding
        by_leader = {}
        for row in rows:
            key = row.get("leader") or "unattributed"
            item = by_leader.setdefault(key, {"leader": key, "gross_pnl": 0, "fees": 0, "funding": 0, "net_pnl": 0, "trades": 0, "wins": 0})
            value = _number(row.get("realized_pnl")); fee = _number(row.get("fee")); fund = _number(row.get("funding"))
            item["gross_pnl"] += value; item["fees"] += fee; item["funding"] += fund; item["net_pnl"] += value - fee + fund; item["trades"] += 1; item["wins"] += value - fee + fund > 0
        for item in by_leader.values():
            item["win_rate"] = item["wins"] / item["trades"] if item["trades"] else 0
            item["contribution_pct"] = item["net_pnl"] / net * 100 if net else 0
        curve = []
        return_pct = 0
        peak = None; max_dd = 0; max_dd_pct = 0
        for snap in sorted(snaps, key=lambda x: x.get("created_at") or 0):
            equity = _number(snap.get("equity")); flow = sum(_number(x.get("amount")) for x in flows if _number(x.get("timestamp")) <= _number(snap.get("created_at")))
            adjusted = equity - flow
            peak = adjusted if peak is None else max(peak, adjusted)
            dd = peak - adjusted
            max_dd = max(max_dd, dd); max_dd_pct = max(max_dd_pct, dd / peak * 100 if peak else 0)
            curve.append({"timestamp": snap.get("created_at"), "equity": equity, "adjusted_equity": adjusted, "drawdown": dd})
        if len(curve) >= 2 and curve[0]["adjusted_equity"]:
            return_pct = (curve[-1]["adjusted_equity"] - curve[0]["adjusted_equity"]) / abs(curve[0]["adjusted_equity"]) * 100
        return {"account": account, "gross_pnl": gross, "fees": fees, "funding": funding, "net_pnl": net, "return_pct": return_pct,
                "cash_flow": sum(_number(x.get("amount")) for x in flows), "trades": len(rows),
                "leaders": sorted(by_leader.values(), key=lambda x: x["net_pnl"], reverse=True),
                "window": {"since": since, "until": until},
                "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct, "curve": curve, "items": rows}

    @staticmethod
    def _downsampled_snapshots(db, clause, args, points=400):
        """Bucket the equity curve to at most ``points`` samples.

        Snapshots are written every few seconds, so a long window would
        otherwise pull hundreds of thousands of rows into memory per request.
        Each bucket keeps its most recent row (highest id).
        """
        where = clause.replace("timestamp", "created_at")
        bounds = db.execute("SELECT MIN(created_at),MAX(created_at) FROM equity_snapshots" + where, args).fetchone()
        span = (bounds[1] or 0) - (bounds[0] or 0)
        bucket = max(60.0, span / points) if span else 60.0
        inner = "SELECT MAX(id) FROM equity_snapshots" + where + " GROUP BY CAST(created_at / ? AS INTEGER)"
        return [dict(x) for x in db.execute(
            "SELECT * FROM equity_snapshots WHERE id IN (" + inner + ") ORDER BY created_at", args + [bucket])]

    def latest_open_fill_leaders(self, account):
        """Map symbol -> leader from each symbol's newest non-reduce local fill."""
        with sqlite3.connect(self.path) as db:
            return {str(row[0]).upper(): row[1] for row in db.execute(
                "SELECT symbol, leader, MAX(timestamp) FROM local_fills WHERE account=? AND reduce_only=0 GROUP BY symbol",
                (account,))}

    def snapshots(self, account, limit=240):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM equity_snapshots WHERE account=? ORDER BY created_at DESC LIMIT ?", (account, limit)
            )]

    def insert_cash_flows(self, account, rows):
        with sqlite3.connect(self.path) as db:
            for row in rows:
                external_id = row.get("id") or f"{row.get('kind')}:{row.get('timestamp')}:{row.get('amount')}"
                db.execute("INSERT OR IGNORE INTO cash_flows(account,kind,amount,timestamp,external_id) VALUES(?,?,?,?,?)",
                           (account, row.get("kind"), row.get("amount", 0), row.get("timestamp") or time.time(), f"{account}:{external_id}"))

    def insert_local_fills(self, rows):
        with sqlite3.connect(self.path) as db:
            for row in rows:
                db.execute("""INSERT OR IGNORE INTO local_fills
                    (account,leader,order_id,symbol,side,quantity,price,reduce_only,timestamp)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (row.get("account"), row.get("leader"), row.get("order_id"),
                    row.get("symbol"), row.get("side"), row.get("quantity"), row.get("price"),
                    int(bool(row.get("reduce_only"))), row.get("timestamp") or time.time()))

    def attribute_trades(self, account):
        """Match exchange rows to local fills by symbol, side and execution time.

        A fill is used at most once; unmatched rows deliberately stay in the
        unattributed bucket so the UI never invents a trader assignment.
        """
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            trades = [dict(x) for x in db.execute("SELECT * FROM trades WHERE account=? AND (leader IS NULL OR leader='')", (account,))]
            fills = [dict(x) for x in db.execute("SELECT * FROM local_fills WHERE account=? ORDER BY timestamp", (account,))]
            used = set()
            for trade in trades:
                candidates = []
                for fill in fills:
                    if fill["id"] in used or not fill.get("reduce_only") or _symbol(fill.get("symbol")) != _symbol(trade.get("symbol")):
                        continue
                    delta = abs(_number(fill.get("timestamp")) - _number(trade.get("timestamp")))
                    if delta <= 86400:
                        candidates.append((delta, fill))
                if not candidates:
                    continue
                _, fill = min(candidates, key=lambda x: x[0])
                used.add(fill["id"])
                db.execute("UPDATE trades SET leader=?, attribution='local_fill' WHERE id=?", (fill["leader"], trade["id"]))

    def audit(self, action, detail):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO audit_logs(action,detail,created_at) VALUES(?,?,?)",
                       (action, json.dumps(detail, sort_keys=True), time.time()))


class ConsoleService:
    editable = {"address", "enabled", "coefficient", "total_margin", "symbol_blacklist",
                "late_entry_enabled", "late_entry_offset_pct", "position_stop_loss"}

    def __init__(self, engine, config, store_path="data/console.sqlite3", leader_stream=None):
        self.engine = engine
        self.config = config
        self.leader_stream = leader_stream
        self.store = ConsoleStore(store_path)
        self._snapshot = {"updated_at": 0, "leaders": [], "accounts": [], "status": {}}
        self._subscribers = set()
        self._task = None

    def install(self, app):
        app.router.add_get("/api/v1/dashboard", self.dashboard)
        app.router.add_get("/api/v1/trades", self.trades)
        app.router.add_get("/api/v1/stats", self.stats)
        app.router.add_get("/api/v1/performance/overview", self.performance_overview)
        app.router.add_get("/api/v1/performance/leaders", self.performance_leaders)
        app.router.add_get("/api/v1/performance/equity", self.performance_equity)
        app.router.add_get("/api/v1/events", self.events)
        app.router.add_patch("/api/v1/leaders/{name}", self.update_leader)
        app.router.add_post("/api/v1/control/{action}", self.control)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)

    async def start(self, app):
        self._task = asyncio.create_task(self.refresh_loop())

    async def stop(self, app):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for response in list(self._subscribers):
            response.force_close()

    async def dashboard(self, request):
        return web.json_response(self._snapshot)

    async def trades(self, request):
        account = self._account_name()
        limit = min(int(request.query.get("limit", 100)), 500)
        return web.json_response({"items": self.store.trades(account, limit)})

    async def stats(self, request):
        account = self._account_name()
        snapshots = self.store.snapshots(account)
        trades = self.store.trades(account, 500)
        result = self.store.performance(account)
        return web.json_response({"account": account, "snapshots": snapshots, **result})

    def _performance(self, request):
        account = request.query.get("account") or self._account_name()
        def number(name):
            value = request.query.get(name)
            return float(value) if value not in (None, "") else None
        return self.store.performance(account, number("since"), number("until"))

    async def performance_overview(self, request):
        result = self._performance(request)
        result["positions"] = self._attributed_positions()
        return web.json_response(result)

    async def performance_leaders(self, request):
        return web.json_response({"items": self._performance(request)["leaders"]})

    async def performance_equity(self, request):
        return web.json_response({"items": self._performance(request)["curve"]})

    async def events(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"})
        await response.prepare(request)
        self._subscribers.add(response)
        try:
            await response.write(("data: " + json.dumps(self._snapshot) + "\n\n").encode())
            while True:
                await asyncio.sleep(30)
                await response.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.discard(response)
        return response

    async def update_leader(self, request):
        name = request.match_info["name"]
        if name not in self.engine.leaders:
            raise web.HTTPNotFound(text="Unknown leader")
        payload = await request.json()
        changes = {k: v for k, v in payload.items() if k in self.editable}
        if not changes:
            raise web.HTTPBadRequest(text="No editable fields")
        try:
            if "address" in changes:
                address = str(changes["address"]).strip()
                if not (address.startswith("0x") and len(address) == 42):
                    raise ValueError("address must be a 42-character 0x address")
                int(address[2:], 16)
                changes["address"] = address.lower()
            if "coefficient" in changes and float(changes["coefficient"]) < 0:
                raise ValueError("coefficient must be non-negative")
            if "total_margin" in changes and float(changes["total_margin"]) <= 0:
                raise ValueError("total_margin must be positive")
            if "late_entry_offset_pct" in changes and float(changes["late_entry_offset_pct"]) < 0:
                raise ValueError("late_entry_offset_pct must be non-negative")
            if "position_stop_loss" in changes:
                stop = changes["position_stop_loss"]
                if not isinstance(stop, dict):
                    raise ValueError("position_stop_loss must be an object")
                if "max_loss_usdt" in stop and float(stop["max_loss_usdt"]) <= 0:
                    raise ValueError("position stop loss must be positive")
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        self.engine.config_manager.update_leader(name, changes)
        await self.engine.update_leader_config(name, changes)
        if self.leader_stream and "address" in changes:
            self.leader_stream.addresses = [str(item.get("address")) for item in self.engine.leaders.values()]
        self.store.audit("leader.update", {"leader": name, "fields": list(changes)})
        return web.json_response({"ok": True, "leader": self.engine.leaders[name]})

    async def control(self, request):
        action = request.match_info["action"]
        if action not in ("pause", "resume"):
            raise web.HTTPNotFound()
        self.engine.paused = action == "pause"
        self.store.audit("control." + action, {})
        return web.json_response({"ok": True, "paused": self.engine.paused})

    def _account_name(self):
        return next((x.get("execution_account") for x in self.engine.leaders.values()
                      if x.get("execution_account")), "default")

    def _attributed_positions(self):
        """Annotate the latest account positions with the owning leader.

        Ownership comes from the engine's symbol claims; when a symbol was
        never claimed (manual or pre-existing exposure) we fall back to the
        symbol's newest opening fill, then to ``unattributed`` rather than
        guessing.
        """
        accounts = self._snapshot.get("accounts") or []
        rows = accounts[0].get("positions") if accounts else []
        if not isinstance(rows, list):
            return []
        owners = {}
        for key, leader in getattr(self.engine, "owners", {}).items():
            symbol = key.partition(":")[2]
            if symbol:
                owners[symbol.upper()] = leader
        fills = self.store.latest_open_fill_leaders(self._account_name())
        result = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            annotated = dict(row)
            annotated["leader"] = owners.get(symbol) or fills.get(symbol) or "unattributed"
            result.append(annotated)
        return result

    async def refresh_loop(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                pass
            await asyncio.sleep(3)

    async def refresh(self):
        leaders = []
        for name, cfg in self.engine.leaders.items():
            try:
                positions = await leader_snapshot(cfg)
                rows = [self._position_dict(p) for p in positions.values()]
            except Exception as exc:
                rows = [{"error": type(exc).__name__}]
            leaders.append({"name": name, "address": str(cfg.get("address", "")), "config": cfg, "positions": rows})
        account_rows, orders, equity, balance, unrealized = [], [], 0.0, 0.0, 0.0
        exchange = self.engine.exchange
        try:
            if self.engine.fill_events:
                self.store.insert_local_fills(self.engine.fill_events)
                self.engine.fill_events.clear()
            raw = await exchange.fetch_positions()
            account_rows = [self._account_position(p) for p in raw if _number(p.get("contracts"))]
            equity = await exchange.equity()
            unrealized = sum(_number(x.get("unrealizedPnl")) for x in raw)
            balance = await exchange.wallet_balance() if hasattr(exchange, "wallet_balance") else equity - unrealized
            self.store.snapshot(self._account_name(), equity, unrealized)
        except Exception:
            log.exception("Console account refresh failed")
        try:
            orders = await exchange.fetch_open_orders()
        except Exception as exc:
            log.warning("Console open orders refresh failed: %s", exc)
            orders = [{"error": type(exc).__name__}]
        try:
            if hasattr(exchange, "fetch_realized_pnl"):
                self.store.insert_trades(self._account_name(), await exchange.fetch_realized_pnl())
                self.store.attribute_trades(self._account_name())
            if hasattr(exchange, "fetch_cash_flows"):
                self.store.insert_cash_flows(self._account_name(), await exchange.fetch_cash_flows())
        except Exception:
            log.exception("Console performance refresh failed")
        self._snapshot = {"updated_at": time.time(), "leaders": leaders,
                          "accounts": [{"name": self._account_name(), "balance": balance, "equity": equity, "unrealized_pnl": unrealized,
                                        "positions": account_rows, "orders": orders}],
                          "status": {"paused": self.engine.paused, "risk_locked": self.engine.risk.locked,
                                     "hard_stop": self.engine.risk.config,
                                     "position_stop_loss": [x.get("position_stop_loss") for x in self.engine.leaders.values()
                                                             if x.get("position_stop_loss", {}).get("enabled")]}}
        message = ("data: " + json.dumps(self._snapshot, separators=(",", ":")) + "\n\n").encode()
        for response in list(self._subscribers):
            try:
                await response.write(message)
            except Exception:
                self._subscribers.discard(response)

    @staticmethod
    def _position_dict(p):
        margin = abs(p.size * p.entry_price / (p.leverage or 1))
        return {"symbol": _symbol(p.symbol), "side": p.side.upper(), "size": abs(p.size),
                "entry_price": p.entry_price, "mark_price": p.mark_price,
                "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage,
                "roi": _roi(p.unrealized_pnl, p.size, p.entry_price, p.leverage,
                             margin * (p.leverage or 1), margin)}

    @staticmethod
    def _account_position(p):
        contracts = _number(p.get("contracts")); size = contracts * (_number(p.get("contractSize")) or 1)
        entry = _number(p.get("entryPrice")); pnl = _number(p.get("unrealizedPnl"))
        notional = _number(p.get("notional")); leverage = _number(p.get("leverage")) or 1
        return {"symbol": _symbol(p.get("symbol")), "side": str(p.get("side") or "").upper(),
                "size": abs(size), "entry_price": entry, "mark_price": _number(p.get("markPrice")),
                "unrealized_pnl": pnl, "leverage": leverage,
                "roi": _roi(pnl, size, entry, leverage, notional)}
