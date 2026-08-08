# Changelog - Apollo AI Trader v3.8.0

## [3.8.0] - 2026-08-07

### 🚀 重大新功能

#### 多用户架构
- 新增 `UserManager` 模块，支持多用户隔离
- 每个用户拥有独立的策略池、持仓、资金配置
- 系统级策略可共享给所有用户使用
- 用户级策略仅属于创建者

#### 策略分级与生命周期管理
- 新增 `StrategyLifecycleManager`（策略生命周期总管）
- 策略层级：`PENDING` → `TRIAL`(3%) → `FORMAL`(10%) → `CORE`(20%)
- 自动绩效评估与层级升降
- 信号衰减检测与自动响应
- 资金分配按层级动态缩放

#### 外部持仓自动接管
- 自动检测非本系统买入的持仓
- 生成 `ManagedPositionStrategy` 接管管理
- 持仓盈亏不污染策略绩效评分

### 🔧 核心改进

#### BaseStrategy 增强
- `buy/sell/short/cover` 增加下单日志
- 集成 `LifecycleManager` 下单前检查链
- 支持 `lifecycle_manager` 外部注入

#### DBManager 扩展
- 新增多用户表结构
- 策略配置表增加 `tier`、`trial_score`、`optimize_count` 等字段
- 新增 `user_id` 字段区分用户级/系统级策略
- 新增事件日志表

#### SchedulerJobs 增强
- 新增 `evaluate_strategies_job`（每小时评估）
- 新增 `detect_decay_job`（每日衰减检测）
- 新增 `detect_unmanaged_positions_job`（每30分钟）

### 📊 绩效评估体系
- 综合评分公式：夏普(25%) + 回撤(20%) + 盈利因子(15%) + 索提诺(15%) + 胜率×盈亏比(15%) + 执行质量(10%)
- 试用期转正门槛：score≥75 且交易≥30笔 且运行≥14天
- 衰减检测：滚动夏普/盈利因子/回撤三重监测

### 🛡️ 风控整合
- `LifecycleManager` 统一协调风控、资金、层级、衰减检查
- 下单前检查链：交易许可 → 风控 → 资金 → 层级 → 衰减
- 严重衰减自动降规模50%

### 📁 新增文件
- `core/strategy_lifecycle_manager.py` - 策略生命周期总管
- `core/user_manager.py` - 多用户管理
- `core/account_manager.py` - 账户资金管理
- `strategies/equity/managed_position_strategy.py` - 外部持仓接管策略
- `data/migrations/v3.8.0.sql` - 数据库迁移脚本

### 📝 修改文件
- `strategies/base_strategy.py` - 增加下单日志 + LifecycleManager 集成
- `core/db_manager.py` - 多用户 + 策略层级字段
- `core/scheduler_jobs.py` - 新增评估/衰减/接管任务
- `core/strategy_engine.py` - 注入 LifecycleManager
- `main.py` - 初始化新组件
- `config/system_config.json` - 新增多用户/生命周期配置节
