# Apollo-AI-Trader v2.3.0 升级包

## 本次变更
- 合并 `OrderFlowStrategy` + `TripleFilterScalp` → **`ApolloMultiIndicatorStrategy`**
- 新增 6 大指标：KDJ / VWAP / Keltner / Bollinger / ATR / Volume RVOL
- 引入**共振评分机制**（满分 11，门槛 6 才开仓）
- 多空双向完整支持
- 零外部依赖（纯 NumPy）
- 全部参数支持热加载（改 JSON 无需重启）

## 文件清单
| 文件 | 用途 |
|------|------|
| `apollo_multi_indicator_strategy.py` | 核心策略（替换原 OrderFlow + TripleFilter） |
| `main.py` | 启动入口（注册新策略类 + 热加载线程） |
| `apollo_multi_indicator_config.json` | 全部可调参数（热加载） |
| `README_v2.3.0.md` | 本文件 |

## 覆盖方法
1. 将 `apollo_multi_indicator_strategy.py` → 覆盖到 `strategies/equity/`
2. 将 `main.py` → 覆盖到项目根目录（替换原 `main.py`）
3. 将 `apollo_multi_indicator_config.json` → 放到 `config/strategies/`
4. 删除 `strategies/equity/order_flow_strategy.py`（已被合并）
5. 删除 `strategies/equity/triple_filter_scalp_strategy.py`（已被合并）
6. 删除 `__pycache__`，重启

## 策略名称含义
- **Apollo** → 项目品牌
- **MultiIndicator** → 多指标共振（EMA+MACD+RSI+KDJ+VWAP+Keltner+Bollinger+ATR+Volume+盘口）
- **Strategy** → 统一后缀

## 共振评分（满分 11，门槛 6）
| 指标 | 多头加分 | 空头加分 |
|------|----------|----------|
| EMA 排列 | +2 | +2 |
| Keltner 位置 | +1 | +1 |
| MACD 动能 | +2 | +2 |
| RSI 极值 | +2 | +2 |
| KDJ 极值 | +1 | +1 |
| RVOL 放量 | +1 | +1 |
| 盘口失衡 | +2 | +2 |
| VWAP 偏离 | +1 | +1 |

## 可调参数（全部热加载）
详见 `apollo_multi_indicator_config.json`，改完保存即生效。
