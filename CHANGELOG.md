# Changelog

## v2.7.0 (2026-07-24) — 数据库驱动 + 回测门禁 + 热加载

### 🔑 核心升级

- **数据库成为唯一配置源**：新增 `strategies` 表，所有策略参数从数据库读取，不再依赖 JSON 文件
- **策略配置版本管理**：每次参数变更自动递增 `version`，旧参数快照写入 `param_history` 表
- **修改来源追踪**：每次变更记录 `modifier` 字段（manual / ai_pick / optimization / telegram / rollback）
- **策略启动前回测门禁 (PreliveGate)**：复用 vnpy `BacktestingEngine`，验证通过才允许部署
- **门禁阈值可配置**：`min_total_return` / `min_sharpe_ratio` / `max_drawdown` / `min_trade_count` / `min_win_rate`
- **运行时热加载**：后台线程每 N 秒检测 `updated_at` 变化，只对变更策略验证+重载，其他策略零影响
- **Telegram 事件分级上报**：INFO / WARN / ERROR / TRADE / GATE / STATUS / ROLLBACK / HOTRELOAD
- **Telegram 新增命令**：
  - `/gate` — 查看门禁阈值
  - `/gate_set <key> <val>` — 修改阈值
  - `/history ` — 查看参数变更历史
  - `/rollback  <v>` — 回滚到指定版本
  - `/reload` — 手动触发热加载
  - `/add_strat  <class>` — 添加策略
  - `/remove_strat ` — 移除策略
  - `/version` — 查看当前运行版本

### 📊 数据库新增/增强表

| 表名 | 用途 |
|------|------|
| `strategies` | 策略配置主表（唯一配置源） |
| `param_history` | 参数历史快照（每次变更前自动备份） |
| `prelive_gate_results` | 回测门禁验证结果 |
| `strategy_deploy_log` | 部署/回滚/移除日志 |
| `notification_log` | Telegram 事件通知日志 |

### 🔧 修改的文件

| 文件 | 变更 |
|------|------|
| `main.py` | 重构启动流程：load_config → init_db → AI选股 → **boot(门禁)** → notifier → hot_reload |
| `core/db_manager.py` | 新增 strategies 表 CRUD / 参数版本管理 / 门禁结果 / 部署日志 / 变更检测 |
| `core/engine.py` | 重写为数据库驱动：boot() / check_and_reload_changed() / rollback() / add/remove |
| `monitoring/telegram_notifier.py` | 重写为事件分级 + 节流 + DB 日志 |
| `monitoring/telegram_webhook.py` | 新增 7 个命令处理器 |

### 🆕 新增文件

| 文件 | 用途 |
|------|------|
| `core/prelive_gate.py` | 回测门禁引擎，封装 vnpy BacktestingEngine |

### 📋 启动流程（v2.7.0）

```
main.py
  │
  ├─ 1. load_config()          读取 config/system_config.json
  ├─ 2. CustomDBManager()      初始化数据库 + 建表
  ├─ 3. init_gateways()        启动双引擎 (US/HK)
  ├─ 4. AI 选股                结果写入 ai_stock_pool 表
  ├─ 5. StrategyEngine.boot()   🔑 核心启动流程
  │     ├─ 从 strategies 表读取所有 enabled=1 的策略
  │     ├─ 对每个策略 → PreliveGate 回测验证
  │     ├─ 验证通过 → 注册到 CTA 引擎 → start
  │     └─ 验证失败 → 记录原因 → 跳过（保留旧版本运行）
  ├─ 6. TelegramNotifier      启动事件上报
  ├─ 7. TelegramCommandListener 启动命令监听
  ├─ 8. start_hot_reload()     启动后台热加载线程
  └─ 9. 主循环                定时任务（选股/报告/DualLink）
```

### 🔄 运行时热加载流程

```
数据库 updated_at 变化
  │
  ├─ hot_reload 线程检测到差异
  ├─ 只对变更策略执行：
  │   ├─ PreliveGate 回测验证
  │   ├─ 通过 → 停止旧实例 → 注册新参数 → 启动
  │   └─ 失败 → 保留旧版本继续运行 → Telegram 告警
  └─ 其他策略完全不受影响
```

### ⚠️ 升级注意事项

- `config/strategies.json` 不再作为运行时配置源，策略需迁移到 `strategies` 表
- 首次升级后需手动插入策略配置到数据库（参考 `scripts/seed_strategies.py`）
- Telegram bot token 和 chat_id 仍在 `config/system_config.json` 中配置
- 门禁阈值默认为保守值，可根据实盘经验调整

## v2.6.0 (2026-07-23)

- 升级至 vnpy 4.4.0 核心架构
- 新增12个实盘策略
- 新增AI选股/诊股/参数建议模块
- 新增回测优化器（网格搜索+Walk-forward）
- 双引擎双链路支持US/HK市场
- 策略参数从数据库自动读取
