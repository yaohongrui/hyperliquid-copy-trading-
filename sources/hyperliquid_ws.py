import asyncio, json, logging
try: import websockets
except ImportError: websockets=None
log=logging.getLogger(__name__)
class HyperliquidWS:
 def __init__(self,url="wss://api.hyperliquid.xyz/ws",addresses=(), reconnect_seconds=3, mirror_addresses=()):
  self.url=url; self.addresses=list(addresses); self.reconnect_seconds=reconnect_seconds; self._ws=None
  # Leaders opted into limit-order mirroring also need their fills: that is
  # how the mirror tells maker from taker and how it notices an order executed.
  # ``webData2`` is not subscribed: the live API rejects that request type.
  self.mirror_addresses=list(mirror_addresses)
 async def set_addresses(self,addresses,mirror_addresses=None):
  """Update subscriptions; drop the socket so the stream reconnects."""
  addresses=list(addresses)
  mirrors=list(self.mirror_addresses if mirror_addresses is None else mirror_addresses)
  if addresses==self.addresses and mirrors==self.mirror_addresses: return
  self.addresses=addresses
  self.mirror_addresses=mirrors
  ws=self._ws
  if ws is not None:
   try: await ws.close()
   except Exception: log.warning('Failed to close HL websocket for resubscribe',exc_info=True)
 async def stream(self):
  if websockets is None: raise RuntimeError('websockets package required')
  while True:
   try:
    async with websockets.connect(self.url,ping_interval=20) as ws:
     self._ws=ws
     try:
      log.info('HL websocket connected; subscribing to %d leader(s), %d mirrored',
               len(self.addresses), len(self.mirror_addresses))
      for address in self.addresses:
       log.info('HL subscribing to %s', address)
       await ws.send(json.dumps({'method':'subscribe','subscription':{'type':'allDexsClearinghouseState','user':address}}))
      for address in self.mirror_addresses:
       log.info('HL subscribing to fills for %s', address)
       await ws.send(json.dumps({'method':'subscribe','subscription':{'type':'userFills','user':address}}))
      if len(self.mirror_addresses)==1:
       # orderUpdates carries no user field, so it is only attributable when a
       # single leader is mirrored.  With several, REST polling covers it.
       log.info('HL subscribing to order updates for %s', self.mirror_addresses[0])
       await ws.send(json.dumps({'method':'subscribe','subscription':{'type':'orderUpdates','user':self.mirror_addresses[0]}}))
      # The stream is change-driven: closes that happened while the socket was
      # down are never replayed.  Ask the consumer to re-pull a REST snapshot
      # once per (re)connect so the diff baseline sees them.
      yield {'channel': 'resync'}
      async for raw in ws: yield json.loads(raw)
     finally:
      if self._ws is ws: self._ws=None
   except asyncio.CancelledError: raise
   except Exception as exc: log.warning('HL websocket disconnected: %s',exc); await asyncio.sleep(self.reconnect_seconds)
