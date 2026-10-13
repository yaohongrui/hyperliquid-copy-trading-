from datetime import datetime, timezone
class RiskManager:
 def __init__(self,config=None): self.config=config or {}; self.baseline=None; self.locked=False
 def set_baseline(self,equity): self.baseline=float(equity)
 def check(self,equity):
  if self.baseline is None: self.set_baseline(equity)
  loss=self.baseline-equity; hard=self.config.get('max_daily_loss_usdt')
  pct=self.config.get('max_daily_drawdown_pct')
  hit=(hard is not None and loss>=hard) or (pct is not None and loss/max(self.baseline,1e-9)*100>=pct)
  if hit: self.locked=True
  return not self.locked
 def reset_if_new_day(self,equity,last_day=None):
  day=datetime.now(timezone.utc).date().isoformat()
  if day != last_day: self.baseline=float(equity); self.locked=False
  return day
