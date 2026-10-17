# HL 跟单机器人

Hyperliquid 交易员跟单系统：订阅 HL 官方 WebSocket 监控交易员仓位变化，通过 CCXT 在 Gate / Binance 合约账户实时镜像下单，带 Telegram 管理机器人和 Web 控制台。

## 功能

### 跟单核心
- 订阅 HL `allDexsClearinghouseState`，对比前后仓位生成信号：`open` / `increase` / `decrease` / `close`（反手 `close_reverse` 一律跳过，需手动处理）
- 仓位计算：`目标仓位 = 交易员仓位 × (fixed_balance_usdt / total_margin × coefficient)`；`fixed_balance_usdt` 不填则用实时权益
- 除延迟入场用限价单外，检测到即市价复制
- **symbol 所有权**：同一个币同一账户只归一个交易员，避免多交易员互相打架
- **小单累计（冰山单）**：下单量低于交易所最小值时记入 carry，累计够了再一起下；交易员减仓时先抵消未成交的加仓
- **启动补仓**：重启时对比交易员现有仓位，现价比其入场价有利才补齐，否则跳过；跳过后交易员再加仓则无条件跟

### 风控
- **仓位止损**（`position_stop_loss`）：按亏损额（USDT）换算触发价，用交易所原生条件单（不经程序盯盘）；每 5s 对账，仓位变动/手动平仓自动重建或撤销，平仓即清理
- **账户硬止损**（`hard_stop`）：`max_daily_loss_usdt` 或 `max_daily_drawdown_pct`，基线为 UTC 00:00 的账户权益；触发后市价全平并暂停新开仓

### 延迟入场（`late_entry`）
- 每个交易员可单独开关；开仓信号挂限价单，价格比交易员入场价偏移 `late_entry_offset_pct`%

### Telegram
- 命令：`/status` `/positions` `/leader_positions` `/pause` `/resume`
- 通知：成交（价格/数量/方向）、已接受、失败（原因）、跳过（原因），持仓报告可发图片（`report_format: image|text`）
- 只响应配置的 chat_id，重启不重放旧命令

### Web 控制台（`127.0.0.1:8766`，Caddy 反代 HTTPS）
- Dashboard：我的持仓/挂单/权益、交易员持仓，SSE 3 秒刷新
- Performance：已实现盈亏、手续费、资金费、现金流调整后收益率、回撤、分交易员聚合；成交按本地 fill 归因到交易员，对不上的进 `unattributed`
- 控件：暂停/恢复、改交易员参数（coefficient、total_margin、黑名单、延迟入场、仓位止损等，写回 config.yaml 并热生效），带审计日志
- 数据存 SQLite（`console.database`）
- 无登录，域名能打开就能控制引擎；API key 和手动下单故意不开放

## 配置（`config/config.yaml`）

支持 `${ENV_VAR}` 占位符；自动加载同目录 `lead.env`。

- `hyperliquid`: `ws_url`, `reconnect_seconds`
- `execution_accounts.<name>`: `exchange`（gate/binance）、`api_key`/`secret_key`、`settle`、`margin_mode`、`leverage`、`dry_run`、`hard_stop`、`fixed_balance_usdt`
- `leaders[]`: `name`、`address`（HL 地址）、`execution_account`、`coefficient`、`total_margin`、`enabled`、`symbol_blacklist`、`late_entry_enabled`、`late_entry_offset_pct`、`position_stop_loss.enabled` / `.max_loss_usdt`
- `state.path`: 状态文件（symbol 所有权、小单 carry、风控基线、止损单 id），每次变动原子写入
- `console`: `database`、`domain`
- `telegram`: `enabled`、`bot_token`、`chat_id`、`polling_timeout`、`report_format`

## 运行

```bash
pip install -r requirements.txt
python validate.py config/config.yaml   # 校验配置
python main.py --config config/config.yaml
pytest tests/                           # 测试
```

生产部署用 `systemd/hyper.service`，HTTPS 用 `systemd/yhr.lol.Caddyfile`（Caddy 自动签发证书）。详见 `CONSOLE.md`。
