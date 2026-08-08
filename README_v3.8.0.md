# Apollo AI Trader v3.8.0 升级指南

## 🚀 重大新功能

### 1. 多用户架构

```
┌─────────────────────────────────────────────┐
│              UserManager                  │
├─────────────────────────────────────────────┤
│ ADMIN  │ POWER  │ STANDARD │ TRIAL       │
│ 全部权限│ 高级    │ 标准     │ 受限       │
└─────────────────────────────────────────────┘
```

- **系统级策略**（`owner='SYSTEM'`）：所有用户可见可用
- **用户级策略**（`owner=user_id`）：仅创建者可见
- **策略晋升**：优秀用户策略可晋升为系统级，共享给所有人
- **权限分级**：ADMIN > POWER > STANDARD > TRIAL

### 2. 策略生命周期管理

```
PENDING → TRIAL(3%) → FORMAL(10%) → CORE(20%)
                ↓ 不达标
         OPTIMIZE → 重试
                ↓ 多次失败
          DEPRECATED
```

| 层级 | 资金比例 | 晋升条件 |
|------|---------|---------|
| TRIAL | 3% | 初始状态 |
| FORMAL | 10% | score≥75 + 交易≥30笔 + 运行≥14天 |
| CORE | 20% | score≥85 + 运行≥60天 + 交易≥200笔 |
| ADOPT | 5% | 外部持仓接管（自动创建） |
| SYSTEM | 10% | 系统级共享策略 |

### 3. 绩效评估体系

综合评分（0-100）：

| 指标 | 权重 | 目标值 |
|------|------|--------|
| 夏普比率 | 25% | ≥2.0 |
| 最大回撤 | 20% | ≤10% |
| 盈利因子 | 15% | ≥2.0 |
| 索提诺比率 | 15% | ≥2.0 |
| 胜率×盈亏比 | 15% | ≥1.5 |
| 执行质量 | 10% | 填充率≥95% |

### 4. 信号衰减监测

三重检测：
- 滚动夏普 < 基线 × 50% → 警告
- 滚动盈利因子 < 基线 × 60% → 警告
- 当前回撤 > 历史最大 × 1.5 → 严重警告

响应：
- MEDIUM → 记录事件，继续观察
- HIGH → 资金规模减半 + 暂停开新仓

### 5. 外部持仓自动接管

```
系统启动 → 扫描所有网关持仓 → 找出未管理持仓
    → 自动创建 ManagedPositionStrategy → 仅做风控管理
    → 持仓清空后自动退役
```

## 📁 新增文件

| 文件 | 说明 |
|------|------|
| `core/user_manager.py` | 多用户管理 |
| `core/account_manager.py` | 账户资金管理 |
| `core/strategy_lifecycle_manager.py` | 策略生命周期总管 |
| `strategies/equity/managed_position_strategy.py` | 外部持仓接管策略 |
| `data/migrations/v3.8.0.sql` | 数据库迁移脚本 |

## 📝 修改文件

| 文件 | 主要变更 |
|------|---------|
| `strategies/base_strategy.py` | 集成 LifecycleManager + 下单日志 |
| `core/db_manager.py` | 多用户表 + 策略层级字段 + 事件日志 |
| `core/strategy_engine.py` | 注入依赖 + 用户级部署 |
| `core/scheduler_jobs.py` | 新增评估/衰减/接管任务 |
| `core/config_loader.py` | 多用户配置 + 环境变量覆盖 |
| `strategies/strategy_factory.py` | 用户级/系统级策略注册 |
| `strategies/equity/trend_strategy.py` | 修正 on_5m_bar 信号逻辑 |
| `main.py` | 初始化新组件 + 注册定时任务 |
| `config/system_config.json` | 新增 lifecycle/users/scheduler 节 |

## 🔧 升级步骤

### Step 1: 备份数据库

```bash
cp data/apollo.db data/apollo.db.backup.v3.7
```

### Step 2: 执行数据库迁移

```bash
sqlite3 data/apollo.db < data/migrations/v3.8.0.sql
```

### Step 3: 替换代码文件

将以下文件覆盖到对应位置：
- `core/` 目录全部替换
- `strategies/` 目录全部替换
- `main.py` 替换
- `config/system_config.json` 替换（注意保留自定义配置）

### Step 4: 修改 config.json

在 `config.json` 中添加：

```json
{
  "enable_multi_user": true,
  "default_user": "SYSTEM",
  "user_config_path": "config/users"
}
```

### Step 5: 验证

```bash
python -c "from core import __version__; print(__version__)"
# 应输出: 3.8.0

python -c "from strategies import __version__; print(__version__)"
# 应输出: 3.8.0
```

### Step 6: 启动

```bash
python main.py
```

检查日志中是否出现：
```
🚀 Apollo AI Trader v3.8.0 启动中...
✅ 数据库就绪: data/apollo.db
✅ 默认管理员: admin001
✅ FUTU_US 网关就绪
✅ FUTU_HK 网关就绪
✅ AccountManager 就绪
✅ LifecycleManager 就绪
✅ StrategyEngine 就绪
✅ 定时任务已启动
🎯 Apollo AI Trader v3.8.0 运行中...
```

## ⚠️ 注意事项

1. **默认管理员密码**为 `admin123`，首次登录后请立即修改
2. **现有策略**会自动标记为 `owner='SYSTEM'` 和 `tier='SYSTEM'`
3. **资金分配**需要 AccountManager 连接真实网关后才能准确计算
4. **定时任务**需要安装 `apscheduler`：`pip install apscheduler`
5. **降级方案**：如遇到问题，可回退到 v3.7.x 备份

## 🏗️ 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                        main.py                            │
├──────────────────────────────────────────────────────────────┤
│  ┌────────────┐  ┌──────────────┐  ┌────────────────┐   │
│  │UserManager │  │AccountMgr   │  │SchedulerJobs  │   │
│  └─────┬──────┘  └──────┬───────┘  └──────┬─────────┘   │
│        │                 │                   │             │
│  ┌─────┴─────────────────┴──────┐  ┌─────┴──────────┐   │
│  │     StrategyEngine             │  │ LifecycleMgr   │   │
│  │  ┌───────────────────────┐   │  │ (总管)         │   │
│  │  │ BaseStrategy          │   │←─┤ 评估/衰减/晋升  │   │
│  │  │ +Lifecycle检查       │   │  │ 资金分配        │   │
│  │  └───────────────────────┘   │  └────────────────┘   │
│  │  ┌───────────────────────┐   │                    │
│  │  │ ManagedPosition      │   │                    │
│  │  │ (接管策略)           │   │                    │
│  │  └───────────────────────┘   │                    │
│  └───────────────────────────────┘                    │
│           │                                │             │
│  ┌────────┴───────┐  ┌────────────┐  ┌────┴──────────┐ │
│  │RiskManager     │  │OrderManager│  │DBManager     │ │
│  │(微观风控)      │  │(智能路由)  │  │(多用户DB)    │ │
│  └────────────────┘  └────────────┘  └───────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## 📊 资金分配示例

假设账户总资金 = $1,000,000

| 层级 | 资金比例 | 可用资金 | 说明 |
|------|---------|---------|------|
| TRIAL × 5个 | 3% each | $15,000 each | 试用期小资金验证 |
| FORMAL × 3个 | 10% each | $100,000 each | 正式运行 |
| CORE × 1个 | 20% | $200,000 | 核心策略 |
| SYSTEM × 10个 | 10% shared | $100,000 pool | 系统共享池 |
| **合计** | **85%** | **$850,000** | 保留15%现金缓冲 |

## 🔍 故障排查

### 问题：策略不下单

检查顺序：
1. 日志中是否有 `[5m]` → 确认 K 线数据到达
2. 日志中是否有 `[Lifecycle]` → 确认 LifecycleManager 检查
3. 日志中是否有 `[BUY]/[SELL]` → 确认下单尝试
4. 日志中是否有 `[风控拦截]` → 确认风控放行
5. 检查 `tier` 和资金比例 → `SELECT tier, trial_score FROM strategy_config`

### 问题：用户无法登录

```sql
SELECT username, status, role FROM users WHERE username='xxx';
-- 确认 status='ACTIVE'
```

### 问题：外部持仓未接管

检查 `detect_unmanaged_positions_job` 是否运行：
```bash
grep "未管理持仓" logs/main*.log
```

## 📞 支持

- 文档：`docs/`
- 日志：`logs/` 和 `.vntrader/log/`
- 问题反馈：admin@apollo.local
