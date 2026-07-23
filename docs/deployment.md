# 部署指南

## 环境要求

- Python >= 3.10
- Windows 10/11（vnpy 主要支持 Windows）
- 富途 OpenD（模拟盘/真实盘）
- Interactive Brokers TWS 或 IB Gateway（可选）
- PostgreSQL（可选，默认 SQLite）

## 安装步骤

```bash
# 1. 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 vnpy（如未包含）
pip install vnpy

# 4. 安装 TA-Lib（Windows 需先下载 wheel）
# 下载地址: https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib
pip install TA_Lib‑0.4.28‑cp312‑cp312‑win_amd64.whl

# 5. 初始化数据库
python -c "from core.db_manager import DBManager; DBManager().init_schema()"
```

## 配置步骤

### 1. 富途账户
编辑 `config/accounts_config.json`：
```json
{
  "futu": {
    "host": "127.0.0.1",
    "port": 11111,
    "market": "SIMULATE",
    "accounts": [{"name": "futu_sim_1", "type": "simulate", "enabled": true}]
  }
}
```

### 2. Telegram Bot
1. 在 Telegram 中搜索 @BotFather，创建 Bot，获取 Token
2. 获取 Chat ID（搜索 @userinfobot）
3. 编辑 `config/alerts_config.json`：
```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "bot_token": "123456:ABC-DEF...",
      "chat_id": "123456789"
    }
  }
}
```

### 3. AI 模型
编辑 `config/ai_config.json`：
```json
{
  "primary": "zhipu",
  "fallback": "deepseek",
  "models": {
    "zhipu": {"api_key": "你的智谱APIKey", "model": "glm-4-plus"},
    "deepseek": {"api_key": "你的DeepSeekKey", "model": "deepseek-chat"}
  }
}
```

## 启动方式

| 模式 | 命令 | 用途 |
|------|------|------|
| 调试 | `python scripts/run_debug.py` | 单策略、详细日志 |
| 回测 | `python scripts/run_backtest.py vwap data/cache/NVDA.csv` | 历史回测 |
| 优化 | `python scripts/run_optimize.py vwap data/cache/NVDA.csv grid` | 参数搜索 |
| 实盘 | `python scripts/run_live.py` | 无人值守 |
| 维护 | `python scripts/run_weekend_maintenance.py` | 周末自动化 |

## 多机部署

```
Machine A (主交易机)
├── ApolloEngine (live mode)
├── FutuGateway → 富途 OpenD
├── TelegramNotifier
└── OpenClawClient

Machine B (回测/AI 机)
├── BacktestEngine
├── ParamOptimizer
├── LLMClient → AI 选股/诊股
└── 推送结果到 Machine A
```

通过 `config/accounts_config.json` 中的 `machines` 字段区分机器角色。
远程控制使用 `/stop <IP>` 关停指定机器。

## 安全建议

1. **API Key 不要提交到 Git**：使用 `.env` 文件 + `python-dotenv`
2. **Telegram Bot Token 保密**：只在配置文件存储
3. **远程控制 IP 白名单**：在 `config/alerts_config.json` 中设置
4. **每日亏损熔断**：在 `config/risk_config.json` 中设置合理阈值
5. **模拟盘先行**：至少运行 2 周模拟盘验证后再上真实资金
