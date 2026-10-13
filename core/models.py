from dataclasses import dataclass, field
from typing import Literal

Side = Literal["long", "short"]
@dataclass(frozen=True)
class Position:
    symbol: str
    size: float
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    @property
    def side(self): return "long" if self.size >= 0 else "short"
    @property
    def abs_size(self): return abs(self.size)
@dataclass(frozen=True)
class Signal:
    leader: str; symbol: str; kind: str; delta: float; target_size: float
    side: Side; price: float = 0.0; timestamp: float = 0.0
@dataclass
class Order:
    id: str; symbol: str; side: str; amount: float; price: float|None=None; status: str="open"; reduce_only: bool=False
@dataclass
class Protection:
    stop_id: str|None=None; take_profit_id: str|None=None
@dataclass
class AccountState:
    paused: bool=False; risk_locked: bool=False; daily_baseline: float|None=None
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    protection: dict[str, Protection] = field(default_factory=dict)
