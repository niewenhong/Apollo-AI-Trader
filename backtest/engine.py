# -*- coding: utf-8 -*-
"""
回测引擎（支持多策略、多标的、多周期）
直接复用策略基类的计算逻辑，保证回测与实盘一致性。
"""
import json
import os
import logging
from typing import Type, Dict, List, Optional, Callable
from datetime import datetime
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.performance_tracker import PerformanceTracker
from core.db_manager import DBManager

logger = logging.getLogger("backtest.engine")


class Bar:
    """回测用 Bar 对象（模拟 vnpy BarData）"""
    def __init__(self, dt: datetime, o: float, h: float, l: float, c: float, v: float):
        self.datetime = dt
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.close_price = c
        self.high_price = h
        self.low_price = l
        self.volume = v
        self.open_price = o


class BacktestEngine:
    """回测引擎"""

    def __init__(self, capital: float = 100000.0, commission: float = 0.001,
                 slippage: float = 0.01):
        self.capital = capital
        self.commission = commission
        self.slippage = slippage
        self.tracker = PerformanceTracker()
        self.db = DBManager()
        self.results: List[dict] = []

    def run(self, strategy_class: Type[BaseStrategy],
            data: List[Bar],
            params: Optional[dict] = None,
            symbol: str = "TEST") -> dict:
        """
        运行单策略回测
        :param strategy_class: 策略类
        :param data: Bar 列表
        :param params: 策略参数
        :param symbol: 标的名
        :return: 回测结果
        """
        # 创建策略实例（无 vnpy adapter）
        strategy = strategy_class.__new__(strategy_class)
        strategy.backtest_mode = True
        strategy.vt_symbol = symbol
        strategy.symbol = symbol
        strategy.exchange = "BACKTEST"
        strategy.pos = 0
        strategy.target_pos = 0
        strategy.debug_mode = False
        strategy.dry_run = False
        strategy.risk_manager = type(strategy).__init__  # placeholder
        strategy.active_orders = {}

        # 应用参数
        if params:
            for k, v in params.items():
                setattr(strategy, k, v)

        # 初始化
        strategy.on_init()

        # 逐 Bar 回放
        cash = self.capital
        pos = 0
        entry_price = 0.0
        trades = []
        equity_curve = []

        for bar in data:
            # 更新策略
            strategy.on_bar(bar)

            # 获取目标仓位
            target = strategy.get_target_position()
            diff = target - pos

            if diff != 0:
                price = bar.close
                # 加滑点
                if diff > 0:
                    fill_price = price + self.slippage
                else:
                    fill_price = price - self.slippage

                # 计算手续费
                cost = abs(diff) * fill_price * self.commission
                cash -= cost

                # 更新持仓
                old_pos = pos
                pos = target

                # 记录成交
                trade = {
                    "datetime": bar.datetime,
                    "direction": "buy" if diff > 0 else "sell",
                    "volume": abs(diff),
                    "price": fill_price,
                    "pnl": 0.0
                }

                # 计算已实现盈亏（平仓时）
                if old_pos != 0 and ((old_pos > 0 and diff < 0) or (old_pos < 0 and diff > 0)):
                    # 平仓
                    if old_pos > 0:
                        realized = (fill_price - entry_price) * abs(diff)
                    else:
                        realized = (entry_price - fill_price) * abs(diff)
                    trade["pnl"] = realized
                    self.tracker.add_trade_pnl(realized)

                if pos != 0:
                    entry_price = fill_price

                trades.append(trade)

            # 计算当前权益
            market_value = pos * bar.close
            equity = cash + market_value
            equity_curve.append((bar.datetime, equity))
            self.tracker.update(bar.datetime, equity)

        # 最终统计
        final_equity = cash + pos * data[-1].close if data else self.capital
        total_return = ((final_equity - self.capital) / self.capital) * 100.0

        # 计算最大回撤
        max_dd = 0.0
        peak = self.capital
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # 胜率
        winning = sum(1 for t in trades if t["pnl"] > 0)
        losing = sum(1 for t in trades if t["pnl"] < 0)
        total_closed = winning + losing
        win_rate = (winning / total_closed * 100.0) if total_closed > 0 else 0.0

        # 盈亏比
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        result = {
            "strategy": strategy_class.__name__,
            "params": params or {},
            "total_return_pct": round(total_return, 2),
            "win_rate_pct": round(win_rate, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
            "num_trades": len(trades),
            "final_equity": round(final_equity, 2),
            "sharpe_ratio": self.tracker.get_sharpe_ratio()
        }

        self.results.append(result)
        logger.info(f"[Backtest] {result['strategy']} 收益={result['total_return_pct']}% "
                    f"胜率={result['win_rate_pct']}% 回撤={result['max_drawdown_pct']}%")

        # 写入数据库
        try:
            self.db.log_backtest(strategy_class.__name__, params or {}, result)
        except Exception as e:
            logger.warning(f"写入数据库失败: {e}")

        return result

    def optimize(self, strategy_class: Type[BaseStrategy],
                  data: List[Bar],
                  param_grid: Dict[str, list],
                  metric: str = "total_return_pct") -> List[dict]:
        """
        网格搜索参数优化
        :param param_grid: {"ema_fast": [10,20,30], "threshold": [0.5,1.0]}
        :param metric: 排序指标
        """
        import itertools
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        all_results = []

        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            result = self.run(strategy_class, data, params)
            all_results.append(result)

        # 按指标排序
        reverse = not metric.startswith("max_drawdown")
        all_results.sort(key=lambda x: x.get(metric, 0), reverse=reverse)
        return all_results[:20]  # Top 20

    def save_report(self, filepath: str = "data/export/backtest_report.json"):
        """保存回测报告"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(self.results, f, indent=4, default=str)
        logger.info(f"[Backtest] 报告已保存: {filepath}")

    def get_best_params(self, metric: str = "total_return_pct") -> dict:
        """获取最优参数"""
        if not self.results:
            return {}
        reverse = not metric.startswith("max_drawdown")
        sorted_r = sorted(self.results, key=lambda x: x.get(metric, 0), reverse=reverse)
        return sorted_r[0]
