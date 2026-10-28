# Web console

The console is served by the existing aiohttp process on `127.0.0.1:8766`.
For the configured public hostname, install Caddy and use
`systemd/yhr.lol.Caddyfile`; Caddy obtains and renews HTTPS automatically.

Open `https://yhr.lol/`. The console stores snapshots and normalized exchange
trades in `data/console.sqlite3`. It refreshes every three seconds and sends
updates over SSE. The web controls can pause/resume new entries, update
leader sizing, blacklist, delayed-entry, and position-stop-loss settings, and
move an individual account position's native stop to its entry price
("Breakeven") or revert it. Leaders whose `source` is not `hyperliquid` show
their Binance portfolio ID instead of a copy address; the portfolio ID and the
`position_sources` block are configuration-only and require a restart to
change. Disabled leaders stay listed on the Configuration
page (and are marked as disabled) so they can be re-enabled without a restart;
they produce no signals until enabled and are hidden from the Monitor page.
The console has no login; access to the public hostname grants control of the
running engine.
API keys, exchange selection, and manual orders are intentionally unavailable
from the web UI. Account positions, open orders, trades, and performance rows
show the Hyperliquid symbol name when `symbol_map` maps the venue instrument.

The Performance page shows realized gross PnL, fees, cash-flow-adjusted
return, drawdown, and trader aggregates; `/api/v1/performance/overview` also
exposes funding and trade counts.
Exchange deposits, withdrawals, and funding history are normalized when the
configured CCXT adapter supports them. Existing databases are upgraded in
place when the console starts; records that cannot be matched to local fills
remain under the `unattributed` trader bucket.
