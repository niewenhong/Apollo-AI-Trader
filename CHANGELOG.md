# Changelog

## v2.6.0 (2026-07-23)
- 升级至 vnpy 4.4.0 核心架构
- 新增12个实盘策略：multi_indicator, dual_thrust, 期权全策略(7个), IPO, CBBC, Warrant
- 新增AI选股模块（技术面+资金面评分，结果存数据库）
- 新增诊股模块（单票深度分析）
- 新增参数建议模块（基于历史回测+AI审核）
- 新增数据库存储（选股结果、诊股结果、参数历史、审核决策、回测结果）
- 新增回测优化器（网格搜索+Walk-forward验证）
- 新增Telegram远程控制命令（/ai_confirm, /optimize, /diagnose, /ipo）
- 策略参数从数据库自动读取，不再硬编码
- 双引擎双链路支持US/HK市场
- 单OpenD连接