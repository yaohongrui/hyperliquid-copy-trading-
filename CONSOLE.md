# Web console

The console is served by the existing aiohttp process on `127.0.0.1:8766`.
For the configured public hostname, install Caddy and use
`systemd/yhr.lol.Caddyfile`; Caddy obtains and renews HTTPS automatically.

Set credentials before starting the service:

```sh
export CONSOLE_USERNAME=admin
export CONSOLE_PASSWORD='use-a-long-random-password'
```

Open `https://yhr.lol/`. The console stores snapshots and normalized exchange
trades in `data/console.sqlite3`. It refreshes every three seconds and sends
updates over SSE. The web controls can pause/resume new entries and update
leader sizing, blacklist, delayed-entry, and position-stop-loss settings.
API keys, exchange selection, and manual orders are intentionally unavailable
from the web UI.
