## v2.7.0-beta (2026-07-25)

### 新增
- **SubscriptionManager**: 全套订阅（K_1M+K_5M+K_15M+K_60M=4额度/只），市场路由（港股HK链路/美股US链路），延迟60秒反订阅，日线走历史接口，配额审计
- **MultiPeriodDB**: 多周期本地数据库（kline_1m/5m/15m/60m/1d分表），数据缺口检测，原生OHLCV零损耗存储
- **MarketDataBus**: 统一行情总线，自动落库+15m ATR门禁检查+策略分发
- **StrategyMatcher**: 策略-标的匹配器，自动检测市场regime（trend/range），为每个标的匹配最优策略+参数
- **MultiPeriodBacktestEngine**: 多周期回测引擎，100%读本地库，与实盘预热使用相同BarGenerator逻辑
- **MultiPeriodKlineHandler**: 富途原生多周期K线回调（K_1M/K_5M/K_15M/K_60M），转换为vn.py BarData事件分发

### 改进
- 取消分级订阅，统一全套订阅，避免数据缺失和调试混乱
- 主策略周期从1m升到5m/15m（降噪），1m只做精确进场
- 门禁用15m ATR（噪声远低于1m）
- 回测与实盘数据完全同源（本地库）
- 历史数据按需补齐，消耗历史K线额度（7天滚动）

### 注意
- 需在 futu_gateway.py 的 connect 中注册 MultiPeriodKlineHandler
- 首次运行自动创建多周期表
- 300订阅额度可支撑75只标的全套订阅
- 历史K线额度300（7天滚动），同股票多周期只算1额度
