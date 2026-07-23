# -*- coding: utf-8 -*-
"""
卖出看跌期权策略（Sell Put / Cash-Secured Put）
卖出 OTM 看跌期权，收取权利金，赌标的不会跌破行权价。
适合温和看涨或震荡的市场环境。Theta 对你友好。
"""
import numpy as np
from datetime import datetime, timedelta
from vnpy.trader.object import BarData, TickData
from strategies.base_strategy import BaseStrategy


class SellPutStrategy(BaseStrategy):
    """卖出看跌策略（股票版：用备兑/现金担保思路）"""

    strategy_name = "sell_put"

    target_delta = 0.3          # 目标 Delta（找 OTM Put）
    days_to_expiry_min = 30
    days_to_expiry_max = 45
    min_premium_yield_pct = 1.5  # 最低权利金收益率
    max_strike_distance_pct = 10.0
    allocation_pct = 5.0         # 单笔占用资金占比
    fixed_size = 1

    def __init__(self, vnpy_adapter, settings=None):
        self.signal = "hold"
        self.selected_strike = 0.0
        self.expiry_date = ""
        self.premium_collected = 0.0
        super().__init__(vnpy_adapter, settings)

    def on_init(self):
        self.write_log("Sell Put 策略初始化完成")

    def on_bar(self, bar: BarData):
        """
        简化版逻辑（不直接交易期权合约，用股票模拟）：
        - 当价格回调到支撑位附近，模拟"卖出 Put 被行权 = 低价买入股票"
        - 当价格远离支撑位，平仓获利
        """
        if bar.close_price <= 0:
            return

        # 计算动态支撑位（用过去20日最低价）
        if not hasattr(self, '_lows'):
            self._lows = []
        self._lows.append(bar.low_price)
        if len(self._lows) > 20:
            self._lows.pop(0)

        if len(self._lows) < 10:
            return

        support = min(self._lows[-10:])
        current_price = bar.close_price

        # 距离支撑位的百分比
        distance_pct = ((current_price - support) / support) * 100.0 if support > 0 else 0

        new_signal = "hold"

        if self.pos <= 0:
            # 价格接近支撑位 → 模拟"卖出 Put"入场（低价接货）
            if distance_pct < self.max_strike_distance_pct * 0.5:
                new_signal = "long"
                self.write_log(
                    f"SELL PUT 入场: price={current_price:.2f} "
                    f"support={support:.2f} dist={distance_pct:.1f}%"
                )
        else:
            # 价格远离支撑位 → 平仓获利
            if distance_pct > self.max_strike_distance_pct:
                new_signal = "flat"
                self.write_log(
                    f"SELL PUT 平仓: price={current_price:.2f} profit_dist={distance_pct:.1f}%"
                )

        self.signal = new_signal

    def on_tick(self, tick: TickData):
        pass

    def calculate_signals(self, data) -> str:
        return self.signal

    def get_target_position(self) -> int:
        if self.signal == "long":
            return self.fixed_size
        elif self.signal == "flat":
            return 0
        return self.pos  # 持仓不变
