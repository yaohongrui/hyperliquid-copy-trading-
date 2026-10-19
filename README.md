# HL 跟单机器人

Hyperliquid 交易员跟单系统：通过 HL 官方 WebSocket 监控交易员仓位变化，用 CCXT 在 Gate / Binance 的 USDT 永续账户实时镜像下单，附带 Telegram 管理机器人和 Web 控制台。

> **实盘注意**：Web 控制台默认只监听 `127.0.0.1:8766`，本身没有登录鉴权。一旦通过 Caddy 暴露到公网，任何能打开域名的人都可以暂停引擎、修改交易员参数。API key 和手动下单接口故意不开放。

## 目录

- [功能](#功能)
- [跟单规则细节](#跟单规则细节)
- [风控](#风控)
- [配置](#配置)
- [运行与部署](#运行与部署)
- [Telegram](#telegram)
- [Web 控制台](#web-控制台)
- [状态与数据](#状态与数据)
- [项目结构](#项目结构)
- [已知限制与注意事项](#已知限制与注意事项)
- [测试](#测试)

## 功能

### 跟单核心

- 订阅 HL `allDexsClearinghouseState`，对比前后仓位生成 `open` / `increase` / `decrease` / `close` 信号；反手（`close_reverse`）一律跳过，需要手动处理。
- 启动和 WebSocket 每次重连后，都会用 REST 快照补齐基线（HIP-3 的各个 perp dex 也会一起拉取），避免断线期间的平仓事件丢失。
- 仓位换算：`目标仓位 = 交易员仓位 × (sizing_balance / total_margin × coefficient)`，`sizing_balance` 默认是账户实时权益，也可以用 `fixed_balance_usdt` 固定。
- 延迟入场：每个交易员可单独开启，开仓信号改为在交易员入场价偏移处挂限价单。
- **symbol 所有权**：同一执行账户下，一个币同时只归一个交易员，避免多个交易员互相打架。
- **小单累计（冰山单）**：下单量低于交易所最小数量/最小名义价值时，先记入 carry，累计够了再一起下；交易员减仓时优先抵消未成交的加仓。
- **启动补仓**：重启时对比交易员现有仓位和账户实际仓位，只在现价不劣于交易员入场价时补齐；不满足条件则跳过，之后交易员再加仓仍会无条件跟随。
- **止损后抑制再入场**：账户被原生止损、爆仓或手动平仓后，只要交易员还持有该币，就不再跟随加仓；直到交易员平掉该币、重新开仓，才恢复正常跟单。
- **symbol_map**：处理 HL 币名与 Gate / Binance 合约名不一致的情况（包括 HIP-3 的 `XYZ:XXX` 命名），也可以把某个币标记为完全忽略。

### 风控

- **单仓止损**（`position_stop_loss`）：按亏损金额（USDT）换算出触发价，在交易所挂原生条件单，不依赖程序盯盘。每 5 秒对账，仓位变化或手动平仓后自动重建/撤销，平仓即清理。
- **账户硬止损**（`hard_stop`）：按 UTC 自然日统计，支持 `max_daily_loss_usdt` 和 `max_daily_drawdown_pct`，触发后暂停新开仓并市价全平。
- **暂停/恢复**：Telegram 或控制台可以暂停新开仓；暂停期间减仓和平仓仍会正常执行。

### 通知与控制

- Telegram 长轮询机器人：`/status`、`/positions`、`/leader_positions`、`/pause`、`/resume`、`/unlock_hard_stop`。
- 成交、挂单被接受、下单失败、信号跳过都会推送通知，并附带原因。
- 持仓报告可渲染成 PNG 图片发送（`report_format: image|text`）。
- 只响应配置的 `chat_id`；重启时会跳过旧消息，不会重放历史命令。

### 数据与统计

- Web 控制台每 3 秒刷新账户、挂单、交易员仓位，通过 SSE 推送到浏览器。
- Performance 页面统计已实现盈亏、手续费、资金费、现金流调整后收益率、最大回撤、分交易员盈亏和胜率。
- 交易所成交会按本地订单归因到交易员；对不上的记录进入 `unattributed`，不会瞎猜。
- 数据保存在 SQLite，旧库在控制台启动时自动就地升级/清理。

## 跟单规则细节

### 信号类型

| kind | 含义 | 处理 |
| --- | --- | --- |
| `open` | 交易员从空仓开仓 | 市价；开启延迟入场时改用限价单 |
| `increase` | 同方向加仓 | 市价，并合并未成交的小单 carry |
| `decrease` | 同方向减仓 | reduce-only 市价，数量取 `min(账户持仓, 信号数量)` |
| `close` | 平仓 | reduce-only 市价，平掉账户该币全部持仓 |
| `close_reverse` | 反手 | **跳过**，旧仓不动，需要人工处理 |

### 仓位换算

```text
ratio  = sizing_balance / total_margin × coefficient
target = 交易员仓位 × ratio
delta  = (交易员新仓位 - 交易员旧仓位) × ratio
```

- `sizing_balance`：配置了 `fixed_balance_usdt` 就用它，否则用账户实时权益（Gate 为钱包余额 + 未实现盈亏，Binance 为钱包余额 + 所有仓位未实现盈亏）。
- `total_margin` 是交易员的保证金规模，只作为分母，不是你的保证金。必须大于 0。
- `coefficient` 是额外倍率，可以是小数；设为 0 等于不跟。
- 杠杆（`leverage`）只影响保证金占用，不改变跟单的名义仓位；交易所杠杆不够时下单会失败。

例：交易员 `total_margin: 500000`、`coefficient: 40`、你的 `fixed_balance_usdt: 100`，则 `ratio = 0.008`。交易员持有 1 BTC，你跟随 0.008 BTC。

### 下单方式

- `open`：默认市价；若 `late_entry_enabled: true` 且不是启动补仓，则挂限价单，价格为交易员入场价：
  - 买入：`entry × (1 - late_entry_offset_pct%)`
  - 卖出：`entry × (1 + late_entry_offset_pct%)`
  - 限价单按 `leader:symbol` 跟踪，默认 60 秒未成交自动撤单（可用 `late_entry_ttl_seconds` 调整）。交易员发出 `decrease` / `close` / `close_reverse` 时也会立即撤掉对应挂单，避免成交后形成无人管理的孤儿仓位。
  - 进程重启后未成交挂单仍会从 `state.json` 恢复跟踪并继续按 TTL 撤销。
- `increase`：始终市价（延迟入场只作用于 `open`）。
- `decrease` / `close`：reduce-only 市价。
- Gate 的市价单会显式带 `IOC`；每次非 reduce-only 下单前会把该合约杠杆设置为配置值（不超过交易所上限），Gate 的 `margin_mode: crossed` 走全仓杠杆接口。
- 下单数量按交易所 `contractSize` 从基础币数量换算成张数，并按交易所精度取整。低于最小张数或最小名义价值时，非减仓单进入小单 carry，减仓单直接跳过。

### 启动补仓

进程启动时，对每个启用的交易员先处理一次 REST 快照：

1. 计算目标仓位与账户实际仓位的差额；账户已经满足目标则跳过。
2. 账户存在反方向仓位则跳过。
3. 现价必须不劣于交易员入场价：做多要求 `现价 <= 交易员入场价`，做空要求 `现价 >= 交易员入场价`，否则跳过。
4. 满足条件才用市价补齐差额。

启动补仓只执行一次。被跳过后，如果交易员继续加仓，后续信号会正常跟随（不再做价格优劣检查）。

### symbol 所有权

- 所有权 key 是 `<execution_account>:<SYMBOL>`，持久化在 `state.json`。
- 某个交易员开仓/加仓时先抢占该 symbol；其他交易员再对该 symbol 开仓/加仓会被跳过。
- 非所有者对该 symbol 的减仓/平仓也会被跳过，防止误平别人的仓位。
- 账户该 symbol 持仓归零后释放所有权。

### 小单累计（冰山单）

- 开仓/加仓数量低于交易所最小值时，不报错重试，而是按 `交易员:symbol` 记录 `{side, amount}`。
- 下一次同方向加仓会把 carry 合并进下单量；成交后清空。
- 交易员减仓时先抵消未成交的加仓：减仓量小于 carry 就只减少 carry，不真正下单。
- symbol 被 `symbol_map` 标记为 skip 时会清空 carry。
- 重启时按当前目标仓位重新校准 carry；交易员已平仓则丢弃陈旧 carry。

### 止损后抑制再入场

- `entered` 记录该 symbol 是否真的成交过开仓单；`stopped` 记录“我们已离场、但交易员还持有”的状态。
- 当账户持仓归零、交易员仍持有，且此前确实建过仓（原生止损、爆仓、手动平仓），标记为 `stopped`。
- `stopped` 期间交易员的 `open` / `increase` 信号全部跳过，避免止损后立刻被加仓信号重新拉回场内。
- 交易员平掉该币后 `stopped` 自动清除；下一次开仓会正常复制。
- 5 秒对账也会检测这种情况，所以即使没有新的 WS 信号，手动平仓也能被识别。
- 重启时 `entered` 会从本地成交记录（`local_fills`）恢复，避免重启后把“从未成交的启动补仓”误判成止损。

## 风控

### 单仓止损（`position_stop_loss`）

每个交易员独立配置：

```yaml
position_stop_loss:
  enabled: true
  max_loss_usdt: 10
```

- 触发价按基础币数量换算：

  ```text
  持仓数量 = |contracts × contractSize|
  多头触发价 = 入场价 - max_loss_usdt / 持仓数量
  空头触发价 = 入场价 + max_loss_usdt / 持仓数量
  ```

- 用交易所原生条件单平掉整个仓位：
  - Gate：`triggerPrice` + `reduceOnly` 的市价条件单，通过 Gate 的 price-orders 接口查询/撤销。
  - Binance：`STOP_MARKET` + `closePosition=true`，触发时由交易所决定平仓数量。
- 每 5 秒对账一次，只处理引擎自己拥有的 symbol：
  - 仓位归零：释放所有权，并撤销残留条件单。
  - 仓位存在：确保条件单存在；持仓张数或触发价变化时重建。
  - 会清理同一合约下重复的 reduce-only 条件单。
- 触发价已经劣于当前标记价（做多触发价 ≥ 标记价、做空触发价 ≤ 标记价）时，撤销该币已有的条件单、不再挂新单，只记录警告。
- 状态文件里保存 `{id, contracts, trigger}`，重启后可以继续对账。
- 单仓止损只覆盖引擎自己建立的仓位；手动开的仓位没有所有权，不会自动加保护单。

### 账户硬止损（`hard_stop`）

配置在 `execution_accounts.<name>.hard_stop` 下：

```yaml
hard_stop:
  max_daily_loss_usdt: 40        # 当日亏损达到 40 USDT
  max_daily_drawdown_pct: 30     # 或当日回撤达到 30%
```

- 两个条件同时配置时是“或”的关系。
- 基线是 UTC 自然日第一次检查时的账户权益，保存在 `state.json`；跨 UTC 日后重置基线并解锁。
- 每 5 秒检查一次。触发后：
  1. 设置 `paused = true`；
  2. 撤销账户上所有普通挂单和条件单（包括延迟入场限价单），防止平仓后再次成交；
  3. 市价 reduce-only 平掉账户所有永续仓位（包括非引擎建立的仓位）；逐个仓位处理，失败会重试直到账户为空；
  4. 当日保持 `locked`；只有确认账户已空后才会记录 `flattened`，未完成平仓会持续重试。
  5. 通过 Telegram 推送一次 `HARD STOP TRIGGERED` 通知。
- 手动解除：Telegram `/unlock_hard_stop`。会以当前权益重置当日基线、清除 `locked` / `flattened` 并恢复新开仓；否则旧基线会在下一次检查时立刻再次触发。
- `locked` 会持久化，且 `process()` 在 `paused` 或 `locked` 时都会拒绝 `open` / `increase` / `close_reverse`。因此进程重启后即使 `paused` 恢复为 `false`，当日锁定期间也不会再开新仓；跨日后 `locked` 自动解除。

### 暂停/恢复

- 暂停只阻止新风险：`open`、`increase`、`close_reverse` 会被跳过。
- `decrease`、`close` 和已有的交易所条件单不受影响。
- 入口：Telegram `/pause` `/resume`、控制台按钮、HTTP `POST /api/v1/control/{pause|resume}`、`POST /command`。

## 配置

### 加载与密钥

- 主配置：`config/config.yaml`。
- 支持 `${ENV_VAR}` 占位符。
- 启动时自动加载配置文件同目录下的 `lead.env`（只填充尚未存在的环境变量，真实环境变量优先）。
- `lead.env` 已在 `.gitignore` 中，不要提交。
- `config/config.yaml.save` 不会被读取，只是手工备份。

### 顶层字段

```yaml
hyperliquid:
  ws_url: wss://api.hyperliquid.xyz/ws
  reconnect_seconds: 3
  addresses: []          # 未使用：实际订阅的是 leaders[].address
```

> `hyperliquid.addresses` 是历史字段，当前代码不使用。WebSocket 订阅的地址来自启用的 `leaders[].address`。

### execution_accounts

```yaml
execution_accounts:
  gate_primary:
    exchange: gate
    api_key: ${GATE_API_KEY}
    secret_key: ${GATE_SECRET_KEY}
    settle: usdt
    margin_mode: crossed
    leverage: 50
    dry_run: false
    fixed_balance_usdt: 100
    hard_stop:
      max_daily_drawdown_pct: 30
```

| 字段 | 说明 |
| --- | --- |
| `exchange` | `gate` 或 `binance`（Binance 走 `binanceusdm`） |
| `api_key` | 交易所 API key |
| `secret_key` | **Gate** 使用这个字段名 |
| `api_secret` | **Binance** 使用这个字段名 |
| `settle` | Gate 结算币种，默认 `usdt`；也用于 Gate 账本和条件单接口 |
| `margin_mode` | Gate 全仓请填 `crossed`（兼容 `cross`） |
| `leverage` | 下单前设置的目标杠杆；不超过交易所上限。只影响保证金，不改变仓位大小 |
| `dry_run` | `true` 时使用内存模拟交易所，不连真实账户 |
| `fixed_balance_usdt` | 固定跟单预算；不填则使用账户实时权益 |
| `hard_stop.max_daily_loss_usdt` | 当日亏损达到该 USDT 数触发硬止损 |
| `hard_stop.max_daily_drawdown_pct` | 当日回撤达到该百分比触发硬止损 |

> **一个进程实际只交易一个执行账户。** `main.py` 用第一个启用交易员的 `execution_account` 创建唯一的交易所实例；所有启用的交易员都应该指向同一个账户。给不同交易员配不同账户不会自动切换下单账户。

### leaders

```yaml
leaders:
- name: A
  address: '0x15ba...'
  source: hyperliquid
  execution_account: gate_primary
  coefficient: 40
  total_margin: 500000
  enabled: true
  symbol_blacklist: []
  late_entry_enabled: true
  late_entry_offset_pct: 0.2
  position_stop_loss:
    enabled: true
    max_loss_usdt: 10
```

| 字段 | 说明 |
| --- | --- |
| `name` | 交易员名称，用于通知、日志、所有权和归因，必须唯一 |
| `address` | HL 地址。YAML 未加引号的 `0x...` 被解析成整数时，代码会自动转回十六进制 |
| `source` | 目前未使用，固定为 `hyperliquid` 即可 |
| `execution_account` | 指向 `execution_accounts` 中的键；同一进程内应保持一致 |
| `coefficient` | 跟单倍率，非负，可为小数 |
| `total_margin` | 交易员保证金规模（分母），必须大于 0 |
| `enabled` | 是否启用。**只在进程启动时生效**，见下方注意事项 |
| `symbol_blacklist` | 黑名单，匹配 HL 币名（如 `BTC`）或加 `USDT` 后缀（如 `BTCUSDT`） |
| `late_entry_enabled` | 是否对 `open` 使用延迟限价入场 |
| `late_entry_offset_pct` | 限价相对交易员入场价的偏移百分比，非负 |
| `late_entry_ttl_seconds` | 延迟入场限价单的存活时间，默认 60 秒；超时自动撤单 |
| `position_stop_loss.enabled` | 是否启用单仓止损 |
| `position_stop_loss.max_loss_usdt` | 单仓最大亏损金额（USDT），必须大于 0 |

注意：

- 黑名单会跳过该币的**所有**信号，包括减仓和平仓。已有仓位需要手动处理，或者先移出黑名单。
- `symbol_map` 的 skip 同理：所有信号都不执行。

### state

```yaml
state:
  path: data/state.json
```

保存 symbol 所有权、止损抑制状态、已成交标记、小单 carry、单仓止损单 id、账户硬止损基线。每次变动原子写入。

### console

```yaml
console:
  database: data/console.sqlite3
  domain: yhr.lol
```

- `database`：SQLite 路径，目录会自动创建。
- `domain`：仅作文档/部署参考，代码不读取。

### telegram

```yaml
telegram:
  enabled: true
  bot_token: ${TELEGRAM_GATE_BOT_TOKEN}
  chat_id: ${TELEGRAM_GATE_CHAT_ID}
  polling_timeout: 25
  report_format: image
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | `false` 时不启动机器人，也不发送任何通知 |
| `bot_token` | BotFather 提供的 token |
| `chat_id` | 唯一允许控制的会话；其他会话的消息会被忽略 |
| `polling_timeout` | 长轮询秒数，默认 25 |
| `report_format` | `image`（默认）或 `text`；除 `text` 外都按图片处理 |

`enabled: true` 但 token/chat_id 缺失或仍是未展开的 `${...}` 时，只打印警告，不会启动机器人。

### symbol_map

顶层可选配置，把 HL 币名映射到执行交易所的合约：

```yaml
symbol_map:
  'XYZ:SKHX': SKHX/USDT:USDT   # CCXT 统一标的
  PEPE: 1000PEPE               # 裸基础币种
  AMD: AMD_USDT                # 交易所原生写法
  'XYZ:OLD': skip              # 完全忽略
  'XYZ:DEAD': null             # 同上
```

规则：

- 键是 HL 币名，大小写不敏感；值是 CCXT 统一标的、基础币种或原生写法。
- 映射目标必须是 USDT 永续；指向现货或非 USDT 合约会在启动时报错。
- 解析顺序：先精确匹配（保留 HIP-3 命名空间），未命中再按基础币种匹配。
- 基础币种匹配时，skip 条目优先于普通映射，与配置顺序无关。
- 值为 `skip`、`ignore`、`none`、`null`、空字符串或空值都表示忽略该币：所有信号都不执行，carry 会被清空。
- HIP-3 名称包含冒号，YAML 中**必须加引号**，例如 `'XYZ:SKHX'`；不加引号会被解析成键值对。
- 控制台和 Telegram 会把交易所合约名反向显示成 HL 名称。
- 实盘启动时会逐个解析所有非 skip 映射，拼写错误会直接阻止启动。
- 只支持配置文件修改，运行中修改需要重启进程。

### 完整示例

```yaml
hyperliquid:
  ws_url: wss://api.hyperliquid.xyz/ws
  reconnect_seconds: 3

execution_accounts:
  gate_primary:
    exchange: gate
    api_key: ${GATE_API_KEY}
    secret_key: ${GATE_SECRET_KEY}
    settle: usdt
    margin_mode: crossed
    leverage: 50
    dry_run: false
    fixed_balance_usdt: 100
    hard_stop:
      max_daily_drawdown_pct: 30

symbol_map:
  'XYZ:SKHX': SKHX/USDT:USDT
  'XYZ:OLD': skip

leaders:
- name: A
  address: '0x0000000000000000000000000000000000000000'
  source: hyperliquid
  execution_account: gate_primary
  coefficient: 40
  total_margin: 500000
  enabled: true
  symbol_blacklist: []
  late_entry_enabled: true
  late_entry_offset_pct: 0.2
  position_stop_loss:
    enabled: true
    max_loss_usdt: 10
- name: B
  address: '0x1111111111111111111111111111111111111111'
  source: hyperliquid
  execution_account: gate_primary
  coefficient: 2
  total_margin: 200000
  enabled: true
  symbol_blacklist: [DOGE]
  late_entry_enabled: false
  late_entry_offset_pct: 0.2
  position_stop_loss:
    enabled: true
    max_loss_usdt: 8

state:
  path: data/state.json

console:
  database: data/console.sqlite3
  domain: example.com

telegram:
  enabled: true
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: ${TELEGRAM_CHAT_ID}
  polling_timeout: 25
  report_format: image
```

## 运行与部署

环境要求：Python 3.11+。

```bash
pip install -r requirements.txt

# 校验配置（检查 leaders / execution_accounts / symbol_map 的基本结构）
python validate.py config/config.yaml

# 前台运行
python main.py --config config/config.yaml
```

- 默认配置路径是 `config/config.yaml`，可以用 `--config` 指定。
- 日志输出到 stdout，级别 INFO；下单结果、跳过原因、启动参数、硬止损都会记录。
- 实盘启动顺序：加载配置 → 创建交易所 → 加载市场 → 校验 `symbol_map` → 拉取账户仓位（失败则退出）→ 处理各交易员 REST 快照 → 启动 WebSocket → 启动控制台和 5 秒对账任务。
- `dry_run: true` 时使用内存模拟交易所：市价单立即成交、限价单保持挂单、权益取 `fixed_balance_usdt`（未配置则为 0），不会连接真实账户，也不会创建交易所原生止损单。适合验证配置和跟单流程，但不能验证交易所连通性。

### systemd

`systemd/hyper.service` 已按 `/root/hyper` 部署写好：

```bash
cp systemd/hyper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hyper
journalctl -u hyper -f
```

### HTTPS 反向代理

控制台只监听 `127.0.0.1:8766`。用 Caddy 反代：

```caddyfile
yhr.lol {
    reverse_proxy 127.0.0.1:8766
}
```

Caddy 会自动申请和续期证书。参考 `systemd/yhr.lol.Caddyfile`，更多说明见 `CONSOLE.md`。

## Telegram

### 命令

| 命令 | 作用 |
| --- | --- |
| `/status` | 模式、执行账户、是否暂停、止损配置、账户权益 |
| `/positions` | 状态 + 执行账户持仓和挂单 |
| `/leader_positions` | 所有交易员的实时仓位（别名 `/leader_positons`，兼容旧拼写） |
| `/pause` | 暂停新开仓/加仓 |
| `/resume` | 恢复新开仓/加仓 |
| `/unlock_hard_stop` | 解除硬止损锁定：按当前权益重置当日基线并恢复新开仓 |

### 通知

- `ORDER FILLED`：成交方向、仓位方向、动作、成交数量、均价。
- `ORDER ACCEPTED`：订单已被交易所接受但尚未成交（例如延迟入场的限价单）。
- `ORDER FAILED`：下单异常及原因。
- `ORDER SKIPPED`：跳过原因，包括暂停、黑名单、symbol skip、所有权冲突、启动价格不利、小单累计、无可减仓位、反手跳过等。
- `HARD STOP TRIGGERED`：账户硬止损触发，包含权益、当日基线、当日亏损和限额；同时提示可用 `/unlock_hard_stop` 解除。
- 启动/停止时分别发送 `SYSTEM STARTED` / `SYSTEM STOPPED`。
- 启动时会先消费掉历史 update，避免重启后重放旧命令。

`report_format: image` 时，`/positions` 和 `/leader_positions` 会渲染成深色 PNG 报告卡片；渲染或发送失败会自动回退为文本消息。

## Web 控制台

默认地址：`http://127.0.0.1:8766/`，公网通过 Caddy 反代。

### 页面

- **Monitor**
  - 账户余额、未实现盈亏、权益、系统状态、止损配置。
  - 暂停/恢复按钮。
  - 当前挂单、执行账户持仓、各交易员实时仓位。
  - 交易员配置表单。
- **Performance**
  - 时间范围：Today / 30D / All / 自定义 `since`–`until`（Unix 秒）。
  - 指标：已实现毛盈亏、手续费、资金费、净盈亏、现金流调整后收益率、最大回撤、成交数。
  - 图表：账户权益曲线、分交易员已实现盈亏。
  - 分交易员表：净盈亏、毛盈亏、手续费、资金费、胜率、成交数、贡献占比。
  - 当前持仓按交易员归因。
- **Configuration**
  - 修改交易员地址、系数、`total_margin`、启用状态、延迟入场、单仓止损。
  - 保存后写回 `config.yaml` 并热更新引擎（`enabled` 除外，见注意事项）。
  - 每次修改写入 `audit_logs`。

### 可编辑字段

通过 `PATCH /api/v1/leaders/{name}` 可以修改：

`address`、`enabled`、`coefficient`、`total_margin`、`symbol_blacklist`、`late_entry_enabled`、`late_entry_offset_pct`、`position_stop_loss`。

校验规则：

- `address` 必须是 42 字符的 `0x` 地址。
- `coefficient >= 0`。
- `total_margin > 0`。
- `late_entry_offset_pct >= 0`。
- `position_stop_loss.max_loss_usdt > 0`。

修改 `address` 时，引擎会清空该交易员的旧仓位基线（`previous`）、停止/成交/小单 carry 状态和未成交挂单，然后按启动流程重新拉取新地址的 REST 快照，并让 WebSocket 重新订阅新地址。旧地址的仓位不会被当成 `close` 误平。

`symbol_blacklist` 只能通过 API 修改，控制台表单里没有输入框。

### API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/dashboard` | 最近一次快照 |
| GET | `/api/v1/trades?limit=100` | 成交记录，最多 500 条 |
| GET | `/api/v1/stats` | 快照 + 绩效汇总 |
| GET | `/api/v1/performance/overview?account=&since=&until=` | 绩效、交易员聚合、持仓归因 |
| GET | `/api/v1/performance/leaders` | 分交易员绩效 |
| GET | `/api/v1/performance/equity` | 权益曲线 |
| GET | `/api/v1/events` | SSE 快照流，3 秒推送，30 秒心跳 |
| PATCH | `/api/v1/leaders/{name}` | 修改交易员配置 |
| POST | `/api/v1/control/{pause\|resume}` | 暂停/恢复 |
| POST | `/command` | 与 Telegram 命令等价的文本接口 |

### 绩效统计口径

- `net_pnl = gross_pnl - fees + funding`。
- `gross_pnl` 是交易所返回的已实现盈亏；`fee` 存正数成本；`funding` 正数表示收入。
- 权益曲线用 `equity - 累计现金流` 做调整，再计算收益率和最大回撤。
- 现金流来自交易所的充值、提现、资金费历史（适配器支持时）。
- 曲线查询会按时间分桶，最多返回 400 个点，避免长时间窗口拉爆内存。

### 交易归因

- 引擎本地成交（`local_fills`）记录每一笔真实下单，包括 reduce-only 标记。
- 交易所平仓记录优先匹配本地 reduce-only 成交：同 symbol、时间差 24 小时内、最近的一条。
- 交易所原生止损/爆仓/手动平仓没有本地 reduce-only 成交，会归因到该 symbol 在平仓时间之前最近的一笔开仓成交（`attribution = stop_close`）。
- 无法匹配的记录保留为 `unattributed`，不会猜测。

### 交易所盈亏数据源

- **Gate**：以合约账户账本（account book）为准，把同一订单的 `pnl` 和 `fee` 腿合并成一条记录。这是唯一能完整覆盖每次平仓（包括止损和反手）的数据源。
- **Binance**：优先 `fetchIncome`，失败时回退到成交记录。
- Gate 的 `position_close`（平仓历史）仅作为回退；一旦账本数据可用，旧的回退记录会被清理。
- 控制台启动时会就地迁移旧库：毫秒时间戳转秒、删除历史 fee-only 脏数据、删除被账本取代的 `position_close` 记录。

## 状态与数据

### `data/state.json`

每次变动原子写入，包含：

| 键 | 内容 |
| --- | --- |
| `symbol_owners` | `执行账户:SYMBOL -> 交易员`，symbol 所有权 |
| `stopped` | `交易员:SYMBOL -> true`，止损后抑制再入场 |
| `entered` | `交易员:SYMBOL -> true`，是否真实成交过开仓 |
| `small_order_carry` | `交易员:SYMBOL -> {side, amount}`，小单累计 |
| `protection` | `执行账户:SYMBOL -> {id, contracts, trigger}`，单仓止损条件单 |
| `risk` | `{baseline, day, locked}`，账户硬止损基线 |

### `data/console.sqlite3`

| 表 | 内容 |
| --- | --- |
| `equity_snapshots` | 每 3 秒的权益/未实现盈亏快照 |
| `trades` | 交易所已实现盈亏记录、归因交易员、来源 |
| `cash_flows` | 充值、提现、资金费 |
| `local_fills` | 引擎本地成交，用于归因和重启恢复 |
| `audit_logs` | 控制台修改/暂停操作的审计记录 |

`data/` 已在 `.gitignore` 中，不会进入版本库。

## 项目结构

```text
main.py                 启动入口、WebSocket 消费、5 秒风控对账
validate.py             配置结构校验
config/
  config.yaml           主配置
  lead.env              密钥（git 忽略）
  loader.py             环境变量/占位符加载
  manager.py            控制台写回 config.yaml
core/
  engine.py             信号执行、仓位管理、小单 carry、止损抑制
  detector.py           仓位差分生成信号
  models.py             Position / Signal / Order 数据结构
  symbols.py            symbol_map 解析与反向显示
exchanges/
  base.py               交易所接口
  ccxt_exchange.py      Gate / Binance 实现
  dry_run.py            内存模拟交易所
risk/manager.py         账户级每日硬止损
sources/
  hyperliquid_ws.py     HL WebSocket 订阅与重连
  position_queries.py   REST 快照、Telegram 持仓报表
state/store.py          state.json 原子读写
telegram/
  bot.py                长轮询命令与通知
  report_image.py       PNG 报告渲染
api/
  app.py                aiohttp 应用与 /command
  console.py            控制台 API、SQLite、绩效与归因
  static/               控制台前端
systemd/                systemd 与 Caddy 配置
tests/                  单元测试
```

## 已知限制与注意事项

- **单执行账户**：一个进程只使用第一个启用交易员的 `execution_account` 创建交易所实例。所有启用的交易员应指向同一个账户。
- **`enabled` 只在启动时生效**：启动时只加载 `enabled: true` 的交易员，控制台也只能看到这些交易员。运行中把某个交易员改成 `enabled: false` 不会停止它继续跟单，需要重启进程；启动时未启用的交易员无法从控制台启用。
- **反手不处理**：`close_reverse` 永远跳过，旧仓保持不动，需要手动平仓后再等待下一次开仓信号。
- **关闭单仓止损不会撤销已存在的条件单**：把 `position_stop_loss.enabled` 改成 `false` 只是不再新建/对账，已经挂在交易所的止损单仍然有效，需要手动撤销或等仓位平掉。
- **暂停状态不持久化**：重启后 `paused` 恢复为 `false`；账户硬止损的 `locked` 会持久化，且 `process()` 会同时检查 `locked`，因此重启后当日锁定仍然有效。
- **控制台保存会重写 YAML**：通过控制台修改交易员参数会重新序列化整个 `config.yaml`，注释和手工排版会丢失；`${ENV}` 占位符会保留。
- **symbol_map 需要重启**：它不在控制台可编辑字段中，修改后必须重启进程。
- **控制台无鉴权**：暴露公网前请自行加 Basic Auth、VPN 或 IP 限制。`lead.env` 里的 `CONSOLE_USERNAME` / `CONSOLE_PASSWORD` 当前代码没有使用。
- **地址修改**：控制台可以改交易员地址。修改后引擎会重置该交易员的仓位基线、撤销旧挂单、立即用新地址拉取 REST 快照，并触发 WebSocket 重新订阅；不需要重启进程。
- **未使用字段**：`hyperliquid.addresses`、`leaders[].source`、`console.domain` 当前不影响运行。
- **启动仓位查询失败会直接退出**：`prepare_startup` 故意让异常向上抛出，避免把未知账户状态当成零仓位处理。
- **账户硬止损会平掉所有仓位**：包括非引擎建立的手动仓位。
- **数据库无自动清理**：权益快照每 3 秒写入一条，长期运行后 SQLite 会持续增长；查询已做降采样，但没有保留策略。

## 测试

```bash
pytest tests/
```

`pyproject.toml` 已配置 `pythonpath = ["."]`，不需要额外设置。测试覆盖：symbol_map 解析、CCXT 合约解析、信号跳过、启动补仓、小单累计、止损后抑制再入场、风控基线、绩效归因与旧库迁移。
