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
        # ``locked`` means the day's loss limit was hit; ``flattened`` means the
        # account was successfully closed out after that.  Keeping them apart
        # lets a failed flatten retry without re-triggering the alert.
        self.flattened = bool(state.get("flattened", False))

    def state(self):
        return {"baseline": self.baseline, "day": self.day, "locked": self.locked,
                "flattened": self.flattened}

    def evaluate(self, equity, now=None):
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(timezone.utc).date().isoformat()
        if self.day != day or self.baseline is None:
            self.day = day
            self.baseline = float(equity)
            self.locked = False
            self.flattened = False
            return False
        loss = float(self.baseline) - float(equity)
        hard = self.config.get("max_daily_loss_usdt")
        pct = self.config.get("max_daily_drawdown_pct")
        hit = (hard is not None and loss >= float(hard)) or (
            pct is not None and loss / max(float(self.baseline), 1e-9) * 100 >= float(pct)
        )
        if hit:
            if not self.locked:
                self.flattened = False
            self.locked = True
        return self.locked

    def unlock(self, equity, now=None):
        """Clear the lock and rebaseline to current equity.

        Keeping the old baseline would immediately re-trigger the same loss,
        so a manual unlock starts a fresh daily baseline.
        """
        now = now or datetime.now(timezone.utc)
        self.day = now.astimezone(timezone.utc).date().isoformat()
        self.baseline = float(equity)
        self.locked = False
        self.flattened = False
        return self.state()

    def check(self, equity):
        return not self.evaluate(equity)
