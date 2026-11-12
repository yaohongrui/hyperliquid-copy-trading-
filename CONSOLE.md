# Web console

The console is served by the existing aiohttp process on `127.0.0.1:8766`.
For the configured public hostname, install Caddy and use
`systemd/yhr.lol.Caddyfile`; Caddy obtains and renews HTTPS automatically.

Open `https://yhr.lol/`. The console stores snapshots and normalized exchange
trades in `data/console.sqlite3`. It refreshes every three seconds and sends
updates over SSE. The web controls can pause/resume new entries, update
leader sizing, blacklist, delayed-entry, position-stop-loss, and
auto-breakeven settings, and move an individual account position's native stop
to its entry price ("Breakeven") or revert it. Disabled leaders stay listed on
the Configuration page (and are marked as disabled) so they can be re-enabled
without a restart; they produce no signals until enabled and are hidden from
the Monitor page. The console has **no login**; access to the public hostname
grants control of the running engine. Add Basic Auth, a VPN, or IP
restrictions before exposing it.

API keys, exchange selection, and manual orders are intentionally unavailable
from the web UI.

## Data sources (Hyperliquid SDK)

All performance data comes from the official `hyperliquid-python-sdk`; every
call runs through `asyncio.to_thread` so the console does not block the
engine's event loop.

| Source | SDK call | Stored as |
| --- | --- | --- |
| Fills | `Info.user_fills_by_time` (`aggregate_by_time=false`) | `trades.source = hl_fill` |
| Funding | `Info.user_funding_history` | `trades.source = hl_funding`, `funding = delta.usdc` |
| Non-funding ledger | `Info.user_non_funding_ledger_updates` | `cash_flows` (`deposit`, `withdraw`, `send`, `receive`, internal/vault transfers) |

Fills mapping:

```text
id           = hl:fill:<tid> (or hash:oid:time:coin when tid is missing)
symbol       = coin
side         = buy when side == 'B', else sell
amount       = sz
price        = px
realizedPnl  = closedPnl
fee          = fee
funding      = 0
order_id     = str(oid)
source       = hl_fill
timestamp    = time / 1000
```

Only perpetual fills that carry `closedPnl` are kept; spot fills are ignored.
Funding rows carry `source = hl_funding` and feed the PnL total but are not
counted as trades, so they cannot inflate the trade count or win rate.

Ledger rows are written to `cash_flows`; the amount is taken from
`delta.usdc`, then `delta.ustd`, then `delta.amount`. Withdrawals are always
negative. Deposit/withdraw/send/receive cash flows are used to flow-adjust the equity curve
(`equity - cumulative cash flow`).

`fetch_order(order_id, symbol=None)` uses `Info.query_order_by_oid` and returns
`{'status', 'filled', 'remaining', 'average', 'price'}`; an unknown order
returns `None`, while a query failure is raised so the engine keeps tracking
it.

### Lookback and incremental refresh

The Hyperliquid API caps responses at roughly 2000 rows. The client paginates
by splitting the time window (`performance_page_size`, default 2000) and
deduplicates by stable id.

- First start with no SQLite watermark backfills
  `console.performance_lookback_days` (default 30 days), day by day.
- After that, `ConsoleStore.latest_timestamp` supplies a watermark and each
  refresh starts `performance_overlap_seconds` (default 300 seconds) before
  it, so rows landing on a page boundary are not lost.
- Refreshes run in the background (`console.performance_seconds`, default 60)
  with a busy guard, so a large first backfill does not stall the 3-second
  dashboard loop.

## Performance page

The Performance page shows realized gross PnL, fees, funding, net PnL,
cash-flow-adjusted return, drawdown, and trader aggregates;
`/api/v1/performance/overview` also exposes funding and trade counts.

- `net_pnl = gross_pnl - fees + funding`.
- `fee` is stored as a positive cost; positive `funding` is income.
- The equity curve is adjusted by cumulative cash flows before return and
  maximum drawdown are computed.
- Curve queries are downsampled to at most 400 points.
- Funding rows (`hl_funding`) contribute to `funding`/`net_pnl` but are
  excluded from trade counts and win rate.

## Trade attribution

The engine records every real order in `local_fills`, including the reduce-only
flag and order id.

1. Exact match on `local_fills.order_id == trades.order_id`
   (`attribution = order_id`).
2. Otherwise, a symbol/time-based match against a local reduce-only fill.
3. Exchange-native stops, liquidations, and manual closes have no local fill;
   they fall back to the most recent opening fill for that symbol before the
   close time (`attribution = stop_close`).
4. `hl_funding` rows are skipped by attribution so they do not steal a closing
   fill.

Records that cannot be matched remain under the `unattributed` trader bucket;
the console never guesses.

## Retention

`equity_snapshots` are written every few seconds and grow without bound, so
they are pruned to `console.snapshot_retention_days` (default 90). Trades and
cash flows are the permanent PnL record and are not deleted. Existing databases
are upgraded in place (including the `order_id` column and millisecond →
second timestamp normalization) when the console starts.
