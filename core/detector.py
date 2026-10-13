from .models import Position, Signal

def _kind(old: float, new: float) -> str:
    if old == 0 and new != 0: return "open"
    if old == 0 and new == 0: return "noop"
    if old * new < 0: return "close_reverse"
    if abs(new) > abs(old): return "increase"
    if abs(new) < abs(old): return "decrease" if new else "close"
    return "noop"

def diff_positions(leader: str, previous: dict[str, Position], current: dict[str, Position], coefficient: float = 1.0):
    signals=[]
    for symbol in sorted(set(previous)|set(current)):
        old, new = previous.get(symbol), current.get(symbol)
        ov, nv = (old.size if old else 0.0), (new.size if new else 0.0)
        target = nv * coefficient
        delta = target - ov * coefficient
        kind = _kind(ov, nv)
        if kind != "noop": signals.append(Signal(leader, symbol, kind, delta, target, "long" if nv >= 0 else "short", new.mark_price if new else old.mark_price))
    return signals
