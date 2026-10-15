from datetime import datetime, timezone


class RiskManager:
    """Daily account loss guard.

    The baseline is persisted by the caller.  On the first observation of a
    UTC day there is no historical balance available, so that observation
    becomes the baseline for that day.
    """

    def __init__(self, config=None, state=None):
        self.config = config or {}
        state = state or {}
        self.baseline = state.get("baseline")
        self.day = state.get("day")
        self.locked = bool(state.get("locked", False))

    def state(self):
        return {"baseline": self.baseline, "day": self.day, "locked": self.locked}

    def evaluate(self, equity, now=None):
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(timezone.utc).date().isoformat()
        if self.day != day or self.baseline is None:
            self.day = day
            self.baseline = float(equity)
            self.locked = False
            return False
        loss = float(self.baseline) - float(equity)
        hard = self.config.get("max_daily_loss_usdt")
        pct = self.config.get("max_daily_drawdown_pct")
        hit = (hard is not None and loss >= float(hard)) or (
            pct is not None and loss / max(float(self.baseline), 1e-9) * 100 >= float(pct)
        )
        if hit:
            self.locked = True
        return self.locked

    def check(self, equity):
        return not self.evaluate(equity)
