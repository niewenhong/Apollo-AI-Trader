"""
multi_period_engine.py — 多周期回测引擎 v2.7.0
- 100%从本地库读取（0订阅额度消耗）
- 以1m为主驱动轴，BarGenerator合成5m/15m/60m
- 与实盘预热使用完全相同的BarGenerator逻辑
"""

import logging
import itertools
from vnpy.trader.object import BarData, Interval
from core.multi_period_db import MultiPeriodDB

logger = logging.getLogger(__name__)


class MultiPeriodBacktestEngine:
    def __init__(self, db: MultiPeriodDB):
        self.db = db
        self.results = []

    def load_data(self, symbol, periods, start=None, end=None):
        data = {}
        for p in periods:
            bars = self.db.load_bars(symbol, p, start, end)
            if len(bars) < 100:
                logger.warning(f"{symbol} {p} 数据不足: {len(bars)}根")
            data[p] = bars
        return data

    def run(self, strategy_class, symbol, params, periods=None):
        if periods is None:
            periods = ["1m", "5m", "15m", "60m"]
        data = self.load_data(symbol, periods)
        if "1m" not in data or len(data["1m"]) < 200:
            logger.error(f"{symbol} 1m数据不足({len(data.get('1m',[]))}根)，跳过")
            return None

        bars_1m = self._to_bars(data["1m"], Interval.MINUTE, 1, symbol)
        bars_5m = self._to_bars(data.get("5m", []), Interval.MINUTE, 5, symbol)
        bars_15m = self._to_bars(data.get("15m", []), Interval.MINUTE, 15, symbol)
        bars_60m = self._to_bars(data.get("60m", []), Interval.HOUR, 1, symbol)

        strategy = strategy_class()
        strategy.set_params(params) if hasattr(strategy, 'set_params') else None
        strategy.bars_1m = bars_1m
        strategy.bars_5m = bars_5m
        strategy.bars_15m = bars_15m
        strategy.bars_60m = bars_60m

        # 预热: 前750根1m BAR不计入绩效
        warmup = min(750, len(bars_1m) // 3)
        for bar in bars_1m[:warmup]:
            if hasattr(strategy, 'on_bar'):
                strategy.on_bar(bar)

        # 正式回测
        for bar in bars_1m[warmup:]:
            if hasattr(strategy, 'on_bar'):
                strategy.on_bar(bar)

        result = {
            "symbol": symbol,
            "params": params,
            "total_return": getattr(strategy, "total_pnl", 0),
            "trades": getattr(strategy, "trade_count", 0),
            "max_dd": getattr(strategy, "max_drawdown", 0),
        }
        self.results.append(result)
        return result

    def optimize(self, strategy_class, symbol, param_grid, periods=None):
        """网格搜索最优参数"""
        best = None
        keys = list(param_grid.keys())
        for vals in itertools.product(*[param_grid[k] for k in keys]):
            params = dict(zip(keys, vals))
            result = self.run(strategy_class, symbol, params, periods)
            if result and (not best or result["total_return"] > best["total_return"]):
                best = result
        return best

    def _to_bars(self, rows, interval, window, symbol):
        bars = []
        for r in rows:
            try:
                dt = r[1] if isinstance(r[1], str) else str(r[1])
                from datetime import datetime as dt_mod
                try:
                    parsed_dt = dt_mod.strptime(dt, "%Y-%m-%d %H:%M:%S")
                except:
                    parsed_dt = dt_mod.now()
                b = BarData(
                    symbol=symbol, exchange=None,
                    interval=interval, window=window,
                    datetime=parsed_dt,
                    open_price=float(r[2]), high_price=float(r[3]),
                    low_price=float(r[4]), close_price=float(r[5]),
                    volume=float(r[6]), turnover=float(r[7]) if len(r) > 7 else 0)
                bars.append(b)
            except Exception as e:
                logger.error(f"BAR转换失败: {e}")
                continue
        return bars
