import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from sources.position_queries import _address, _number, _roi, leader_snapshot
from core.symbols import SymbolMap

log = logging.getLogger(__name__)


class ConsoleStore:
    def __init__(self, path, symbol_map=None):
        self.path = Path(path)
        self.symbol_map = symbol_map if isinstance(symbol_map, SymbolMap) else SymbolMap(symbol_map)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection per worker thread: store calls run through
        # asyncio.to_thread, and opening a fresh connection per call was
        # holding the event loop while SQLite touched disk.
        self._local = threading.local()
        with self._connection() as db:
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
            # Gate position-close rows predate the ledger feed.  The ledger
            # re-reports every close (including stop-outs that position history
            # merges into a reopened episode), so retire the old rows rather
            # than double-count them.
            db.execute("DELETE FROM trades WHERE source = 'position_close'")

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0)
            # WAL lets the request handlers read while a background write is in
            # flight; busy_timeout waits out the brief writer serialization.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def snapshot(self, account, equity, unrealized):
        with self._connection() as db:
            db.execute("INSERT INTO equity_snapshots(account,equity,unrealized_pnl,created_at) VALUES(?,?,?,?)",
                       (account, equity, unrealized, time.time()))

    def trades(self, account, limit=100):
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM trades WHERE account=? ORDER BY timestamp DESC LIMIT ?", (account, limit)
            )]
        return [dict(row, symbol=self.symbol_map.display(row.get("symbol"))) for row in rows]

    def insert_trades(self, account, rows):
        rows = list(rows)
        with self._connection() as db:
            # The ledger supersedes position history: a refresh that falls back
            # to position_close while the ledger endpoint is unavailable must
            # not leave duplicates behind once the ledger recovers.
            if any(row.get("source") == "ledger" for row in rows):
                db.execute("DELETE FROM trades WHERE account=? AND source='position_close'", (account,))
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
        with self._connection() as db:
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
        items = [dict(row, symbol=self.symbol_map.display(row.get("symbol"))) for row in rows]
        return {"account": account, "gross_pnl": gross, "fees": fees, "funding": funding, "net_pnl": net, "return_pct": return_pct,
                "cash_flow": sum(_number(x.get("amount")) for x in flows), "trades": len(rows),
                "leaders": sorted(by_leader.values(), key=lambda x: x["net_pnl"], reverse=True),
                "window": {"since": since, "until": until},
                "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct, "curve": curve, "items": items}

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
        with self._connection() as db:
            return {str(row[0]).upper(): row[1] for row in db.execute(
                "SELECT symbol, leader, MAX(timestamp) FROM local_fills WHERE account=? AND reduce_only=0 GROUP BY symbol",
                (account,))}

    def snapshots(self, account, limit=240):
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM equity_snapshots WHERE account=? ORDER BY created_at DESC LIMIT ?", (account, limit)
            )]

    def insert_cash_flows(self, account, rows):
        with self._connection() as db:
            for row in rows:
                external_id = row.get("id") or f"{row.get('kind')}:{row.get('timestamp')}:{row.get('amount')}"
                db.execute("INSERT OR IGNORE INTO cash_flows(account,kind,amount,timestamp,external_id) VALUES(?,?,?,?,?)",
                           (account, row.get("kind"), row.get("amount", 0), row.get("timestamp") or time.time(), f"{account}:{external_id}"))

    def insert_local_fills(self, rows):
        with self._connection() as db:
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
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            trades = [dict(x) for x in db.execute("SELECT * FROM trades WHERE account=? AND (leader IS NULL OR leader='')", (account,))]
            fills = [dict(x) for x in db.execute("SELECT * FROM local_fills WHERE account=? ORDER BY timestamp", (account,))]
            used = set()
            for trade in trades:
                candidates = []
                for fill in fills:
                    if fill["id"] in used or not fill.get("reduce_only") or self.symbol_map.display(fill.get("symbol")) != self.symbol_map.display(trade.get("symbol")):
                        continue
                    delta = abs(_number(fill.get("timestamp")) - _number(trade.get("timestamp")))
                    if delta <= 86400:
                        candidates.append((delta, fill))
                if not candidates:
                    continue
                _, fill = min(candidates, key=lambda x: x[0])
                used.add(fill["id"])
                trade["leader"] = fill["leader"]
                db.execute("UPDATE trades SET leader=?, attribution='local_fill' WHERE id=?", (fill["leader"], trade["id"]))
            # Exchange-side closes (native stop-loss, liquidation, manual) never
            # produce a local reduce-only fill.  Attribute them to the leader
            # whose newest opening fill at or before the close belongs to; fills
            # after the close belong to a reopened episode and are ignored.
            for trade in trades:
                if trade.get("leader"):
                    continue
                trade_ts = _number(trade.get("timestamp"))
                symbol = self.symbol_map.display(trade.get("symbol"))
                opener = None
                for fill in fills:
                    if fill.get("reduce_only") or self.symbol_map.display(fill.get("symbol")) != symbol:
                        continue
                    if _number(fill.get("timestamp")) <= trade_ts:
                        opener = fill
                if opener and opener.get("leader"):
                    db.execute("UPDATE trades SET leader=?, attribution='stop_close' WHERE id=?",
                               (opener["leader"], trade["id"]))

    def audit(self, action, detail):
        with self._connection() as db:
            db.execute("INSERT INTO audit_logs(action,detail,created_at) VALUES(?,?,?)",
                       (action, json.dumps(detail, sort_keys=True), time.time()))

    def prune(self, retention_days=90, now=None):
        """Drop equity snapshots older than the retention window.

        Snapshots are the only table written on a fixed cadence and grow
        without bound; trades and cash flows are the PnL record and stay.
        """
        cutoff = (now or time.time()) - float(retention_days) * 86400
        with self._connection() as db:
            return db.execute("DELETE FROM equity_snapshots WHERE created_at < ?", (cutoff,)).rowcount


class ConsoleService:
    editable = {"address", "enabled", "coefficient", "total_margin", "symbol_blacklist",
                "late_entry_enabled", "late_entry_offset_pct", "position_stop_loss"}

    def __init__(self, engine, config, store_path="data/console.sqlite3", leader_stream=None):
        self.engine = engine
        self.config = config
        self.leader_stream = leader_stream
        self.symbol_map = getattr(engine, "symbol_map", None)
        if not isinstance(self.symbol_map, SymbolMap):
            self.symbol_map = SymbolMap((config or {}).get("symbol_map"))
        self.store = ConsoleStore(store_path, self.symbol_map)
        self._snapshot = {"updated_at": 0, "leaders": [], "accounts": [], "status": {}}
        self._subscribers = set()
        self._task = None
        console_config = (config or {}).get("console") or {}
        self._last_rest = 0.0
        self._rest_interval = max(10.0, float(console_config.get("leader_rest_seconds", 60)))
        # Ledger/trade/equity-curve refreshes paginate REST and scan SQLite;
        # the dashboard only needs the 3-second account view.
        self._performance_interval = max(10.0, float(console_config.get("performance_seconds", 60)))
        self._retention_days = float(console_config.get("snapshot_retention_days", 90))
        self._last_performance = 0.0
        self._last_prune = 0.0

    def install(self, app):
        app.router.add_get("/api/v1/dashboard", self.dashboard)
        app.router.add_get("/api/v1/trades", self.trades)
        app.router.add_get("/api/v1/stats", self.stats)
        app.router.add_get("/api/v1/performance/overview", self.performance_overview)
        app.router.add_get("/api/v1/performance/leaders", self.performance_leaders)
        app.router.add_get("/api/v1/performance/equity", self.performance_equity)
        app.router.add_get("/api/v1/events", self.events)
        app.router.add_patch("/api/v1/leaders/{name}", self.update_leader)
        app.router.add_post("/api/v1/positions/breakeven", self.breakeven)
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
        return web.json_response({"items": await asyncio.to_thread(self.store.trades, account, limit)})

    async def stats(self, request):
        account = self._account_name()
        snapshots = await asyncio.to_thread(self.store.snapshots, account)
        result = await asyncio.to_thread(self.store.performance, account)
        return web.json_response({"account": account, "snapshots": snapshots, **result})

    async def _performance(self, request):
        account = request.query.get("account") or self._account_name()
        def number(name):
            value = request.query.get(name)
            return float(value) if value not in (None, "") else None
        return await asyncio.to_thread(self.store.performance, account, number("since"), number("until"))

    async def performance_overview(self, request):
        result = await self._performance(request)
        result["positions"] = await self._attributed_positions()
        return web.json_response(result)

    async def performance_leaders(self, request):
        result = await self._performance(request)
        return web.json_response({"items": result["leaders"]})

    async def performance_equity(self, request):
        result = await self._performance(request)
        return web.json_response({"items": result["curve"]})

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
            if "enabled" in changes:
                value = changes["enabled"]
                if isinstance(value, bool):
                    pass
                elif isinstance(value, (int, str)):
                    changes["enabled"] = str(value).strip().lower() not in ("false", "0", "no", "")
                else:
                    raise ValueError("enabled must be a boolean")
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        self.engine.config_manager.update_leader(name, changes)
        address_changed = await self.engine.update_leader_config(name, changes)
        config = self.engine.leaders[name]
        if address_changed or "enabled" in changes:
            if self.leader_stream:
                # Only enabled leaders are streamed, so toggling one on adds
                # its subscription without waiting for a restart.
                await self.leader_stream.set_addresses(
                    [_address(item.get("address")) for item in self.engine.leaders.values()
                     if item.get("enabled", True)]
                )
        if config.get("enabled", True) and (address_changed or "enabled" in changes):
            # Seed the new baseline immediately so the first diff is against
            # the leader's real positions, not an empty one.
            try:
                await self.engine.process(name, await leader_snapshot(config))
            except Exception:
                log.exception("Initial snapshot after leader update failed for %s", name)
        await asyncio.to_thread(self.store.audit, "leader.update", {"leader": name, "fields": list(changes)})
        return web.json_response({"ok": True, "leader": self.engine.leaders[name]})

    async def control(self, request):
        action = request.match_info["action"]
        if action not in ("pause", "resume"):
            raise web.HTTPNotFound()
        self.engine.paused = action == "pause"
        await asyncio.to_thread(self.store.audit, "control." + action, {})
        return web.json_response({"ok": True, "paused": self.engine.paused})

    async def breakeven(self, request):
        """Move one account position's stop to its entry price, or revert it."""
        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid JSON body")
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise web.HTTPBadRequest(text="symbol is required")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() not in ("false", "0", "no", "")
        key, leader, engine_symbol = self._owner_for_display(symbol)
        try:
            if enabled:
                trigger = (await self.engine.set_breakeven(leader, engine_symbol) if key
                           else await self._place_breakeven(symbol))
            else:
                if not key:
                    raise ValueError("position is not managed by the engine")
                await self.engine.clear_breakeven(leader, engine_symbol)
                trigger = None
        except (KeyError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc).strip("'"))
        except Exception:
            log.exception("Breakeven request failed for %s", symbol)
            raise web.HTTPInternalServerError(text="breakeven request failed")
        await asyncio.to_thread(self.store.audit, "position.breakeven", {"symbol": symbol, "enabled": enabled})
        return web.json_response({"ok": True, "symbol": symbol, "breakeven": enabled, "trigger": trigger})

    def _owner_for_display(self, display):
        """Find the engine owner key behind a position's displayed symbol."""
        for key, leader in getattr(self.engine, "owners", {}).items():
            _, _, symbol = key.partition(":")
            if self.symbol_map.display(symbol) == display:
                return key, leader, symbol
        return None, None, None

    async def _place_breakeven(self, display):
        """Place a native stop at entry for a position the engine does not own."""
        rows = await self.engine.exchange.fetch_positions()
        row = next((p for p in rows if _number(p.get("contracts"))
                    and self.symbol_map.display(p.get("symbol")) == display), None)
        if row is None:
            raise ValueError("open position not found")
        contracts = abs(_number(row.get("contracts")))
        entry = _number(row.get("entryPrice"))
        mark = _number(row.get("markPrice"))
        side = str(row.get("side") or "").lower()
        if contracts <= 0 or entry <= 0:
            raise ValueError("position size or entry price unavailable")
        if mark > 0 and ((side == "long" and entry >= mark) or (side == "short" and entry <= mark)):
            raise ValueError("position is not in profit; a breakeven stop would trigger immediately")
        # set_protection talks to ccxt directly, where a contract market's
        # amount is a number of contracts rather than base-asset quantity.
        await self.engine.exchange.set_protection(
            row.get("symbol"), "sell" if side == "long" else "buy", contracts, entry)
        return entry

    def _account_name(self):
        return (next((x.get("execution_account") for x in self.engine.leaders.values()
                      if x.get("enabled", True) and x.get("execution_account")), None)
                or next((x.get("execution_account") for x in self.engine.leaders.values()
                         if x.get("execution_account")), "default"))

    async def _attributed_positions(self):
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
                owners[self.symbol_map.display(symbol)] = leader
        fill_leaders = await asyncio.to_thread(self.store.latest_open_fill_leaders, self._account_name())
        fills = {self.symbol_map.display(k): v for k, v in fill_leaders.items()}
        result = []
        for row in rows:
            symbol = self.symbol_map.display(row.get("symbol"))
            annotated = dict(row)
            annotated["leader"] = owners.get(symbol) or fills.get(symbol) or "unattributed"
            result.append(annotated)
        return result

    async def _refresh_leader_fallback(self):
        """Feed a REST leader snapshot to the engine at a low fixed rate.

        The websocket is change-driven and can go silent without disconnecting,
        so this doubles as a stale-subscription fallback.  It stays far below
        the 3-second display cadence because Hyperliquid allows 1200 weight per
        minute per IP and the websocket resync shares that budget.
        """
        now = time.time()
        if now - self._last_rest < self._rest_interval:
            return
        self._last_rest = now
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name, cfg in self.engine.leaders.items():
                if not cfg.get("enabled", True):
                    continue
                started = time.time()
                try:
                    snapshot = await leader_snapshot(cfg, session)
                except Exception as exc:
                    log.warning("Leader REST fallback failed for %s: %s", name, exc)
                    continue
                # A websocket event may have landed while the REST call was in
                # flight; processing the older snapshot would diff backwards.
                if getattr(self.engine, "previous_at", {}).get(name, 0) >= started:
                    continue
                try:
                    await self.engine.process(name, snapshot)
                except Exception:
                    log.exception("Leader REST fallback processing failed for %s", name)

    async def refresh_loop(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                log.warning("Console refresh failed", exc_info=True)
            await asyncio.sleep(3)

    async def refresh(self):
        await self._refresh_leader_fallback()
        leaders = []
        previous = getattr(self.engine, "previous", {})
        for name, cfg in self.engine.leaders.items():
            positions = previous.get(name)
            if positions is None:
                rows = [{"error": "awaiting leader state"}]
            else:
                rows = [self._position_dict(p) for p in positions.values()]
            leaders.append({"name": name, "address": str(cfg.get("address", "")),
                            "enabled": bool(cfg.get("enabled", True)),
                            "config": cfg, "positions": rows})
        account_rows, orders, equity, balance, unrealized = [], [], 0.0, 0.0, 0.0
        account_ok = False
        exchange = self.engine.exchange
        if self.engine.fill_events:
            events = list(self.engine.fill_events)
            try:
                await asyncio.to_thread(self.store.insert_local_fills, events)
                del self.engine.fill_events[:len(events)]
            except Exception:
                log.exception("Console local fill refresh failed")
        try:
            raw = await exchange.fetch_positions()
            account_rows = [self._account_position(p) for p in raw if _number(p.get("contracts"))]
            for row in account_rows:
                key, _, _ = self._owner_for_display(row.get("symbol"))
                row["breakeven"] = bool(key and (getattr(self.engine, "breakeven", None) or {}).get(key))
            equity = await exchange.equity()
            unrealized = sum(_number(x.get("unrealizedPnl")) for x in raw)
            balance = await exchange.wallet_balance() if hasattr(exchange, "wallet_balance") else equity - unrealized
            account_ok = True
        except Exception:
            log.exception("Console account refresh failed")
        try:
            orders = [dict(order, symbol=self.symbol_map.display(order.get("symbol")))
                      for order in await exchange.fetch_open_orders()]
        except Exception as exc:
            log.warning("Console open orders refresh failed: %s", exc)
            orders = [{"error": type(exc).__name__}]
        if time.monotonic() - self._last_performance >= self._performance_interval:
            self._last_performance = time.monotonic()
            await self._refresh_performance(equity if account_ok else None, unrealized)
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

    async def _refresh_performance(self, equity, unrealized):
        """Slow path: equity curve, ledger trades and cash flows.

        Kept off the 3-second display loop because the Gate ledger paginates
        REST and attribution scans the whole local-fill table.
        """
        account = self._account_name()
        exchange = self.engine.exchange
        try:
            if equity is not None:
                await asyncio.to_thread(self.store.snapshot, account, equity, unrealized)
            if hasattr(exchange, "fetch_realized_pnl"):
                rows = await exchange.fetch_realized_pnl()
                await asyncio.to_thread(self.store.insert_trades, account, rows)
                await asyncio.to_thread(self.store.attribute_trades, account)
            if hasattr(exchange, "fetch_cash_flows"):
                rows = await exchange.fetch_cash_flows()
                await asyncio.to_thread(self.store.insert_cash_flows, account, rows)
        except Exception:
            log.exception("Console performance refresh failed")
        await self._maybe_prune()

    async def _maybe_prune(self):
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            removed = await asyncio.to_thread(self.store.prune, self._retention_days)
            if removed:
                log.info("Pruned %d equity snapshots older than %g days", removed, self._retention_days)
        except Exception:
            log.exception("Console retention prune failed")

    def _position_dict(self, p):
        margin = abs(p.size * p.entry_price / (p.leverage or 1))
        return {"symbol": self.symbol_map.display(p.symbol), "side": p.side.upper(), "size": abs(p.size),
                "entry_price": p.entry_price, "mark_price": p.mark_price,
                "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage,
                "roi": _roi(p.unrealized_pnl, p.size, p.entry_price, p.leverage,
                             margin * (p.leverage or 1), margin)}

    def _account_position(self, p):
        contracts = _number(p.get("contracts")); size = contracts * (_number(p.get("contractSize")) or 1)
        entry = _number(p.get("entryPrice")); pnl = _number(p.get("unrealizedPnl"))
        notional = _number(p.get("notional")); leverage = _number(p.get("leverage")) or 1
        return {"symbol": self.symbol_map.display(p.get("symbol")), "side": str(p.get("side") or "").upper(),
                "size": abs(size), "entry_price": entry, "mark_price": _number(p.get("markPrice")),
                "unrealized_pnl": pnl, "leverage": leverage,
                "roi": _roi(pnl, size, entry, leverage, notional)}
