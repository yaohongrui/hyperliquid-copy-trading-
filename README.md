# HL 跟单机器人

Hyperliquid 交易员跟单系统：通过 HL 官方 WebSocket 监控交易员仓位变化，用官方 `hyperliquid-python-sdk` 在同一交易所（主网，包含全部 HIP-3 perp dex）实时镜像下单，附带 Telegram 管理机器人和 Web 控制台。

> **实盘注意**：`execution_accounts` 里的 API 钱包私钥拥有下单权限，请只通过环境变量 / `lead.env` 提供，不要提交到版本库。程序默认连主网；`testnet: true` 可切到测试网。Web 控制台没有登录鉴权，通过 Caddy 暴露到公网后，任何能打开域名的人都可以暂停引擎、修改交易员参数。

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

- 订阅 HL `allDexsClearinghouseState`，对比前后仓位生成 `open` / `increase` / `decrease` / `close` 信号；反手（`close_reverse`）按“目标仓位 − 账户实际仓位”下成一笔净额反向单。
- 启动和 WebSocket 每次重连后，都会用 REST 快照补齐基线（HIP-3 的各个 perp dex 也会一起拉取），避免断线期间的平仓事件丢失。
- 仓位换算：`目标仓位 = 交易员仓位 × (sizing_balance / total_margin × coefficient)`，`sizing_balance` 默认是账户实时权益，也可以用 `fixed_balance_usdt` 固定。
- 延迟入场：每个交易员可单独开启，开仓信号改为在交易员入场价偏移处挂限价单。
- **symbol 所有权**：同一执行账户下，一个币同时只归一个交易员，避免多个交易员互相打架。
- **小单累计（冰山单）**：下单量低于交易所最小数量/最小名义价值时，先记入 carry，累计够了再一起下；交易员减仓时优先抵消未成交的加仓。
- **启动补仓**：重启时对比交易员现有仓位和账户实际仓位，只在现价不劣于交易员入场价时补齐；不满足条件则跳过，之后交易员再加仓仍会无条件跟随。
- **止损后抑制再入场**：账户被原生止损、爆仓或手动平仓后，只要交易员还持有该币，就在冷却窗口内跳过加仓；冷却结束、交易员平仓或反手后恢复跟单。
- **symbol_map（可选）**：执行和交易员在同一交易所，币名天然一致；`symbol_map` 只保留别名/忽略能力，也可以把某个币标记为完全忽略。

### 风控

- **单仓止损**（`position_stop_loss`）：按亏损金额（USDT）换算出触发价，在交易所挂原生条件单，不依赖程序盯盘。每 5 秒对账，仓位变化或手动平仓后自动重建/撤销，平仓即清理。
- **账户硬止损**（`hard_stop`）：按 UTC 自然日统计，支持 `max_daily_loss_usdt` 和 `max_daily_drawdown_pct`，触发后暂停新开仓并市价全平。
- **暂停/恢复**：Telegram 或 Web 控制台可以暂停新开仓；暂停期间减仓和平仓仍会正常执行。
- **止损后冷却**（`stop_out_cooldown_seconds`）：原生止损或爆仓离场后，在交易员仍持有该币时暂时不再跟随加仓，避免立刻被同一波行情拉回场内。

### 通知与控制

- Telegram 长轮询机器人：`/status`、`/positions`、`/leader_positions`、`/pause`、`/resume`、`/unlock_hard_stop`。
- 成交、挂单被接受、下单失败、信号跳过都会推送通知，并附带原因。
- 持仓报告可渲染成 PNG 图片发送（`report_format: image|text`）。
- 只响应配置的 `chat_id`；重启时会跳过旧消息，不会重放历史命令。
- Web 控制台实时展示账户、挂单、交易员仓位和绩效，并支持暂停/恢复、参数修改、单仓保本。

### 数据与统计

- Web 控制台每 3 秒刷新账户、挂单、交易员仓位，通过 SSE 推送到浏览器。
- Performance 页面统计已实现盈亏、手续费、资金费、出入金调整后收益率、最大回撤、分交易员盈亏和胜率。
- 交易所成交优先按订单号归因到交易员；对不上的记录进入 `unattributed`，不会瞎猜。
- 数据保存在 SQLite；权益快照按 `console.snapshot_retention_days` 自动清理，成交和现金流永久保留。

## 跟单规则细节

### 信号类型

| kind | 含义 | 处理 |
| --- | --- | --- |
| `open` | 交易员从空仓开仓 | 市价；开启延迟入场时改用限价单 |
| `increase` | 同方向加仓 | 市价，并合并未成交的小单 carry |
| `decrease` | 同方向减仓 | reduce-only 市价，数量取 `min(账户持仓, 信号数量)` |
| `close` | 平仓 | reduce-only 市价，平掉账户该币全部持仓 |
| `close_reverse` | 反手 | 按净额差下一笔反向单（市价），并清除旧的止损/冷却状态 |

### 仓位换算

```text
ratio  = sizing_balance / total_margin × coefficient
target = 交易员仓位 × ratio
delta  = (交易员新仓位 - 交易员旧仓位) × ratio
```

- `sizing_balance`：配置了 `fixed_balance_usdt` 就用它，否则用 Hyperliquid 账户权益（各 perp dex 的 `accountValue` 之和）。
- `total_margin` 是交易员的保证金规模，只作为分母，不是你的保证金。必须大于 0。
- `coefficient` 是额外倍率，可以是小数；设为 0 等于不跟。
- 杠杆（`leverage`）只影响保证金占用，不改变跟单的名义仓位；交易所杠杆不够时下单会失败。

例：交易员 `total_margin: 500000`、`coefficient: 40`、你的 `fixed_balance_usdt: 100`，则 `ratio = 0.008`。交易员持有 1 BTC，你跟随 0.008 BTC。

### 下单方式

- `open`：默认市价；若 `late_entry_enabled: true` 且不是启动补仓，则挂限价单，价格为交易员入场价：
  - 买入：`entry × (1 - late_entry_offset_pct%)`
  - 卖出：`entry × (1 + late_entry_offset_pct%)`
  - 限价单按 `leader:symbol` 跟踪，**没有 TTL**：一直挂在交易所，直到成交、交易员减仓/平仓/反手、交易员被禁用，或新的开仓信号替换它。
  - 进程重启后未成交挂单仍会从 `state.json` 恢复跟踪。
  - 对开启了 `mirror_orders` 的交易员，延迟入场会被忽略：订单类型严格跟随交易员（他市价我们市价，他限价我们镜像限价）。
- `increase`：始终市价（延迟入场只作用于 `open`）。
- `decrease` / `close`：reduce-only 市价。
- 市价单用 IOC 限价单实现（以盘口中间价加/减 `slippage`，默认 5%）；每次非 reduce-only 下单前会把该币杠杆设置为配置值（不超过交易所上限，全仓）。
- 下单数量按各币的 `szDecimals` 取整，价格按 5 位有效数字及 `6 - szDecimals` 位小数取整。低于交易所最小名义价值（约 $10）时，非减仓单进入小单 carry，减仓单直接跳过。

### 限价单镜像（`mirror_orders`）

高频交易员大量使用 post-only 限价单时，单纯等仓位变化再市价跟单会付很多 taker 手续费。对显式开启
`mirror_orders: true` 的交易员，额外做以下事情：

- 通过 REST `frontendOpenOrders` 读取该交易员的**普通限价挂单**（`isTrigger=false`、`orderType=Limit`），
  默认每 10 秒刷新一次；空仓的 HIP-3 dex 每 120 秒探测一次，避免打满 HL 的 1200 weight/min 预算。
- 订阅该交易员的 `userFills`，用来区分 maker/taker 成交，并把刚成交的数量从缓存的挂单里扣掉。
- 只开启**一个**镜像交易员时，额外订阅 `orderUpdates`，挂单/撤单/成交能在秒级同步；开启多个时该 channel
  不带用户字段、无法归属，退回 10 秒一次的 REST 刷新。
- **每个交易员最多镜像 2 个单**：离各自标的价格最近的**上方一个 + 下方一个**（严格上下各一个，没有距离阈值）；
  只有一侧有挂单时只挂一个。价格与交易员完全相同，使用 `Alo`（Post-Only）。
- 数量 = 交易员该挂单剩余数量 × 该交易员的 `ratio`。`reduceOnly` 挂单也镜像，但数量会 clamp 到账户实际持仓；
  没有对应持仓或方向不符时跳过。
- 交易员**撤单**：我们也撤。交易员**成交**：我们的单继续挂着，直到成交；如果交易员只成交了一部分就撤掉
  剩余，我们也撤掉剩余。
- 交易员成交后，缓存挂单立即扣减，并重新选出该侧最近的下一单补位。
- 为避免与仓位差分重复下单：maker 开仓成交会记入 credit，`process()` 消费后不再下市价单，但仍会做
  所有权、`entered` 和止损保护等簿记；taker 成交不记 credit，仍走市价路径。平仓的 maker 成交只有在
  我们确实镜像了该单时才抑制市价平仓，否则安全优先、直接市价平掉。
- 暂停/硬止损时撤销非 reduce-only 的镜像单；账户被止损抑制（`stopped`）的币不再镜像加仓。
- `shadow: true` 只打印选中的单，不真实下单，适合上线前观察。

```yaml
mirror_orders:
  enabled: true
  max_orders: 2                 # 每个交易员：上方一个 + 下方一个
  post_only: true
  refresh_seconds: 5            # 镜像单状态轮询
  leader_orders_seconds: 10     # 交易员挂单 REST 刷新
  dex_probe_seconds: 120        # 空 HIP-3 dex 的探测间隔
  failure_cooldown_seconds: 30  # 下单失败后的重试冷却
  credit_grace_ms: 1200         # 成交事件晚于仓位快照时的等待
  credit_ttl_seconds: 900       # 未消费 credit 的过期时间
  shadow: false
```

交易员级别用 `mirror_orders: true` 单独开启；默认关闭。

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
- 抑制不会永久持续，满足以下任一条件就解除：
  - 交易员平掉该币（账户目标归零）；
  - 交易员反手（方向变化）；
  - `stop_out_cooldown_seconds` 冷却到期。到期时如果账户仍空而交易员仍持有，会在同一轮 diff 中按当前目标重新对齐，而不是等待下一次加仓信号。
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

- 用 Hyperliquid 原生触发单平掉整个仓位：`triggerPx` + `isMarket` + `tpsl: sl`，`reduceOnly=true`；触发后按市价成交。
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
- 手动解除：Telegram `/unlock_hard_stop` 或控制台按钮。会以当前权益重置当日基线、清除 `locked` / `flattened` 并恢复新开仓；否则旧基线会在下一次检查时立刻再次触发。
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

### 顶层字段

```yaml
hyperliquid:
  ws_url: wss://api.hyperliquid.xyz/ws
  reconnect_seconds: 3
```

> WebSocket 订阅的地址来自启用的 `leaders[].address`，不需要额外配置。

### mirror_orders

```yaml
mirror_orders:
  enabled: true
  max_orders: 2
  post_only: true
  refresh_seconds: 5
  leader_orders_seconds: 10
  dex_probe_seconds: 120
  failure_cooldown_seconds: 30
  credit_grace_ms: 1200
  credit_ttl_seconds: 900
  shadow: false
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 总开关；仍需交易员级别 `mirror_orders: true` 才会生效 |
| `max_orders` | 每个交易员同时存在的镜像单上限，默认 2（上方一个 + 下方一个） |
| `post_only` | 使用 `Alo`（Add-Liquidity-Only）；穿价会被交易所拒绝 |
| `refresh_seconds` | 镜像单状态与对账循环间隔 |
| `leader_orders_seconds` | 交易员挂单 REST 刷新间隔；调低会成倍增加 HL 请求权重 |
| `dex_probe_seconds` | 没有挂单的 HIP-3 dex 的探测间隔 |
| `failure_cooldown_seconds` | 下单失败（最小量、穿价、保证金不足）后的重试冷却 |
| `credit_grace_ms` | 仓位快照早于 `userFills` 时，最多等这么久再决定是否下市价单 |
| `credit_ttl_seconds` | 未被消费的 maker credit 的过期时间 |
| `shadow` | 只记录选中的镜像单，不真实下单 |

### execution_accounts

```yaml
execution_accounts:
  hl_primary:
    exchange: hyperliquid
    account_address: '0xd38272Da1648a212559a09FB7083097f872A3Dd5'
    api_private_key: ${HL_API_PRIVATE_KEY}
    testnet: false
    leverage: 20
    dry_run: false
    fixed_balance_usdt: 80
    request_timeout_seconds: 10
    hard_stop:
      max_daily_drawdown_pct: 30
```

| 字段 | 说明 |
| --- | --- |
| `exchange` | 固定 `hyperliquid`，其他值会被 `validate.py` 拒绝 |
| `account_address` | 42 位 `0x` 主账户地址；API 钱包只负责签名，仓位和权益属于这里 |
| `api_private_key` | API 钱包私钥（签名用），只放 `lead.env` 或环境变量 |
| `testnet` | `true` 连测试网；默认主网（含全部 HIP-3 perp dex） |
| `leverage` | 下单前设置的目标杠杆，必须为正；不超过该币上限。只影响保证金，不改变仓位大小 |
| `dry_run` | `true` 时使用内存模拟交易所，不连真实账户 |
| `fixed_balance_usdt` | 固定跟单预算，必须为正；不填则使用账户实时权益 |
| `request_timeout_seconds` | Hyperliquid REST 请求超时，默认 10 秒 |
| `hard_stop.max_daily_loss_usdt` | 当日亏损达到该 USDT 数触发硬止损 |
| `hard_stop.max_daily_drawdown_pct` | 当日回撤达到该百分比触发硬止损 |

> **一个进程实际只交易一个执行账户。** `main.py` 用第一个启用交易员的 `execution_account` 创建唯一的交易所实例；所有启用的交易员都应该指向同一个账户。给不同交易员配不同账户不会自动切换下单账户。

`validate.py` 的规则：

- 每个账户必须 `exchange: hyperliquid`。
- `leverage` 必须为正数；`fixed_balance_usdt` 如果配置也必须为正数。
- `hard_stop` 如果配置必须是 mapping。
- `account_address` 如果配置必须是 42 位 `0x` 十六进制地址。
- 非 `dry_run` 账户必须解析出 `api_private_key`（未被展开的 `${...}` 占位符视为缺失），并且必须有合法 `account_address`。

### stop_out_cooldown_seconds

```yaml
stop_out_cooldown_seconds: 600
```

原生止损/爆仓离场后，如果交易员仍持有该币，在这个冷却窗口内跳过 `open` / `increase`。交易员平仓、反手，或冷却到期都会解除；冷却到期且账户仍空时，会立即按交易员当前目标重新对齐。默认 600 秒。

### protection_reconcile_seconds

```yaml
protection_reconcile_seconds: 15
```

仓位/保护单对账与硬止损检查的间隔（秒），默认 5。Hyperliquid 的 HIP-3 账户需要按每个 dex 查询 REST，建议在实盘放在 15 秒左右以留在 API 额度内。

### console

```yaml
console:
  database: data/console.sqlite3
  domain: yhr.lol
  leader_rest_seconds: 60
  dashboard_seconds: 15
  performance_seconds: 180
  performance_lookback_days: 30
  performance_overlap_seconds: 300
  performance_page_size: 2000
  snapshot_retention_days: 90
```

| 字段 | 说明 |
| --- | --- |
| `database` | SQLite 路径，目录会自动创建 |
| `domain` | 仅作文档/部署参考，代码不读取 |
| `leader_rest_seconds` | 交易员 REST 兜底轮询间隔（秒），默认 60，最小 10 |
| `dashboard_seconds` | 账户面板刷新间隔（秒），默认 5，最小 2；HIP-3 多 dex 账户会按每个 dex 查询，勿设得过低 |
| `performance_seconds` | Performance 数据（成交/资金费/现金流）刷新间隔（秒），默认 60，最小 10；示例配置用 180 |
| `performance_lookback_days` | 首次启动回填绩效数据的天数，默认 30 |
| `performance_overlap_seconds` | 增量拉取时向水位之前回看的重叠秒数，默认 300，避免边界漏单 |
| `performance_page_size` | 单次 REST 分页行数，默认 2000（HL 接口上限） |
| `snapshot_retention_days` | 权益快照保留天数，默认 90；成交和现金流不清理 |

### auto_breakeven

```yaml
auto_breakeven:
  enabled: false
```

全局自动保本开关，默认关闭。开启后需要配置 `trigger_usdt`（浮盈达到该金额时把原生止损移到开仓价）。每个交易员可以用自己的 `auto_breakeven` 块覆盖全局设置。

### leaders

```yaml
leaders:
- name: A
  address: '0x15ba...'
  source: hyperliquid
  execution_account: hl_primary
  coefficient: 5
  total_margin: 400000
  enabled: true
  mirror_orders: false
  symbol_blacklist: []
  late_entry_enabled: true
  late_entry_offset_pct: 0.2
  position_stop_loss:
    enabled: false
    max_loss_usdt: 10
```

| 字段 | 说明 |
| --- | --- |
| `name` | 交易员名称，用于通知、日志、所有权和归因，必须唯一 |
| `address` | HL 地址。YAML 未加引号的 `0x...` 被解析成整数时，代码会自动转回十六进制 |
| `source` | 固定为 `hyperliquid` |
| `execution_account` | 指向 `execution_accounts` 中的键；同一进程内应保持一致 |
| `coefficient` | 跟单倍率，非负，可为小数 |
| `total_margin` | 交易员保证金规模（分母），必须大于 0 |
| `enabled` | 是否启用；控制台可以在运行中切换，禁用时会撤掉该交易员的挂单 |
| `symbol_blacklist` | 黑名单，匹配 HL 币名（如 `BTC`）或加 `USDT` 后缀（如 `BTCUSDT`） |
| `late_entry_enabled` | 是否对 `open` 使用延迟限价入场（开启 `mirror_orders` 时无效） |
| `late_entry_offset_pct` | 限价相对交易员入场价的偏移百分比，非负 |
| `mirror_orders` | 是否镜像该交易员的限价挂单（Post-Only、同价、上下各一个），默认 `false` |
| `position_stop_loss.enabled` | 是否启用单仓止损 |
| `position_stop_loss.max_loss_usdt` | 单仓最大亏损金额（USDT），必须大于 0 |

注意：

- 黑名单会跳过该币的**所有**信号，包括减仓和平仓。已有仓位需要手动处理，或者先移出黑名单。
- `symbol_map` 的 skip 同理：所有信号都不执行。
- 所有启用的交易员必须使用同一个 `execution_account`；`validate.py` 会拒绝跨账户启用。

### state

```yaml
state:
  path: data/state.json
```

保存 symbol 所有权、止损抑制状态、已成交标记、小单 carry、单仓止损单 id、账户硬止损基线。每次变动原子写入。

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

> 变量名沿用 `TELEGRAM_GATE_*`，避免迁移服务器上已有的密钥；后续可以统一改名。

`enabled: true` 但 token/chat_id 缺失或仍是未展开的 `${...}` 时，只打印警告，不会启动机器人。

### symbol_map（可选）

执行和交易员在同一交易所，币名已经一致，通常**不需要配置**。保留该字段只用于别名和忽略：

```yaml
symbol_map:
  'XYZ:OLD': skip              # 完全忽略
  'XYZ:DEAD': null             # 同上
```

规则：

- 键是 HL 币名，大小写不敏感；值可以是同名币种或 `skip`。
- 值为 `skip`、`ignore`、`none`、`null`、空字符串或空值都表示忽略该币：所有信号都不执行，carry 会被清空。
- HIP-3 名称包含冒号，YAML 中**必须加引号**，例如 `'XYZ:AMD'`；不加引号会被解析成键值对。
- 不再做跨交易所标的映射；映射到未知币种会在启动时报错。
- 只支持配置文件修改，运行中修改需要重启进程。

### 完整示例

```yaml
hyperliquid:
  ws_url: wss://api.hyperliquid.xyz/ws
  reconnect_seconds: 3

execution_accounts:
  hl_primary:
    exchange: hyperliquid
    account_address: '0x0000000000000000000000000000000000000000'
    api_private_key: ${HL_API_PRIVATE_KEY}
    testnet: false
    leverage: 20
    dry_run: false
    fixed_balance_usdt: 80
    request_timeout_seconds: 10
    hard_stop:
      max_daily_drawdown_pct: 30

stop_out_cooldown_seconds: 600

console:
  database: data/console.sqlite3
  domain: yhr.lol
  leader_rest_seconds: 60
  dashboard_seconds: 15
  performance_seconds: 180
  performance_lookback_days: 30
  performance_overlap_seconds: 300
  performance_page_size: 2000
  snapshot_retention_days: 90

auto_breakeven:
  enabled: false

leaders:
- name: A
  address: '0x0000000000000000000000000000000000000000'
  source: hyperliquid
  execution_account: hl_primary
  coefficient: 5
  total_margin: 400000
  enabled: true
  mirror_orders: false
  symbol_blacklist: []
  late_entry_enabled: true
  late_entry_offset_pct: 0.2
  position_stop_loss:
    enabled: true
    max_loss_usdt: 10

state:
  path: data/state.json

telegram:
  enabled: true
  bot_token: ${TELEGRAM_GATE_BOT_TOKEN}
  chat_id: ${TELEGRAM_GATE_CHAT_ID}
  polling_timeout: 25
  report_format: image
```

## 运行与部署

环境要求：Python 3.11+。

```bash
pip install -r requirements.txt

# 校验配置（执行账户、地址、杠杆、leaders 结构）
python validate.py config/config.yaml

# 前台运行
python main.py --config config/config.yaml
```

- 默认配置路径是 `config/config.yaml`，可以用 `--config` 指定。
- 日志输出到 stdout，级别 INFO；下单结果、跳过原因、启动参数、硬止损都会记录。
- 实盘启动顺序：加载配置 → 创建 Hyperliquid 客户端（发现默认 dex + 所有 HIP-3 dex）→ 校验 `symbol_map` → 拉取账户仓位（失败则退出）→ 处理各交易员 REST 快照 → 启动 WebSocket 和低速率 REST 兜底 → 启动 Web 控制台（`127.0.0.1:8766`）和 5 秒对账任务。
- `dry_run: true` 时使用内存模拟交易所：市价单立即成交、限价单保持挂单、权益取 `fixed_balance_usdt`（未配置则为 0），不会连接真实账户，也不会创建交易所原生止损单。适合验证配置和跟单流程，但不能验证交易所连通性。

### systemd

`systemd/hyper.service` 已按线上 `/home/yhr/hyper` + `.venv` 写好。它会在每次启动前运行 `validate.py`，并设置 `UMask=0077`。安装需要 root：

```bash
sudo cp systemd/hyper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyper
journalctl -u hyper -f
```

线上部署使用 `copy.service`（以及一个设置 `Restart=always` / `OOMScoreAdjust` 的 drop-in）；更新 unit 时不要删除该 drop-in。如果部署在别的路径，修改 `WorkingDirectory` / `ExecStart` / `ExecStartPre` 里的路径，并确保运行用户对 `data/` 有写权限、能读取 `config/config.yaml` 和 `lead.env`。配置文件含私钥时建议 `chmod 600` 并交由服务用户只读。

### HTTPS 反向代理（Caddy）

控制台只监听 `127.0.0.1:8766`。用 Caddy 终止 TLS 并反代：

```caddyfile
yhr.lol {
    handle /api/v1/* {
        reverse_proxy 127.0.0.1:8766
    }
    handle /static/* {
        reverse_proxy 127.0.0.1:8766
    }
    handle /favicon.ico {
        reverse_proxy 127.0.0.1:8766
    }
    handle_path /trade/* {
        reverse_proxy 127.0.0.1:8766
    }
    handle {
        reverse_proxy 127.0.0.1:8766
    }
}
```

参考 `systemd/yhr.lol.Caddyfile`。Caddy 会自动申请和续期证书；改完后 `sudo systemctl reload caddy`。控制台没有登录，公网暴露前请自行加 Basic Auth、VPN 或 IP 限制。更多说明见 `CONSOLE.md`。

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
  - 暂停/恢复按钮、硬止损解锁按钮。
  - 当前挂单、执行账户持仓、各交易员实时仓位。
  - 执行账户每个持仓行提供「一键保本」按钮：把该仓位的原生止损移到开仓价，仅浮盈时可用；再次点击可恢复配置的止损。
  - 交易员配置表单。
- **Performance**
  - 时间范围：Today / 30D / All / 自定义 `since`–`until`（Unix 秒）。
  - 指标：已实现毛盈亏、手续费、资金费、净盈亏、现金流调整后收益率、最大回撤、成交数。
  - 图表：账户权益曲线、分交易员已实现盈亏。
  - 分交易员表：净盈亏、毛盈亏、手续费、资金费、胜率、成交数、贡献占比。
  - 当前持仓按交易员归因。
- **Configuration**
  - 修改交易员地址、系数、`total_margin`、启用状态、延迟入场、单仓止损、自动保本。
  - 保存后写回 `config.yaml` 并热更新引擎。
  - 每次修改写入 `audit_logs`。

禁用的交易员仍会列在 Configuration 页面（并标记为 disabled），可以在不重启的情况下重新启用；禁用期间不产生信号，并会撤掉该交易员的挂单。

### 可编辑字段

通过 `PATCH /api/v1/leaders/{name}` 可以修改：

`address`、`enabled`、`coefficient`、`total_margin`、`symbol_blacklist`、`late_entry_enabled`、`late_entry_offset_pct`、`position_stop_loss`、`auto_breakeven`。

校验规则：

- `address` 必须是 42 字符的 `0x` 地址。
- `coefficient >= 0`。
- `total_margin > 0`。
- `late_entry_offset_pct >= 0`。
- `position_stop_loss.max_loss_usdt > 0`。

修改 `address` 时，引擎会清空该交易员的旧仓位基线（`previous`）、停止/成交/小单 carry 状态和未成交挂单，然后按启动流程重新拉取新地址的 REST 快照，并让 WebSocket 重新订阅新地址。旧地址的仓位不会被当成 `close` 误平。

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
| POST | `/api/v1/positions/breakeven` | 单个仓位一键保本/恢复止损，body：`{"symbol":"BTC","enabled":true}` |
| POST | `/api/v1/control/{pause\|resume}` | 暂停/恢复 |
| POST | `/command` | 与 Telegram 命令等价的文本接口 |

API 密钥、交易所选择和手动下单接口故意不开放。

### 绩效统计口径

- `net_pnl = gross_pnl - fees + funding`。
- `gross_pnl` 是交易所返回的已实现盈亏；`fee` 存正数成本；`funding` 正数表示收入。
- 权益曲线用 `equity - 累计现金流` 做调整，再计算收益率和最大回撤。
- 曲线查询会按时间分桶，最多返回 400 个点，避免长时间窗口拉爆内存。
- 资金费作为独立的 `hl_funding` 行计入 `funding` 和 `net_pnl`，但**不计入成交数和胜率**，避免虚增交易次数。

### 交易归因

- 引擎本地成交（`local_fills`）记录每一笔真实下单，包括 reduce-only 标记和订单号。
- 归因优先级：
  1. `local_fills.order_id == trades.order_id` 精确匹配（`attribution = order_id`）；
  2. 同 symbol、时间接近的本地 reduce-only 成交；
  3. 交易所原生止损/爆仓/手动平仓没有本地成交时，归因到该 symbol 在平仓时间之前最近的一笔开仓成交（`attribution = stop_close`）。
- `source = hl_funding` 的资金费行跳过归因，避免抢走平仓成交。
- 无法匹配的记录保留为 `unattributed`，不会猜测。

### Hyperliquid 盈亏数据源

- **成交（fills）**：`userFillsByTime`（`aggregate_by_time=false`），映射为 `source = hl_fill`，包含 `closedPnl`、`fee`、`oid`、成交时间。只保留永续成交，过滤 spot。
- **资金费（funding）**：`userFundingHistory`，映射为 `source = hl_funding`，金额取 `delta.usdc`，写入 trades 的 `funding` 列以便现金流对账。
- **非资金费账本（ledger）**：`userNonFundingLedgerUpdates`，把 `deposit` / `withdraw` / `send` / `receive` 及内部/金库转账写入 `cash_flows`，金额依次取 `delta.usdc` / `delta.ustd` / `delta.amount`；`withdraw` 保证为负数。
- 分页：HL 接口单次约 2000 行。客户端按时间窗口二分递归拉取，按稳定 id 去重，并用 `console.performance_page_size` 控制页大小。
- 增量：首次启动按 `performance_lookback_days`（默认 30 天）回填；之后从 SQLite 水位向前重叠 `performance_overlap_seconds`（默认 300 秒）拉取，避免边界漏单。
- 权益使用 `equity - unrealized` 作为钱包余额回退，与 Hyperliquid 账户口径一致。

## 状态与数据

### `data/state.json`

每次变动原子写入，包含：

| 键 | 内容 |
| --- | --- |
| `symbol_owners` | `执行账户:SYMBOL -> 交易员`，symbol 所有权 |
| `stopped` | `交易员:SYMBOL -> true`，止损后抑制再入场 |
| `stopped_at` | `交易员:SYMBOL -> 时间戳`，止损冷却起点 |
| `entered` | `交易员:SYMBOL -> true`，是否真实成交过开仓 |
| `small_order_carry` | `交易员:SYMBOL -> {side, amount}`，小单累计 |
| `protection` | `执行账户:SYMBOL -> {id, contracts, trigger}`，单仓止损条件单 |
| `risk` | `{baseline, day, locked}`，账户硬止损基线 |

### `data/console.sqlite3`

| 表 | 内容 |
| --- | --- |
| `equity_snapshots` | 每 3 秒的权益/未实现盈亏快照，按保留期清理 |
| `trades` | 交易所已实现盈亏记录（`hl_fill` / `hl_funding`）、归因交易员、订单号、来源 |
| `cash_flows` | 充值和提现 |
| `local_fills` | 引擎本地成交，用于归因和重启恢复 |
| `audit_logs` | 控制台修改/暂停操作的审计记录 |

`data/` 已在 `.gitignore` 中，不会进入版本库。

## 项目结构

```text
main.py                 启动入口、WebSocket 消费、REST 兜底、5 秒风控对账
validate.py             配置结构校验（HL-only）
config/
  config.yaml           主配置
  lead.env              密钥（git 忽略）
  loader.py             环境变量/占位符加载
  manager.py            控制台写回 config.yaml
core/
  engine.py             信号执行、仓位管理、小单 carry、止损抑制/冷却
  detector.py           仓位差分生成信号
  models.py             Position / Signal / Order 数据结构
  symbols.py            symbol_map 别名/忽略解析
exchanges/
  base.py               交易所接口
  hyperliquid_exchange.py  Hyperliquid 原生执行（含 HIP-3 与 Performance 数据）
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
systemd/
  hyper.service         systemd 服务配置
  yhr.lol.Caddyfile     Caddy 反代示例
tests/                  单元测试
CONSOLE.md              控制台与绩效数据说明
```

## 已知限制与注意事项

- **单执行账户**：一个进程只使用第一个启用交易员的 `execution_account` 创建交易所实例。所有启用的交易员应指向同一个账户。
- **`mirror_orders` 需改 YAML 并重启**：它不在控制台可编辑字段中；改完运行 `validate.py` 再重启。
- **镜像平仓限价可能不成交**：完全复制模式下，交易员用限价减仓/平仓，我们也挂限价；没成交就会继续暴露。
- **关闭单仓止损不会撤销已存在的条件单**：把 `position_stop_loss.enabled` 改成 `false` 只是不再新建/对账，已经挂在交易所的止损单仍然有效，需要手动撤销或等仓位平掉。
- **暂停状态不持久化**：重启后 `paused` 恢复为 `false`；账户硬止损的 `locked` 会持久化，且 `process()` 会同时检查 `locked`，因此重启后当日锁定仍然有效。
- **控制台保存会重写 YAML**：通过控制台修改交易员参数会重新序列化整个 `config.yaml`，注释和手工排版会丢失；`${ENV}` 占位符会保留。
- **symbol_map 需要重启**：它不在控制台可编辑字段中，修改后必须重启进程。
- **控制台无鉴权**：暴露公网前请自行加 Basic Auth、VPN 或 IP 限制。
- **启动仓位查询失败会直接退出**：`prepare_startup` 故意让异常向上抛出，避免把未知账户状态当成零仓位处理。
- **账户硬止损会平掉所有仓位**：包括非引擎建立的手动仓位。
- **首次 Performance 回填可能需要时间**：30 天数据会分页拉取，后台异步执行，不阻塞 3 秒行情刷新。

## 测试

```bash
pytest tests/
```

`pyproject.toml` 已配置 `pythonpath = ["."]`，不需要额外设置。测试覆盖：symbol_map 解析、Hyperliquid 适配器（数量/价格取整、订单解析、HIP-3 多 dex）、Performance 映射与分页、信号跳过、启动补仓、小单累计、止损后抑制再入场、风控基线、控制台绩效归因。
