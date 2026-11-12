import argparse, asyncio, logging, time
from functools import partial
from aiohttp import web
from api.app import create_app
from config.loader import load
from exchanges.dry_run import DryRunExchange
from exchanges.hyperliquid_exchange import HyperliquidExchange
from core.engine import Engine
from core.symbols import SymbolMap
from sources.position_queries import account_positions, leader_positions, leader_snapshot
from sources.hyperliquid_ws import HyperliquidWS
from telegram.bot import RoutingNotifier, TelegramManager
from state.store import StateStore
from api.console import ConsoleService
from config.manager import ConfigManager

log = logging.getLogger("hyper")

def _address(value):
    """Normalize YAML-parsed 0x addresses (PyYAML may parse them as ints)."""
    return hex(value) if isinstance(value, int) else str(value)

async def main(path):
    c = load(path)
    # Keep disabled leaders in the engine so the console can list and
    # re-enable them; ``enabled`` gates signals in Engine.process.
    leaders = {x["name"]: x for x in c.get("leaders", [])}
    enabled = [x for x in leaders.values() if x.get("enabled", True)]
    active = {x["name"]: x for x in enabled}
    # Every leader is fed by the built-in Hyperliquid websocket/REST feed.
    hl_leaders = leaders
    enabled_hl = active
    accounts = c.get("execution_accounts") or {}
    account_name = (enabled or list(leaders.values()) or [{}])[0].get("execution_account")
    account = accounts.get(account_name) if account_name else None
    if not account:
        raise RuntimeError("No execution account is configured for a leader")
    dry_run = bool(account.get("dry_run", False))
    symbol_map = SymbolMap(c.get("symbol_map"))
    exchange = DryRunExchange(account, symbol_map) if dry_run else HyperliquidExchange(account, symbol_map)
    engine = Engine(exchange, leaders, state_store=StateStore((c.get('state') or {}).get('path', 'data/state.json')), symbol_map=symbol_map, auto_breakeven=c.get('auto_breakeven'), stop_out_cooldown=c.get('stop_out_cooldown_seconds', 600))
    engine.config_manager = ConfigManager(path)
    mode = "dry-run" if dry_run else "LIVE"
    log.info("HL copytrade started in %s mode; leaders=%s account=%s", mode, ",".join(active) or "none", account_name)
    stop = asyncio.Event()
    telegram = None
    telegram_task = None
    alerts = None
    alerts_task = None
    ws_task = None
    protection_task = None
    api_runner = None
    exchange_started = False
    telegram_config = c.get("telegram") or {}

    async def _leader_report():
        # Leaders can be toggled at runtime, so always read the live map.
        current = {name: cfg for name, cfg in engine.leaders.items() if cfg.get("enabled", True)}
        return await leader_positions(current)

    def _telegram_manager(token, chat_id):
        return TelegramManager(
            engine,
            token,
            chat_id,
            telegram_config.get("polling_timeout", 25),
            fetch_account_positions=partial(account_positions, c.get("execution_accounts") or {}, active, symbol_map),
            fetch_leader_positions=_leader_report,
            fetch_equity=getattr(exchange, "equity", None),
            account_name=account_name,
            account_config=account,
            mode=mode,
            report_format=telegram_config.get("report_format", "image"),
        )

    if telegram_config.get("enabled", False):
        token = telegram_config.get("bot_token")
        chat_id = telegram_config.get("chat_id")
        if token and chat_id and not str(token).startswith("${") and not str(chat_id).startswith("${"):
            telegram = _telegram_manager(token, chat_id)
            telegram_task = asyncio.create_task(telegram.run(), name="telegram-bot")
        else:
            log.warning("Telegram is enabled but bot_token/chat_id are not configured")
    if telegram is None:
        # Keep a manager for HTTP /command and for normal-channel fallback; it
        # has no session, so its sends are best-effort no-ops.
        telegram = _telegram_manager("", None)

    alerts_config = telegram_config.get("alerts") or {}
    if alerts_config.get("enabled", False):
        alert_token = alerts_config.get("bot_token")
        alert_chat_id = alerts_config.get("chat_id")
        if (alert_token and alert_chat_id
                and not str(alert_token).startswith("${")
                and not str(alert_chat_id).startswith("${")):
            alerts = TelegramManager(
                engine,
                alert_token,
                alert_chat_id,
                telegram_config.get("polling_timeout", 25),
                account_name=account_name,
                account_config=account,
                mode=mode,
            )
            alerts_task = asyncio.create_task(
                alerts.run_sender(
                    "🛡️ <b>ALERTS ONLINE</b>\nHigh-priority alerts are routed to this channel."),
                name="telegram-alerts",
            )
        else:
            log.warning("Telegram alerts are enabled but bot_token/chat_id are not configured")

    notifier = RoutingNotifier(telegram, alerts)
    engine.notifier = notifier
    try:
        if not dry_run:
            await exchange.start()
            exchange_started = True
            try:
                exchange.validate_symbol_map()
            except ValueError as exc:
                raise RuntimeError(f"invalid symbol_map: {exc}") from exc
            await engine.prepare_startup()
        ws = HyperliquidWS(
            (c.get("hyperliquid") or {}).get("ws_url", "wss://api.hyperliquid.xyz/ws"),
            _hyperliquid_addresses(enabled_hl),
            (c.get("hyperliquid") or {}).get("reconnect_seconds", 3),
        )
        console = ConsoleService(engine, c, (c.get('console') or {}).get('database', 'data/console.sqlite3'), ws)
        # Installs predating the entered map have no record of which symbols were
        # actually filled.  Rebuild it from local fills; otherwise a restart after
        # an exchange-native stop looks like a never-filled entry and re-enters.
        await _seed_entered(engine, console.store)
        # WebSocket updates are change-driven and may not send a restart snapshot.
        # Seed the engine from the official REST state so startup sync is evaluated.
        for name, leader_cfg in enabled_hl.items():
            try:
                await engine.process(name, await leader_snapshot(leader_cfg))
                log.info("Initial leader snapshot processed for %s", name)
            except Exception:
                log.exception("Initial leader snapshot failed for %s", name)
        ws_task = asyncio.create_task(
            _consume_leaders(ws, engine, hl_leaders, (c.get("hyperliquid") or {}).get("silence_seconds", 120)),
            name="hyperliquid-ws",
        )
        protection_task = asyncio.create_task(
            _reconcile_protections(engine, stop, c.get('protection_reconcile_seconds', 5)),
            name="protection-reconciler")
        api_runner = web.AppRunner(create_app(telegram, console))
        await api_runner.setup()
        await web.TCPSite(api_runner, "127.0.0.1", 8766).start()
        await stop.wait()
    finally:
        tasks = [task for task in (ws_task, protection_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if exchange_started:
            await exchange.stop()
        if telegram:
            telegram.stop()
        if telegram_task:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
        if alerts:
            alerts.stop()
        if alerts_task:
            alerts_task.cancel()
            await asyncio.gather(alerts_task, return_exceptions=True)
        if api_runner:
            await api_runner.cleanup()
        log.info("HL copytrade stopped")


def _hyperliquid_addresses(leaders):
    """Addresses for the HL subscription: enabled Hyperliquid leaders only."""
    return [
        _address(cfg.get("address"))
        for cfg in leaders.values()
        if cfg.get("enabled", True)
    ]


async def _seed_entered(engine, store):
    """Rebuild ``entered`` from local fills for symbols we still hold.

    A fill history alone cannot tell whether a position is still open: a
    reversal near the exchange minimum can close it while the stored fill was
    recorded as a non-reduce entry.  Seeding ``entered`` for a flat account
    would make reconciliation mark a false stop-out, so verify the live
    position first (a failed probe keeps the old conservative behaviour).
    """
    if not engine.leaders:
        return
    names = list(engine.leaders)
    primary = next((name for name in names if engine.leaders[name].get("enabled", True)), names[0])
    account_key = engine._account_key(primary)
    for symbol, leader in store.latest_open_fill_leaders(account_key).items():
        # Fills of a leader since removed from the config must not resurrect
        # entry bookkeeping for a leader that can no longer receive signals.
        if leader not in engine.leaders:
            continue
        try:
            live = await engine.exchange.position_size(symbol)
        except Exception:
            live = None
        if live is not None and abs(live) <= 1e-12:
            continue
        engine.entered.setdefault(f"{leader}:{symbol}", True)
    engine._save_state()


async def _consume_leaders(source, engine, leaders, silence_seconds=120):
    # The subscription is change-driven, so a healthy socket can be quiet for a
    # long time.  The watchdog only surfaces a silence long enough that a dead
    # subscription is more likely than idle leaders; the console's low-rate
    # REST fallback keeps engine state correct either way.
    watch = {"last_event": time.monotonic()}
    watchdog = asyncio.create_task(_ws_watchdog(watch, float(silence_seconds)), name="hyperliquid-watchdog")
    try:
        async for event in source.stream():
            watch["last_event"] = time.monotonic()
            log.debug("HL event channel=%s", event.get("channel"))
            if event.get("channel") == "resync":
                # Websocket (re)connected: closes that fired during an outage are
                # never replayed, so re-seed every leader from a REST snapshot.
                for name, cfg in leaders.items():
                    if not cfg.get("enabled", True):
                        continue
                    try:
                        await engine.process(name, await leader_snapshot(cfg), resync=True)
                    except Exception:
                        log.exception("Leader snapshot resync failed for %s", name)
                continue
            if event.get("channel") != "allDexsClearinghouseState":
                continue
            data = event.get("data") or {}
            address = str(data.get("user", "")).lower()
            address_map = {_address(item.get("address")).lower(): name for name, item in leaders.items()}
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
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def _ws_watchdog(watch, silence_seconds, interval=15):
    warned = False
    while True:
        await asyncio.sleep(interval)
        silent = time.monotonic() - watch["last_event"]
        if silent < silence_seconds:
            warned = False
        elif not warned:
            log.warning("HL websocket delivered no events for %.0fs (threshold %.0fs); REST fallback is maintaining leader state", silent, silence_seconds)
            warned = True


async def _reconcile_protections(engine, stop, interval=5):
    """Keep manually changed positions and exchange-native stop orders aligned."""
    while not stop.is_set():
        try:
            await engine.check_hard_stop()
            await engine.reconcile_protections()
            await engine.reconcile_pending_orders()
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
