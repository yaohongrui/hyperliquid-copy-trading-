from abc import ABC, abstractmethod
class Exchange(ABC):
 @abstractmethod
 async def create_order(self,symbol,side,amount,order_type='market',price=None,reduce_only=False): ...
 @abstractmethod
 async def fetch_positions(self): ...
 async def fetch_open_orders(self): return []
 async def cancel_order(self,order_id,symbol=None): return None
 async def set_protection(self,*args,**kwargs): return {}
