# -*- coding: utf-8 -*-
"""绩效统计（夏普比率、最大回撤、胜率）"""
import logging
import numpy as np
from typing import List, Dict
from collections import deque

logger = logging.getLogger("core.performance_tracker")

class PerformanceTracker:
    """实时绩效跟踪"""

    def __init__(self, window: int = 252):
        self._equity_curve: deque = deque(maxlen=window * 10)
        self._trade_pnls: List[float] = []
        self._daily_returns: List[float] = []
        self._high_water_mark: float = 0.0
        self._current_drawdown: float = 0.0
        self._max_drawdown: float = 0.0
        self._lock = __import__("threading").Lock()

    def update(self, timestamp, equity: float):
        """更新权益曲线"""
        with self._lock:
            self._equity_curve.append((timestamp, equity))
            if equity > self._high_water_mark:
                self._high_water_mark = equity
            dd = (self._high_water_mark - equity) / self._high_water_mark if self._high_water_mark > 0 else 0
            self._current_drawdown = dd
            if dd > self._max_drawdown:
                self._max_drawdown = dd

            # 日收益率
            if len(self._equity_curve) >= 2:
                prev_eq = self._equity_curve[-2][1]
                if prev_eq > 0:
                    ret = (equity - prev_eq) / prev_eq
                    self._daily_returns.append(ret)

    def add_trade_pnl(self, pnl: float):
        with self._lock:
            self._trade_pnls.append(pnl)

    def get_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """年化夏普比率"""
        with self._lock:
            if len(self._daily_returns) < 30:
                return 0.0
            returns = np.array(self._daily_returns)
            excess = returns - (risk_free_rate / 252.0)
            if excess.std() == 0:
                return 0.0
            sharpe = excess.mean() / excess.std() * np.sqrt(252)
            return round(float(sharpe), 2)

    def get_max_drawdown_pct(self) -> float:
        with self._lock:
            return round(self._max_drawdown * 100.0, 2)

    def get_win_rate(self) -> float:
        """胜率"""
        with self._lock:
            if not self._trade_pnls:
                return 0.0
            wins = sum(1 for p in self._trade_pnls if p > 0)
            return round((wins / len(self._trade_pnls)) * 100.0, 2)

    def get_profit_factor(self) -> float:
        """盈亏比"""
        with self._lock:
            gross_profit = sum(p for p in self._trade_pnls if p > 0)
            gross_loss = abs(sum(p for p in self._trade_pnls if p < 0))
            if gross_loss == 0:
                return float('inf') if gross_profit > 0 else 0.0
            return round(gross_profit / gross_loss, 2)

    def get_summary(self) -> Dict:
        with self._lock:
            return {
                "sharpe_ratio": self.get_sharpe_ratio(),
                "max_drawdown_pct": self.get_max_drawdown_pct(),
                "win_rate": self.get_win_rate(),
                "profit_factor": self.get_profit_factor(),
                "total_trades": len(self._trade_pnls),
                "current_drawdown_pct": round(self._current_drawdown * 100.0, 2)
            }
