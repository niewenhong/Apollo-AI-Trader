# -*- coding: utf-8 -*-
"""VWAP 策略单元测试"""
import sys
import os
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.equity.vwap_strategy import VwapStrategy
from vnpy.trader.object import BarData, TickData


class MockAdapter:
    """模拟 vnpy adapter"""
    def __init__(self):
        self.vt_symbol = "NVDA.SMART"
        self.orders = []
    def buy(self, price, size):
        self.orders.append(("buy", price, size))
    def sell(self, price, size):
        self.orders.append(("sell", price, size))
    def short(self, price, size):
        self.orders.append(("short", price, size))
    def cover(self, price, size):
        self.orders.append(("cover", price, size))
    def cancel_order(self, oid):
        pass


class TestVwapStrategy(unittest.TestCase):

    def setUp(self):
        self.adapter = MockAdapter()
        self.strat = VwapStrategy(self.adapter, settings={
            "threshold_long": -1.5,
            "threshold_short": 1.0,
            "exit_band": 0.3,
            "vol_rank_limit": 0.0,  # 禁用成交量过滤，方便测试
            "fixed_size": 1,
            "price_offset": 0.003,
            "dry_run": True,  # 不实际下单
            "debug_mode": True,
        })
        self.strat.pos = 0

    def _make_bar(self, close, vol=1000, high=None, low=None):
        bar = BarData(
            symbol="NVDA", exchange="SMART",
            datetime=datetime.now(),
            interval="1m",
            open_price=close, high_price=high or close,
            low_price=low or close*0.99, close_price=close,
            volume=vol
        )
        return bar

    def test_initial_state(self):
        """初始状态"""
        self.assertEqual(self.strat.pos, 0)
        self.assertEqual(self.strat.signal, "hold")

    def test_vwap_calculation(self):
        """VWAP 累积计算"""
        bars = [
            self._make_bar(100.0, vol=100),
            self._make_bar(101.0, vol=200),
            self._make_bar(99.0, vol=150),
        ]
        for b in bars:
            self.strat.on_bar(b)
        # VWAP = (100*100 + 101*200 + 99*150) / 450
        expected_vwap = (100*100 + 101*200 + 99*150) / 450
        self.assertAlmostEqual(self.strat.cum_turnover / self.strat.cum_volume, expected_vwap, places=2)

    def test_long_signal(self):
        """价格远低于 VWAP → 做多信号"""
        bars = []
        for i in range(30):
            bars.append(self._make_bar(100.0 + i*0.1, vol=1000))
        # 突然大跌
        bars.append(self._make_bar(85.0, vol=5000))  # -15%
        for b in bars:
            self.strat.on_bar(b)
        # deviation 应该很负
        self.assertLess(self.strat.tick_deviation, -1.5)

    def test_no_duplicate_bar(self):
        """同分钟 Bar 不重复处理"""
        bar = self._make_bar(100.0)
        self.strat.on_bar(bar)
        count_before = self.strat.count
        self.strat.on_bar(bar)  # 同分钟，应跳过
        self.assertEqual(self.strat.count, count_before)


if __name__ == "__main__":
    unittest.main()
