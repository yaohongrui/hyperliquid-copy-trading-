import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from aiohttp import web

from sources.position_queries import _number, _roi, _symbol, leader_snapshot


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
              fee REAL, timestamp REAL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
              id INTEGER PRIMARY KEY, action TEXT, detail TEXT, created_at REAL
            );
            """)

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
                db.execute("""INSERT OR IGNORE INTO trades
                    (account,exchange_trade_id,symbol,side,quantity,price,realized_pnl,fee,timestamp)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (account, str(row.get("id")), row.get("symbol"), row.get("side"),
                    row.get("amount"), row.get("price"), row.get("realizedPnl", row.get("realized_pnl", 0)),
                    row.get("fee", 0), row.get("timestamp") or time.time()))

    def snapshots(self, account, limit=240):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM equity_snapshots WHERE account=? ORDER BY created_at DESC LIMIT ?", (account, limit)
            )]

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
        self.username = os.getenv("CONSOLE_USERNAME", "admin")
        # A missing password must not silently create an admin account with an
        # empty password. Operators must explicitly configure the credential.
        configured_password = os.getenv("CONSOLE_PASSWORD")
        self.password_hash = self._hash(configured_password) if configured_password else None
        self.sessions = {}
        self._snapshot = {"updated_at": 0, "leaders": [], "accounts": [], "status": {}}
        self._subscribers = set()
        self._task = None

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def install(self, app):
        app.router.add_post("/api/v1/auth/login", self.login)
        app.router.add_post("/api/v1/auth/logout", self.logout)
        app.router.add_get("/api/v1/dashboard", self.dashboard)
        app.router.add_get("/api/v1/trades", self.trades)
        app.router.add_get("/api/v1/stats", self.stats)
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

    def _authorized(self, request):
        token = request.cookies.get("console_session")
        if token and token in self.sessions and self.sessions[token] > time.time():
            return True
        try:
            payload, signature = token.rsplit('.', 1)
            expected = hmac.new(self.password_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
            expires = int(payload.rsplit('.', 1)[0])
            return expires > int(time.time()) and hmac.compare_digest(signature, expected)
        except (AttributeError, ValueError):
            return False

    async def _require(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Session"})

    async def login(self, request):
        payload = await request.json()
        if (not self.password_hash or
                not hmac.compare_digest(str(payload.get("username", "")), self.username) or
                not hmac.compare_digest(self.password_hash, self._hash(str(payload.get("password", ""))))):
            raise web.HTTPUnauthorized(text="Invalid credentials")
        expires = int(time.time()) + 86400 * 30
        payload = f"{expires}.{secrets.token_urlsafe(16)}"
        token = payload + "." + hmac.new(self.password_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
        response = web.json_response({"ok": True})
        # Keep local HTTP development usable while retaining Secure cookies
        # when the console is served through the HTTPS reverse proxy.
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        secure = request.secure or forwarded_proto == "https"
        response.set_cookie("console_session", token, httponly=True, secure=secure,
                            samesite="Lax", max_age=86400 * 30)
        return response

    async def logout(self, request):
        token = request.cookies.get("console_session")
        self.sessions.pop(token, None)
        response = web.json_response({"ok": True})
        response.del_cookie("console_session")
        return response

    async def dashboard(self, request):
        await self._require(request)
        return web.json_response(self._snapshot)

    async def trades(self, request):
        await self._require(request)
        account = self._account_name()
        limit = min(int(request.query.get("limit", 100)), 500)
        return web.json_response({"items": self.store.trades(account, limit)})

    async def stats(self, request):
        await self._require(request)
        account = self._account_name()
        snapshots = self.store.snapshots(account)
        trades = self.store.trades(account, 500)
        return web.json_response({"account": account, "snapshots": snapshots,
                                  "realized_pnl": sum(_number(x.get("realized_pnl")) for x in trades)})

    async def events(self, request):
        await self._require(request)
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
        await self._require(request)
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
        await self._require(request)
        action = request.match_info["action"]
        if action not in ("pause", "resume"):
            raise web.HTTPNotFound()
        self.engine.paused = action == "pause"
        self.store.audit("control." + action, {})
        return web.json_response({"ok": True, "paused": self.engine.paused})

    def _account_name(self):
        return next((x.get("execution_account") for x in self.engine.leaders.values()
                      if x.get("execution_account")), "default")

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
            raw = await exchange.fetch_positions()
            account_rows = [self._account_position(p) for p in raw if _number(p.get("contracts"))]
            orders = await exchange.fetch_open_orders()
            equity = await exchange.equity()
            unrealized = sum(_number(x.get("unrealizedPnl")) for x in raw)
            balance = await exchange.wallet_balance() if hasattr(exchange, "wallet_balance") else equity - unrealized
            self.store.snapshot(self._account_name(), equity, unrealized)
            if hasattr(exchange, "fetch_realized_pnl"):
                self.store.insert_trades(self._account_name(), await exchange.fetch_realized_pnl())
        except Exception as exc:
            orders = [{"error": type(exc).__name__}]
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
