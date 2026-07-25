# Apollo-AI-Trader v2.7.0

## 核心升级：数据库驱动 + 回测门禁 + 热加载

### 架构图

```
main.py（仅编排）
    │
    ▼
StrategyEngine（协调层）
    ├── CustomDBManager ── 策略配置/参数历史/门禁结果/部署日志
    ├── PreliveGate ──── 回测门禁（vnpy BacktestingEngine）
    ├── TelegramNotifier ─ 事件分级上报 + DB 日志
    ├── TelegramCommandListener ─ 远程命令（含回滚/门禁/热加载）
    └── AISelector/Advisor ─ 选股 + 参数建议
```

### 启动流程

```
load_config → init_db → AI选股(写DB) → boot()
    │
    ▼
boot() 对每个 enabled 策略：
    1. 从 strategies 表读参数
    2. ParamAdvisor 补充 AI 建议参数
    3. PreliveGate 回测验证（BacktestingEngine）
    4. 通过 → 注册到 CTA 引擎 → start
    5. 失败 → 记录原因 → 跳过（保留旧版本）
    │
    ▼
TelegramNotifier 启动 → 发送门禁报告
TelegramCommandListener 启动 → 监听远程命令
HotReload 线程启动 → 每60s检测 updated_at 变化
```

### 数据库表结构

| 表名 | 用途 |
|------|------|
| `strategies` | **策略配置主表（唯一配置源）** |
| `param_history` | 每次变更前的参数快照 + 修改来源 |
| `prelive_gate_results` | 回测门禁验证结果 |
| `strategy_deploy_log` | 部署/回滚/移除日志 |
| `notification_log` | Telegram 事件通知日志 |
| `ai_stock_pool` | AI 选股池 |
| `stock_diagnosis` | 诊股结果 |
| `param_suggestions` | AI/优化器参数建议 |
| `backtest_results` | 回测优化结果 |
| `execution_pool` | 执行池 |

### 修改来源追踪

| modifier 格式 | 含义 |
|------|------|
| `system:seed` | 种子脚本初始化 |
| `manual:admin` | 手动编辑 |
| `telegram:12345` | Telegram 远程命令 |
| `ai:gpt4o` | AI 选股写入 |
| `optimizer:bayes` | 参数优化器写入 |
| `system:rollback` | 系统回滚 |

### Telegram 命令一览

| 命令 | 功能 |
|------|------|
| `/help` | 显示所有命令 |
| `/status` | 系统状态 |
| `/gate` | 查看门禁阈值 |
| `/gate_set <key> <val>` | 修改门禁阈值 |
| `/history ` | 查看参数变更历史 |
| `/rollback  <v>` | 回滚到指定版本 |
| `/reload` | 手动触发热加载 |
| `/add_strat  <class>` | 添加新策略 |
| `/remove_strat ` | 移除策略 |
| `/version` | 查看当前运行版本 |
| `/positions` | 当前持仓 |
| `/balance` | 账户余额 |
| `/strategies` | 策略列表 |
| `/debug_buy  <vol>` | 模拟买入 |
| `/debug_sell  <vol>` | 模拟卖出 |
| `/cancel  |all` | 撤单 |
| `/shutdown` | 远程关机 |

### 快速启动

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 config/system_config.json
#    填入 telegram_token / telegram_chat_id / llm_api_key

# 3. 确认 OpenD 已启动（127.0.0.1:11111）

# 4. 初始化策略到数据库
python scripts/seed_strategies.py

# 5. 启动主程序
python main.py
```

### 门禁阈值说明

在 `config/system_config.json` 的 `prelive_gate.thresholds` 中配置：

| 阈值 | 默认值 | 含义 |
|------|--------|------|
| `min_total_return` | 0.0 | 最低总收益率（>0 即盈利） |
| `min_sharpe_ratio` | 0.0 | 最低夏普比率 |
| `max_drawdown` | 0.30 | 最大允许回撤 30% |
| `min_trade_count` | 5 | 最少交易次数 |
| `min_win_rate` | 0.35 | 最低胜率 |

### 热加载原理

```
后台线程每60秒：
  1. 读取数据库所有 strategies
  2. 对比内存中 {name: updated_at} 快照
  3. 发现变化 → 只对变更策略执行 验证 → 重载
  4. 未变更策略 → 零影响，继续运行
```

### 升级注意事项

- `config/strategies.json` 不再作为运行时配置源
- 首次升级后需运行 `scripts/seed_strategies.py` 初始化数据库
- Telegram token/chat_id 仍在 `config/system_config.json` 中
- 门禁阈值可远程通过 `/gate_set` 动态调整
