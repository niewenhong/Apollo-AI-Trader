# 用户手册

## 快速开始（5 分钟）

### Step 1：安装
```bash
pip install -r requirements.txt
```

### Step 2：配置富途
1. 下载安装 [富途 OpenD](https://www.futunn.com/download/openAPI)
2. 启动 OpenD，默认端口 11111
3. 编辑 `config/accounts_config.json`，确认 host/port 正确

### Step 3：调试模式运行
```bash
python scripts/run_debug.py
```
这会启动一个带有 VWAP 策略的调试环境，连接富途模拟盘。

### Step 4：观察日志
启动后，在 VeighNa 主窗口的日志区域可以看到：
- `[Engine] 策略初始化`
- `[VWAP] Tick/Bar 数据`
- `[ORDER]` 订单状态
- `[TRADE]` 成交记录

## 策略说明

### VWAP 均值回归
- **逻辑**：价格偏离 VWAP 超过阈值后反向开仓
- **适用**：震荡市、高流动性标的
- **参数**：`threshold_long`（做多阈值）、`threshold_short`（做空阈值）

### 三重过滤短线
- **逻辑**：EMA20/60 看方向 + RSI(6) 找回撤 + MACD 确认
- **适用**：1 分钟 K 线、趋势明确的标的
- **参数**：`ema_fast`、`ema_slow`、`rsi_period`、`rsi_oversold`

### 期货趋势
- **逻辑**：EMA 趋势 + ATR 过滤震荡 + Shelly 仓位
- **适用**：ES/NQ 等流动性好的期货
- **参数**：`ema_fast`、`atr_period`、`shelly_risk_pct`

### 涡轮/牛熊证
- **逻辑**：追踪发行商对冲行为 + 收回价监控
- **适用**：港股蓝筹涡轮（腾讯、美团、阿里）
- **注意**：必须严格监控收回价

### 打新
- **逻辑**：AI 评分筛选新股 → 自动/手动申购
- **适用**：港股/美股 IPO

## 日常操作

### 查看系统状态
通过 Telegram 发送 `/status`，返回：
```
📡 Apollo 运行中
策略数: 3
运行: True
启动: 2026-07-18T09:30:00
```

### 紧急停止
发送 `/stop` 立即停止所有策略。
发送 `/kill_all` 紧急平仓并停止。

### 停止指定机器
发送 `/stop 192.168.1.100` 停止指定 IP 的交易系统。

### 修改策略参数
1. 编辑 `config/strategies/{name}_config.json`
2. 保存文件（系统自动热加载）
3. 无需重启

## 回测流程

```bash
# 1. 准备数据（CSV 格式：datetime,open,high,low,close,volume）
# 2. 运行回测
python scripts/run_backtest.py triple_filter data/NVDA_1m.csv

# 3. 查看结果
# 终端输出 + data/export/backtest_report.html
```

## 参数优化流程

```bash
# 网格搜索
python scripts/run_optimize.py vwap data/NVDA_1m.csv grid

# 贝叶斯优化
python scripts/run_optimize.py triple_filter data/NVDA_1m.csv bayes

# 遗传算法
python scripts/run_optimize.py futures_momentum data/ES_1m.csv genetic
```

最优参数自动写入 `config/strategies/{name}_config.json`。

## 周末维护

设置定时任务（Windows 任务计划程序）：
```bash
# 每周六 09:00 执行
python scripts/run_weekend_maintenance.py
```

自动执行：
1. 回测所有策略
2. AI 参数建议
3. AI 选股
4. 生成周报并推送 Telegram

## 常见问题

**Q: 策略不触发交易？**
- 检查 `config/symbols_config.json` 中标的 `enabled` 是否为 true
- 检查当前是否在交易时段内
- 查看日志中是否有 `[Risk]` 风控拒绝

**Q: 订单发出但没有成交？**
- 检查价格是否合理（是否偏离市场价太远）
- 检查 `price_offset` 参数
- 确认富途 OpenD 连接正常

**Q: AI 功能不工作？**
- 检查 `config/ai_config.json` 中 API Key 是否正确
- 运行 `python -c "from ai.llm_client import LLMClient; print(LLMClient().health_check())"`
- 确认网络连接正常

**Q: Telegram 收不到消息？**
- 确认 Bot Token 和 Chat ID 正确
- 向 Bot 发送任意消息激活
- 检查 `config/alerts_config.json` 中 `enabled` 为 true
