# Apollo AI Trader v3.2.0 升级说明

## 🔥 修复的三大核心问题

### 1. `ImportError: cannot import name 'load_config'`
**根因**：旧版 `config_loader.py` 只导出了 `ConfigLoader` 类，没有 `load_config` 函数。
**修复**：`core/config_loader.py` 新增模块级便捷函数：
```python
from core.config_loader import load_config
CONFIG = load_config("system")  # → 自动加载 config/system_config.json
```

### 2. 重启后策略被全部删除
**根因**：`main.py` 每次启动删除整个 `history.db` → `strategy_config` 表被清空 → 流水线生成新命名的策略 → 热加载发现引擎中的旧策略不在 DB 中 → 全部当孤儿删除。
**修复**：
- `main.py` **不再删除旧数据库**
- `StrategyEngine._restore_active_runs()` 启动时从 `strategy_runs` 表恢复未结束的运行记录
- 热加载**安全阀**：单次删除比例超过 **50%** 时拒绝执行

### 3. 策略没有运行时段和绩效记录
**新增三张表**：

| 表名 | 用途 |
|------|------|
| `strategy_runs` | 每次运行的完整生命周期（run_id, started_at, ended_at, PnL, 胜率, 回撤, Sharpe） |
| `strategy_daily_pnl` | 每日盈亏曲线 + 高点水位 + 当日回撤 |
| `performance_snapshot` | 定时绩效快照（供历史回溯） |

---

## 📦 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `core/config_loader.py` | **覆盖** | 新增 `load_config()` + `get_config_loader()` + `save_config()` |
| `core/db_manager.py` | **覆盖** | 新增 `strategy_runs` / `strategy_daily_pnl` / `performance_snapshot` 三张表 + 全套 CRUD |
| `core/strategy_engine.py` | **覆盖** | 运行生命周期管理 + 绩效自动采集 + 50% 安全阀 + `shutdown()` 优雅关闭 |
| `core/performance_tracker.py` | **覆盖** | 多策略独立跟踪 + Sharpe/Sortino/Calmar + 后台 60s 采集 + 对接 vnpy `self.trades` |
| `main.py` | **覆盖** | 修复导入 + 不删旧库 + 信号处理 + 绩效采集线程 + 启动/关闭摘要 |

---

## 🚀 部署步骤

```bash
# 1. 备份（★ 不要跳过！保留历史记录）
cp data/history.db data/history.db.backup_$(date +%Y%m%d)

# 2. 解压覆盖
unzip output_v3.2.0.zip -d output_v3.2.0/

# 3. 覆盖文件
cp output_v3.2.0/core/config_loader.py   core/config_loader.py
cp output_v3.2.0/core/db_manager.py      core/db_manager.py
cp output_v3.2.0/core/strategy_engine.py core/strategy_engine.py
cp output_v3.2.0/core/performance_tracker.py core/performance_tracker.py
cp output_v3.2.0/main.py                 main.py

# 4. 启动
python main.py
```

---

## 📊 使用方式

### 查看绩效报告
```python
# 在代码中
engine.print_performance_report()              # 所有策略
engine.print_performance_report("TrendStrategy_00700_none")  # 单个

# 获取字典
report = engine.get_performance_report()
```

### 查询运行历史（直接 SQL 或 DBManager 方法）
```python
db.get_run_history("GridStrategy_XLB_none", limit=20)
db.get_all_runs_summary()
db.get_latest_performance("DualThrustStrategy_02015_none")
```

### 日志关键字速查
| 关键字 | 含义 |
|--------|------|
| `[Perf] 📊` | 策略结算绩效摘要 |
| `[DB] 🏃` | 运行开始（run_id 生成） |
| `[DB] 🔚` | 运行结束 |
| `[HotReload] 🚨` | 安全阀触发（拒绝删除） |
| `shutdown complete` | 优雅关闭完成 |

---

## ⚠️ 注意事项

1. **首次升级不要删除 `history.db`** — 新表会在启动时自动创建，旧数据完整保留
2. 如果旧库结构差异太大导致迁移失败，查看日志中 `[DB] 迁移` 开头的行
3. 热加载间隔从 60s 改为 **300s（5 分钟）**，减少不必要的检查
4. 绩效采集间隔 **60s**，从 vnpy 策略对象的 `self.trades` 自动计算

---

## 🔄 升级后启动流程

```
1. main.py 启动
2. DBManager 连接 history.db（不删除！）
3. 创建新表（strategy_runs 等，如已存在则跳过）
4. 打印历史运行摘要
5. StrategyEngine 初始化
6. ★ _restore_active_runs() 从 DB 恢复上次未结束的运行
7. 合约就绪 → 部署策略
8. PerformanceTracker 启动（60s 采集）
9. 正常运行...
10. Ctrl+C → shutdown() → 结算所有策略 → 打印最终报告
```

---

## 🐛 故障排查

| 问题 | 解决 |
|------|------|
| 仍然报 `load_config` 导入错误 | 确认 `core/__init__.py` 存在且为空文件 |
| 启动后策略全部 FAILED | 查看日志中 `[StrategyEngine]` 行的具体错误 |
| 绩效全为 0 | 确认策略对象有 `self.trades` 字典（vnpy CtaTemplate 内置） |
| 数据库锁定 | 确保只有一个进程访问 `history.db` |
