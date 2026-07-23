# -*- coding: utf-8 -*-
"""TradingHours 单元测试"""
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils.trading_hours import (
    is_trading_hour, is_us_regular_hours, is_hk_regular_hours
)


class TestTradingHours(unittest.TestCase):

    def test_hk_morning_session(self):
        """港股上午时段"""
        dt = datetime(2026, 7, 15, 10, 30)  # 周二上午
        self.assertTrue(is_trading_hour("0700", "HKEX", dt))

    def test_hk_lunch_break(self):
        """港股午休"""
        dt = datetime(2026, 7, 15, 12, 30)
        self.assertFalse(is_trading_hour("0700", "HKEX", dt))

    def test_hk_afternoon_session(self):
        """港股下午时段"""
        dt = datetime(2026, 7, 15, 14, 0)
        self.assertTrue(is_trading_hour("0700", "HKEX", dt))

    def test_us_morning(self):
        """美股上午"""
        # 美东时间 10:30 = UTC 14:30 (夏令时)
        dt_utc = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
        self.assertTrue(is_trading_hour("NVDA", "SMART", dt_utc))

    def test_us_after_hours(self):
        """美股收盘后"""
        dt_utc = datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)  # 美东 18:00
        self.assertFalse(is_trading_hour("NVDA", "SMART", dt_utc))

    def test_unknown_exchange(self):
        """未知交易所默认放行"""
        dt = datetime(2026, 7, 15, 12, 0)
        self.assertTrue(is_trading_hour("XXX", "UNKNOWN", dt))


if __name__ == "__main__":
    unittest.main()
