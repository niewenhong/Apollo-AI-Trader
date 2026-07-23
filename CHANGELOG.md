# Apollo-AI-Trader Changelog

## v2.6.0 (2026-07-23)

### Added
- 12 实盘策略：MultiIndicator, DualThrust, SellPut, SellCall, CoveredCall, CashSecuredPut, BullCallSpread, BearPutSpread, IronCondor, IPO, CBBC, Warrant
- 3 实验策略：Trend, Grid, Straddle
- AI 选股引擎 (`ai/stock_selector.py`)：富途行情评分 → 写入数据库
- AI 诊股引擎 (`ai/stock_diagnosis.py`)：技术面/资金面/趋势综合诊断
- AI 参数建议器 (`ai/param_advisor.py`)：基于诊股结果推荐最优参数
- AI 报告生成器 (`ai/report_generator.py`)：每日/每周交易报告
- LLM 客户端 (`ai/llm_client.py`)：AI 审核 + 参数决策
- 数据库管理器 (`core/db_manager.py`)：6 张自定义表（ai_candidates, param_history, review_decisions, param_audit_log, market_regime_snapshots, telegram_commands）
- 回测优化器 (`backtest/optimizer.py`)：网格搜索 + Walk-forward 验证 + AI 审核
- 决策引擎 (`core/decision_engine.py`)：自动审核 + 参数热更新
- Telegram 远程控制：/optimize, /diagnose, /ai_confirm, /ipo 命令
- Tick 数据支持 VWAP 策略（双模式：1m K 线近似 / Tick 精确）
- 结构化产品策略实盘：CBBC（牛熊证）+ Warrant（窝轮）

### Changed
- 策略从数据库自动读取 AI 建议参数（不再硬编码）
- main.py 启动时自动执行 AI 选股 → 写数据库 → 策略自动注册
- 双引擎双网关架构保持不变（单 OpenD）
- 配置分离：config.json（系统）+ strategy_config.json（策略）

### Fixed
- 修复 vnpy 4.4.0 ZoneInfo 时区崩溃问题
- 修复模拟盘 on_trade 不触发导致 pos 不同步问题
- 修复 debug_cancel 三阶段兜底（缓存→main_eng→富途 API）

### Security
- 数据库文件加密存储（SQLCipher 兼容）
- Telegram 命令白名单
- 富途解锁密码不写入日志
