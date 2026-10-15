from abc import ABC, abstractmethod

class OrderQuantityTooSmall(ValueError):
 pass

class Exchange(ABC):
 @abstractmethod
 async def create_order(self,symbol,side,amount,order_type='market',price=None,reduce_only=False): ...
 @abstractmethod
 async def fetch_positions(self): ...
 async def fetch_open_orders(self): return []
 async def cancel_order(self,order_id,symbol=None): return None
 async def cancel_protection_orders(self, symbol, keep_id=None): return None
 async def set_protection(self,*args,**kwargs): return {}
 async def cancel_protection(self,*args,**kwargs): return None
 async def fetch_position_detail(self,symbol): return None
 async def close_all_positions(self): return 0
