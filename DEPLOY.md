# Apollo AI Trader v3.8.0 部署指南

## 系统要求

- **Python**: 3.11+ (推荐 3.13)
- **操作系统**: Windows 10/11, Linux, macOS
- **内存**: ≥ 4GB
- **磁盘**: ≥ 2GB（含数据和日志）
- **FUTU OpenD**: 已安装并运行

## 快速部署

### 1. 克隆/解压

```bash
# 如果使用 git
git clone https://github.com/your-org/Apollo-AI-Trader.git
cd Apollo-AI-Trader
git checkout v3.8.0

# 或直接解压 zip 包
unzip Apollo-AI-Trader-v3.8.0.zip
cd Apollo-AI-Trader-v3.8.0
```

### 2. 创建虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置 FUTU 连接

编辑 `config/system_config.json`：

```json
{
  "futu_us": {
    "host": "127.0.0.1",
    "port": 11111,
    "env": "SIMULATE"
  },
  "futu_hk": {
    "host": "127.0.0.1",
    "port": 11111,
    "env": "SIMULATE"
  }
}
```

### 5. 初始化数据库

```bash
# 执行迁移脚本
sqlite3 data/apollo.db < data/migrations/v3.8.0.sql
```

### 6. 运行升级验证

```bash
python test_upgrade.py
```

预期输出：
```
============================================================
🧪 Apollo AI Trader v3.8.0 升级验证
   时间: 2026-08-07 15:00:00
============================================================

📦 测试1: 模块导入
------------------------------------------------------------
✅ core 包版本 — v3.8.0
✅ strategies 包版本 — v3.8.0
✅ DBManager 导入
✅ UserManager 导入
✅ AccountManager 导入
✅ StrategyLifecycleManager 导入
✅ StrategyEngine 导入
✅ SchedulerJobs 导入
✅ BaseStrategy 导入
✅ StrategyFactory 导入
✅ ManagedPositionStrategy 导入
✅ TrendStrategy 导入

🗄️ 测试2: 数据库结构
------------------------------------------------------------
✅ 数据库存在 — data/apollo.db
✅ 表 users (✓)
✅ 表 strategy_config (✓)
✅ 表 strategy_shares (✓)
✅ 表 strategy_events (✓)
✅ 表 positions (✓)
✅ 表 strategy_performance (✓)
✅ 字段 strategy_config.owner
✅ 字段 strategy_config.user_id
✅ 字段 strategy_config.tier
✅ 字段 strategy_config.trial_score
✅ 字段 users.username
✅ 字段 users.role

👥 测试3: 多用户管理
------------------------------------------------------------
✅ 创建标准用户
✅ 创建高级用户
✅ 重复用户名拒绝
✅ 用户认证成功 — uid=...
✅ 错误密码拒绝
✅ 标准用户可创建策略
✅ 标准用户不可晋升策略
✅ 管理员可管理用户

🔄 测试4: 策略生命周期
------------------------------------------------------------
✅ 空策略返回中性分 — score=50.0
✅ 层级 TRIAL 资金比例 — 3%
✅ 层级 FORMAL 资金比例 — 10%
✅ 层级 CORE 资金比例 — 20%
✅ 层级 ADOPT 资金比例 — 5%
✅ 层级 DEPRECATED 资金比例 — 0%
✅ 日期差计算 — N天前
✅ 缓存清理

⚙️ 测试5: 配置加载
------------------------------------------------------------
✅ 加载 system_config.json
✅ 配置版本 3.8.0 — v3.8.0
✅ lifecycle 配置存在
✅ promotion_score 存在
✅ min_trial_trades 存在
✅ users 配置存在
✅ scheduler 配置存在
✅ evaluate_interval_minutes 存在
✅ 嵌套配置获取

🔄 测试6: 外部持仓接管
------------------------------------------------------------
✅ ManagedPositionStrategy 继承 BaseStrategy
✅ ManagedPositionStrategy.on_init
✅ ManagedPositionStrategy.on_start
✅ ManagedPositionStrategy.on_1m_bar
✅ ManagedPositionStrategy.on_trade
✅ ManagedPositionStrategy.buy
✅ ManagedPositionStrategy.short
✅ is_adopt 在 parameters 中

📋 测试7: BaseStrategy 增强
------------------------------------------------------------
✅ 参数 user_id
✅ 参数 is_adopt
✅ 方法 buy
✅ 方法 sell
✅ 方法 short
✅ 方法 cover
✅ calc_position_size 存在

============================================================
📊 汇总: 35/35 通过, 0 失败

🎉 所有测试通过！系统已就绪，可以启动。
   启动命令: python main.py
```

### 7. 启动系统

```bash
python main.py
```

## Docker 部署

```bash
# 构建镜像
docker build -t apollo-trader:v3.8.0 .

# 运行容器
docker run -d \
  --name apollo-trader \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  apollo-trader:v3.8.0
```

## 从 v3.7.x 升级

### 自动升级

```bash
# 1. 备份
cp -r data/ data.backup.v3.7/
cp config/system_config.json config.backup.v3.7.json

# 2. 替换代码文件
# （解压新版本覆盖）

# 3. 执行迁移
sqlite3 data/apollo.db < data/migrations/v3.8.0.sql

# 4. 验证
python test_upgrade.py

# 5. 启动
python main.py
```

### 回退方案

```bash
# 停止新版本
# 恢复备份
rm -rf data/
cp -r data.backup.v3.7/ data/
cp config.backup.v3.7.json config/system_config.json

# 使用旧版本代码启动
```

## 多用户配置

### 创建用户

```python
from core import UserManager, UserRole
from core.db_manager import DBManager

db = DBManager("data/apollo.db")
um = UserManager(db)

# 创建不同角色用户
um.create_user("trader1", "secure_pass_123", UserRole.STANDARD)
um.create_user("trader2", "secure_pass_456", UserRole.POWER)
um.create_user("admin2", "admin_pass_789", UserRole.ADMIN)
```

### 用户登录

```python
ok, uid = um.authenticate("trader1", "secure_pass_123")
if ok:
    print(f"登录成功: {uid}")
```

### 晋升用户策略为系统级

```python
from core import StrategyLifecycleManager
slm = StrategyLifecycleManager(db)

ok, msg = slm.promote_user_strategy_to_system(
    "TrendStrategy_AMD_SMART",
    promoted_by="admin001"
)
print(f"晋升: {msg}")
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|---------|
| `APOLLO_DB_PATH` | 数据库路径 | `data/apollo.db` |
| `APOLLO_LOG_LEVEL` | 日志级别 | `INFO` |
| `APOLLO_ENABLE_US` | 启用美股 | `true` |
| `APOLLO_ENABLE_HK` | 启用港股 | `true` |
| `APOLLO_FUTU_HOST` | FUTU 地址 | `127.0.0.1` |
| `APOLLO_FUTU_PORT` | FUTU 端口 | `11111` |

## 验证清单

启动后检查：

- [ ] 日志显示 `🚀 Apollo AI Trader v3.8.0 启动中...`
- [ ] 数据库就绪
- [ ] 默认管理员创建成功
- [ ] FUTU_US 网关就绪
- [ ] FUTU_HK 网关就绪
- [ ] AccountManager 就绪
- [ ] RiskManager 就绪
- [ ] LifecycleManager 就绪
- [ ] StrategyEngine 就绪
- [ ] 定时任务已启动
- [ ] 策略加载完成
- [ ] 资金同步成功

## 常见问题

### Q: FUTU 连接失败
A: 确认 FUTU OpenD 已启动，端口与配置一致。

### Q: 策略不下单
A: 检查顺序：
1. 日志有 `[5m]` → K线正常
2. 日志有 `[Lifecycle]` → 检查拒绝原因
3. 日志有 `[BUY]/[SELL]` → 下单已尝试
4. 日志有风控拦截 → 调整风控参数

### Q: 忘记管理员密码
```sql
-- 重置为 admin123
UPDATE users SET password_hash='240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9' WHERE username='admin';
```

## 支持

- 文档: `docs/`
- 问题反馈: admin@apollo.local
- 日志位置: `logs/` 和 `.vntrader/log/`
