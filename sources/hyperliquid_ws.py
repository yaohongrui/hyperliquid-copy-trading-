import asyncio, json, logging
try: import websockets
except ImportError: websockets=None
log=logging.getLogger(__name__)
class HyperliquidWS:
 def __init__(self,url="wss://api.hyperliquid.xyz/ws",addresses=(), reconnect_seconds=3): self.url=url; self.addresses=list(addresses); self.reconnect_seconds=reconnect_seconds
 async def stream(self):
  if websockets is None: raise RuntimeError('websockets package required')
  while True:
   try:
    async with websockets.connect(self.url,ping_interval=20) as ws:
     log.info('HL websocket connected; subscribing to %d leader(s)', len(self.addresses))
     for address in self.addresses:
      log.info('HL subscribing to %s', address)
      await ws.send(json.dumps({'method':'subscribe','subscription':{'type':'allDexsClearinghouseState','user':address}}))
     # The stream is change-driven: closes that happened while the socket was
     # down are never replayed.  Ask the consumer to re-pull a REST snapshot
     # once per (re)connect so the diff baseline sees them.
     yield {'channel': 'resync'}
     async for raw in ws: yield json.loads(raw)
   except asyncio.CancelledError: raise
   except Exception as exc: log.warning('HL websocket disconnected: %s',exc); await asyncio.sleep(self.reconnect_seconds)
