# Apollo AI Trader v2.7.0 升级说明

## 升级方式
覆盖合并到现有 `D:\Apollo-AI-Trader\` 目录，保留你的 `config/` 和 `data/` 目录。

## 新增文件（6个）
```
core/subscription_manager.py          ← 全套订阅管理器
core/multi_period_db.py              ← 多周期本地数据库
core/market_data_bus.py              ← 统一行情总线
core/strategy_matcher.py             ← 策略-标的匹配器
backtest/multi_period_engine.py       ← 多周期回测引擎
vnpy_futu/vnpy_futu/multi_period_kline_handler.py  ← 多周期K线回调
```

## 覆盖文件（1个）
```
main.py                              ← 集成全部新模块
```

## 手动补丁（必须做）
在 `vnpy_futu/vnpy_futu/futu_gateway.py` 的 `connect()` 方法末尾添加：
```python
from vnpy_futu.multi_period_kline_handler import MultiPeriodKlineHandler
self.kline_handler = MultiPeriodKlineHandler(self)
self.quote_ctx.set_handler(self.kline_handler)
```

## 验证清单
- [ ] 启动日志: ✅ 全套订阅: US.AAPL (K_1M+5M+15M+60M)
- [ ] 启动日志: ✅ 全套订阅: HK.00700 (K_1M+5M+15M+60M)
- [ ] 启动日志: 📊 额度: 已用X/300 剩余Y
- [ ] BAR回调: [BAR] US.AAPL MINUTE 1 O=...
- [ ] 日线: 📈 日线 US.AAPL: XX条
- [ ] 门禁: [GATE] US.AAPL 15m OK
- [ ] 数据库: data/history.db 含 kline_1m/5m/15m/60m/1d 表
- [ ] Telegram: /status 正常响应
- [ ] 配额审计: 每5分钟输出一次

## 注意事项
1. 反订阅有60秒延迟（富途硬约束），代码已自动处理
2. 美股订阅自动带 session=ALL（盘前盘后）
3. 日线走历史接口，不占订阅额度
4. 回测100%读本地库，0额度消耗
5. 历史K线额度7天滚动释放，大胆使用

## 设计原则
- 要订就订全套（K_1M+K_5M+K_15M+K_60M）
- 日线走历史，不占实时额度
- 港股走HK链路，美股走US链路
- 本地库是唯一可信数据源
- 回测与实盘同源（都从本地库读取）
- 出问题第一个怀疑策略，不怀疑数据
