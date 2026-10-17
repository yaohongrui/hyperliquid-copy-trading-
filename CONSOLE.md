# Web console

The console is served by the existing aiohttp process on `127.0.0.1:8766`.
For the configured public hostname, install Caddy and use
`systemd/yhr.lol.Caddyfile`; Caddy obtains and renews HTTPS automatically.

Open `https://yhr.lol/`. The console stores snapshots and normalized exchange
trades in `data/console.sqlite3`. It refreshes every three seconds and sends
updates over SSE. The web controls can pause/resume new entries and update
leader sizing, blacklist, delayed-entry, and position-stop-loss settings.
The console has no login; access to the public hostname grants control of the
running engine.
API keys, exchange selection, and manual orders are intentionally unavailable
from the web UI. Account positions, open orders, trades, and performance rows
show the Hyperliquid symbol name when `symbol_map` maps the venue instrument.

The Performance page and `/api/v1/performance/overview` expose realized gross
PnL, fees, funding, cash-flow-adjusted return, drawdown, and trader aggregates.
Exchange deposits, withdrawals, and funding history are normalized when the
configured CCXT adapter supports them. Existing databases are upgraded in
place when the console starts; records that cannot be matched to local fills
remain under the `unattributed` trader bucket.
