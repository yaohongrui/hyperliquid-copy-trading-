import itertools
from core.models import Order
from .base import Exchange
class DryRunExchange(Exchange):
 def __init__(self): self.orders={}; self.positions={}; self._ids=itertools.count(1)
 async def create_order(self,symbol,side,amount,order_type='market',price=None,reduce_only=False):
  oid=str(next(self._ids)); o=Order(oid,symbol,side,amount,price,'closed' if order_type=='market' else 'open',reduce_only); self.orders[oid]=o
  if order_type=='market': self.positions[symbol]=self.positions.get(symbol,0)+(amount if side=='buy' else -amount)
  return o
 async def fetch_positions(self): return self.positions.copy()
 async def cancel_order(self,order_id,symbol=None):
  if order_id in self.orders: self.orders[order_id].status='canceled'
