"""Leader resting-order and fill feeds used by order-type mirroring.

The engine has always followed a leader through position snapshots only.  That
tells us *what* changed, never *how* it changed, so every copy was a market
order.  These feeds add the missing half:

* ``fetch_leader_orders`` reads the leader's resting limit orders (per dex,
  including HIP-3) plus the current mid prices.
* ``LeaderFillBook`` keeps the recent ``userFills`` stream so the engine can
  tell a maker fill from a taker fill and avoid double-executing a fill the
  mirror already copied.

Read-only: nothing here places or cancels an order.
"""

import time
from dataclasses import dataclass

import aiohttp

from .position_queries import _perp_dexes

INFO_URL = "https://api.hyperliquid.xyz/info"


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _address(value):
    # PyYAML parses an unquoted 0x address as an int.
    return hex(value) if isinstance(value, int) else (str(value) if value else "")


@dataclass(frozen=True)
class LeaderRestingOrder:
    """One plain limit order resting on the leader's book."""

    leader: str
    dex: str
    symbol: str
    oid: str
    side: str          # "buy" / "sell"
    price: float
    size: float
    mark: float
    reduce_only: bool
    timestamp: float

    @property
    def distance(self):
        return abs(self.price - self.mark) / self.mark if self.mark > 0 else float("inf")

    @property
    def above(self):
        return self.price > self.mark


@dataclass(frozen=True)
class LeaderFill:
    """One fill from ``userFills`` (deduped by ``tid``)."""

    leader: str
    symbol: str
    side: str          # "buy" / "sell"
    price: float
    size: float
    time: float
    crossed: bool
    start_position: float
    oid: str
    tid: str

    @property
    def signed_size(self):
        return self.size if self.side == "buy" else -self.size

    @property
    def is_entry(self):
        """True when this fill increased the absolute leader position."""
        end = self.start_position + self.signed_size
        return abs(end) > abs(self.start_position) + 1e-12


async def _post(session, payload):
    async with session.post(INFO_URL, json=payload) as response:
        response.raise_for_status()
        return await response.json()


async def fetch_leader_orders(leader, address, session, dexes=None):
    """Return ``{dex: [LeaderRestingOrder]}`` or ``None`` if every dex failed.

    ``None`` is deliberately distinct from an empty dict: a failed fetch must
    never make the mirror cancel every order it placed.  Trigger orders are
    excluded here so callers only ever see plain limits.
    """
    if dexes is None:
        dexes = await _perp_dexes(session)
    books = {}
    failed = 0
    for dex in dexes:
        payload = {"type": "frontendOpenOrders", "user": address}
        mids_payload = {"type": "allMids"}
        if dex:
            payload["dex"] = dex
            mids_payload["dex"] = dex
        try:
            rows = await _post(session, payload) or []
        except Exception:
            failed += 1
            continue
        orders = []
        for row in rows:
            if not isinstance(row, dict) or row.get("isTrigger"):
                continue
            if str(row.get("orderType") or "") != "Limit":
                continue
            price = _num(row.get("limitPx"))
            size = _num(row.get("sz"))
            coin = str(row.get("coin") or "")
            oid = row.get("oid")
            if price <= 0 or size <= 0 or not coin or oid is None:
                continue
            orders.append({
                "coin": coin, "oid": str(oid), "price": price, "size": size,
                "side": "buy" if str(row.get("side")).upper() == "B" else "sell",
                "reduce_only": bool(row.get("reduceOnly")),
                "timestamp": _num(row.get("timestamp")) / 1000.0,
            })
        if not orders:
            # No mids request for a dex without orders: HIP-3 dexes are often
            # empty and every avoided call stays inside the rate budget.
            books[dex] = []
            continue
        try:
            mids = await _post(session, mids_payload) or {}
        except Exception:
            mids = {}
        books[dex] = [
            LeaderRestingOrder(
                leader=leader,
                dex=dex,
                symbol=row["coin"].upper(),
                oid=row["oid"],
                side=row["side"],
                price=row["price"],
                size=row["size"],
                mark=_num(mids.get(row["coin"])),
                reduce_only=row["reduce_only"],
                timestamp=row["timestamp"],
            )
            for row in orders
        ]
    if failed == len(dexes):
        return None
    return books


class LeaderFillBook:
    """Rolling per-leader fill history with stable deduplication."""

    def __init__(self, window=900.0, max_fills=4000):
        self.window = float(window)
        self.max_fills = int(max_fills)
        self._fills = {}
        self._keys = {}
        self._by_oid = {}
        self._newest = {}
        self.last_at = 0.0

    @staticmethod
    def _key(fill):
        if fill.tid:
            return f"tid:{fill.tid}"
        return "raw:{0}:{1}:{2}:{3}:{4}".format(
            fill.oid, fill.time, fill.symbol, fill.size, fill.price)

    def add(self, leader, raw_fills):
        """Append unseen fills and return only the newly added ones."""
        added = []
        for raw in raw_fills or []:
            if not isinstance(raw, dict):
                continue
            fill = LeaderFill(
                leader=leader,
                symbol=str(raw.get("coin") or "").upper(),
                side="buy" if str(raw.get("side")).upper() == "B" else "sell",
                price=_num(raw.get("px")),
                size=_num(raw.get("sz")),
                time=_num(raw.get("time")) / 1000.0,
                crossed=bool(raw.get("crossed")),
                start_position=_num(raw.get("startPosition")),
                oid=str(raw.get("oid") or ""),
                tid=str(raw.get("tid") or ""),
            )
            if fill.size <= 0 or not fill.symbol:
                continue
            key = self._key(fill)
            keys = self._keys.setdefault(leader, set())
            if key in keys:
                continue
            keys.add(key)
            self._fills.setdefault(leader, []).append(fill)
            if fill.oid:
                self._by_oid.setdefault((leader, fill.oid), []).append(fill)
            self._newest[leader] = max(self._newest.get(leader, 0.0), fill.time)
            self.last_at = max(self.last_at, time.time())
            added.append(fill)
        if added:
            self._prune()
        return added

    def _prune(self):
        cutoff = time.time() - self.window
        for leader, fills in list(self._fills.items()):
            if len(fills) <= self.max_fills:
                keep = [fill for fill in fills if fill.time >= cutoff]
            else:
                keep = fills[-self.max_fills:]
            if len(keep) == len(fills):
                continue
            self._fills[leader] = keep
            self._keys[leader] = {self._key(fill) for fill in keep}
            by_oid = {}
            for fill in keep:
                if fill.oid:
                    by_oid.setdefault((leader, fill.oid), []).append(fill)
            for key in [k for k in self._by_oid if k[0] == leader]:
                self._by_oid.pop(key, None)
            self._by_oid.update(by_oid)

    def newest(self, leader):
        return self._newest.get(leader, 0.0)

    def has_oid(self, leader, oid, since=0.0):
        return any(fill.time >= since for fill in self._by_oid.get((leader, oid), ()))

    def fills_for_oid(self, leader, oid, since=0.0):
        return [fill for fill in self._by_oid.get((leader, oid), ())
                if fill.time >= since]

    def filled_size(self, leader, oid, since=0.0):
        """Total leader quantity filled against one order id."""
        return sum(fill.size for fill in self.fills_for_oid(leader, oid, since))

    def fills(self, leader, since=0.0):
        return [fill for fill in self._fills.get(leader, ()) if fill.time >= since]


async def fetch_recent_fills(leader, address, session, since_ms):
    """REST backfill used after a websocket reconnect."""
    payload = {
        "type": "userFillsByTime",
        "user": address,
        "startTime": int(since_ms),
        "aggregateByTime": False,
    }
    rows = await _post(session, payload)
    return rows or []


def session_timeout(seconds=15):
    return aiohttp.ClientTimeout(total=seconds)
