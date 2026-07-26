# Apollo AI Trader v2.8.0

多用户 SaaS 量化交易系统 — Regime 自适应 + 概率加权策略路由 + 模拟/实盘一键切换。

## 核心特性

- **Regime Trainer**：多周期（日线+60分钟+15分钟）特征提取 + 规则概率 + GMM 集成 + Walk-Forward 验证
- **Strategy Matcher**：基于 regime 概率分布的策略权重分配（Softmax 归一化）
- **Param Optimizer**：离线贝叶斯参数优化（Optuna），按股票×策略×regime 组合
- **Trade Mode Manager**：模拟/实盘一键切换，强制风险提示确认，热加载支持
- **Telegram Bot**：独立 Bot Token 架构，命令交互（/status /position /switch /set_risk /pause /resume）
- **Risk Profiles**：保守/稳健/进取/激进 四级模板
- **Docker 容器化**：K8s 就绪，每个用户独立 Pod
- **共享数据库**：SQLite/PostgreSQL，用户隔离 + 全局共享

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 初始化数据库
python -c "from core.data_fetcher import init_database; init_database('data/trading.db')"

# 启动
python main.py
```

## 目录结构

```
Apollo-AI-Trader-v2.8.0/
├── main.py                  # 主入口
├── requirements.txt         # 依赖清单
├── Dockerfile               # 容器镜像
├── start.sh                 # 容器启动脚本
├── config/
│   └── system_config.json   # 系统配置
├── core/
│   ├── __init__.py          # 模块导出
│   ├── data_fetcher.py      # 富途数据拉取 + 数据库 Schema
│   ├── regime_trainer.py    # Regime 概率分布 + Walk-Forward
│   ├── strategy_matcher.py  # 策略权重路由
│   ├── param_optimizer.py   # 贝叶斯参数优化
│   ├── scheduler_jobs.py    # 定时任务调度
│   ├── trade_mode_manager.py# 模拟/实盘切换
│   ├── telegram_bot.py      # Telegram 交互
│   └── risk_profiles.py     # 风险等级模板
├── strategies/              # 策略库（已有）
│   ├── equity/
│   ├── structured_products/
│   └── options/
├── logs/                    # 日志目录
├── data/                    # 数据目录
└── README_UPGRADE.md        # 升级说明
```

## 升级说明

详见 `README_UPGRADE.md`。
