import argparse, asyncio, logging
from functools import partial
from aiohttp import web
from api.app import create_app
from config.loader import load
from exchanges.dry_run import DryRunExchange
from exchanges.ccxt_exchange import CcxtExchange
from core.engine import Engine
from sources.position_queries import account_positions, leader_positions, leader_snapshot
from sources.hyperliquid_ws import HyperliquidWS
from telegram.bot import TelegramManager
from state.store import StateStore

log = logging.getLogger("hyper")

def _address(value):
    """Normalize YAML-parsed 0x addresses (PyYAML may parse them as ints)."""
    return hex(value) if isinstance(value, int) else str(value)

async def main(path):
    c = load(path)
    leaders = {x["name"]: x for x in c.get("leaders", []) if x.get("enabled", True)}
    enabled = list(leaders.values())
    accounts = c.get("execution_accounts") or {}
    account_name = enabled[0].get("execution_account") if enabled else None
    account = accounts.get(account_name) if account_name else None
    if not account:
        raise RuntimeError("No execution account is configured for an enabled leader")
    dry_run = bool(account.get("dry_run", False))
    exchange = DryRunExchange(account) if dry_run else CcxtExchange(account)
    engine = Engine(exchange, leaders, state_store=StateStore((c.get('state') or {}).get('path', 'data/state.json')))
    mode = "dry-run" if dry_run else "LIVE"
    log.info("HL copytrade started in %s mode; leaders=%s account=%s", mode, ",".join(leaders) or "none", account_name)
    stop = asyncio.Event()
    telegram = None
    telegram_task = None
    api_runner = None
    telegram_config = c.get("telegram") or {}
    if telegram_config.get("enabled", False):
        token = telegram_config.get("bot_token")
        chat_id = telegram_config.get("chat_id")
        if token and chat_id and not str(token).startswith("${") and not str(chat_id).startswith("${"):
            telegram = TelegramManager(
                engine,
                token,
                chat_id,
                telegram_config.get("polling_timeout", 25),
                fetch_account_positions=partial(account_positions, c.get("execution_accounts") or {}, leaders),
                fetch_leader_positions=partial(leader_positions, leaders),
                fetch_equity=exchange.equity,
                account_name=account_name,
                account_config=account,
                mode=mode,
                report_format=telegram_config.get("report_format", "image"),
            )
            engine.notifier = telegram
            telegram_task = asyncio.create_task(telegram.run(), name="telegram-bot")
        else:
            log.warning("Telegram is enabled but bot_token/chat_id are not configured")
    else:
        telegram = TelegramManager(
            engine, "", None,
            fetch_account_positions=partial(account_positions, c.get("execution_accounts") or {}, leaders),
            fetch_leader_positions=partial(leader_positions, leaders),
            fetch_equity=getattr(exchange, "equity", None),
            account_name=account_name,
            account_config=account,
            mode=mode,
            report_format=telegram_config.get("report_format", "image"),
        )
    if not dry_run:
        await exchange.start()
        await engine.prepare_startup()
    # WebSocket updates are change-driven and may not send a restart snapshot.
    # Seed the engine from the official REST state so startup sync is evaluated.
    for name, leader_cfg in leaders.items():
        try:
            await engine.process(name, await leader_snapshot(leader_cfg))
            log.info("Initial leader snapshot processed for %s", name)
        except Exception:
            log.exception("Initial leader snapshot failed for %s", name)
    ws = HyperliquidWS(
        (c.get("hyperliquid") or {}).get("ws_url", "wss://api.hyperliquid.xyz/ws"),
        [_address(item.get("address")) for item in enabled],
        (c.get("hyperliquid") or {}).get("reconnect_seconds", 3),
    )
    ws_task = asyncio.create_task(_consume_leaders(ws, engine, leaders), name="hyperliquid-ws")
    protection_task = asyncio.create_task(_reconcile_protections(engine, stop), name="protection-reconciler")
    api_runner = web.AppRunner(create_app(telegram))
    await api_runner.setup()
    await web.TCPSite(api_runner, "127.0.0.1", 8766).start()
    try:
        await stop.wait()
    finally:
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)
        protection_task.cancel()
        await asyncio.gather(protection_task, return_exceptions=True)
        if not dry_run:
            await exchange.stop()
        if telegram:
            telegram.stop()
        if telegram_task:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
        if api_runner:
            await api_runner.cleanup()
        log.info("HL copytrade stopped")


async def _consume_leaders(source, engine, leaders):
    address_map = {_address(item.get("address")).lower(): name for name, item in leaders.items()}
    async for event in source.stream():
        log.debug("HL event channel=%s", event.get("channel"))
        if event.get("channel") != "allDexsClearinghouseState":
            continue
        data = event.get("data") or {}
        address = str(data.get("user", "")).lower()
        leader = address_map.get(address)
        if not leader:
            log.warning("HL event for unknown leader address %s", address)
            continue
        positions = {}
        if "clearinghouseStates" not in data:
            log.error("Invalid HL snapshot: missing clearinghouseStates")
            continue
        for row in [row for _, state in data["clearinghouseStates"] for row in state.get("assetPositions", [])]:
            position = row.get("position") or row
            try:
                size = float(position.get("szi", 0))
            except (TypeError, ValueError):
                continue
            if not size:
                continue
            coin = str(position.get("coin", "")).upper()
            positions[coin] = __import__("core.models", fromlist=["Position"]).Position(
                coin, size, float(position.get("entryPx") or 0),
                float(position.get("markPx") or float(position.get("positionValue") or 0) / abs(size)), float(position.get("unrealizedPnl") or 0),
                float((position.get("leverage") or {}).get("value", 1)) if isinstance(position.get("leverage"), dict) else 1,
            )
        try:
            signals = await engine.process(leader, positions)
            if signals:
                log.info("Leader %s produced %d signal(s): %s", leader, len(signals), ", ".join(f"{s.kind} {s.symbol} {s.delta:g}" for s in signals))
        except Exception:
            log.exception("Leader event processing failed for %s", leader)


async def _reconcile_protections(engine, stop, interval=5):
    """Keep manually changed positions and exchange-native stop orders aligned."""
    while not stop.is_set():
        try:
            await engine.reconcile_protections()
        except Exception:
            log.exception("Protection reconciliation failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        pass
