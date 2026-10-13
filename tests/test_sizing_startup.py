import unittest
from core.engine import Engine
from core.models import Position, Order
class Exchange:
 config={'fixed_balance_usdt':100}
 def __init__(self): self.orders=[]; self.live=0; self.quote=0.07
 async def equity(self): return 82.38
 async def position_size(self,s): return self.live
 async def entry_quote(self,s,side): return self.quote
 async def create_order(self,*args):
  self.orders.append(args); self.live+=args[2] * (-1 if args[1]=='sell' else 1)
  return Order('1',args[0],args[1],args[2],self.quote,'closed')
class Tests(unittest.IsolatedAsyncioTestCase):
 async def test_sizing_restart_and_increment(self):
  e=Exchange(); cfg={'unnamed':{'total_margin':500000,'coefficient':6}}
  engine=Engine(e,cfg); p={'S':Position('S',-362059,0.068539)}
  await engine.process('unnamed',p)
  self.assertAlmostEqual(e.orders[0][2],434.4708)
  await Engine(e,cfg).process('unnamed',p)
  self.assertEqual(len(e.orders),1)
 async def test_unfavorable_then_increase(self):
  e=Exchange(); e.quote=0.06
  engine=Engine(e,{'unnamed':{'total_margin':500000,'coefficient':6,'late_entry_enabled':True}})
  await engine.process('unnamed',{'S':Position('S',-1000,0.068539)})
  self.assertEqual(e.orders,[])
  await engine.process('unnamed',{'S':Position('S',-1100,0.068539)})
  self.assertAlmostEqual(e.orders[0][2],0.12)
  self.assertEqual(e.orders[0][3],'market')
 async def test_no_false_close_after_skipped(self):
  e=Exchange(); e.quote=0.06
  engine=Engine(e,{'unnamed':{'total_margin':500000,'coefficient':6}})
  await engine.process('unnamed',{'S':Position('S',-1000,0.068539)})
  await engine.process('unnamed',{})
  self.assertEqual(e.orders,[])
if __name__=='__main__': unittest.main()
