# Changelog v2.2.0 → v2.3.0

## Added
- `apollo_multi_indicator_strategy.py` — 多指标共振融合策略
  - EMA20/60 趋势判定
  - MACD(12,26,9) 动能确认
  - RSI(6) 极值检测
  - KDJ(9,3,3) 短线极值（新增）
  - VWAP 日内公平价（新增）
  - Keltner(20,2ATR) 趋势包络（新增）
  - Bollinger(20,2σ) 挤压检测（新增）
  - ATR(14) 波动率自适应（新增）
  - Volume RVOL 量能确认（新增）
  - OrderFlow 5档盘口加权失衡
- 共振评分机制（满分 11，门槛 6 才开仓）
- 多空双向完整支持
- 空头移动止盈/硬止损
- `apollo_multi_indicator_config.json` 全参数热加载配置
- `README_v2.3.0.md` 升级说明

## Changed
- `main.py` 重写：注册 `ApolloMultiIndicatorStrategy`，热加载线程覆盖全部新参数
- 策略实例命名规则：`Apollo_MI_{clean_code}`

## Removed
- `order_flow_strategy.py`（已合并入 ApolloMultiIndicatorStrategy）
- `triple_filter_scalp_strategy.py`（已合并入 ApolloMultiIndicatorStrategy）

## Fixed
- KDJ 用严格滚动窗口（不再用 `maximum.accumulate` 近似）
- 空头路径：移动止盈、ATR止损、反向信号止盈全部补齐
- VWAP 每日重置逻辑预留接口
- on_stop 胜率计算除零保护

## Technical Notes
- 零外部依赖（纯 NumPy，不依赖 TA-Lib）
- 所有指标在 on_bar 中用环形缓冲增量计算
- on_tick 仅做轻量决策（盘口失衡 + 持仓管理 + 开仓），无阻塞
- 参数热加载：改 config.json → 守护线程 5s 检测 → 强穿到所有策略实例
