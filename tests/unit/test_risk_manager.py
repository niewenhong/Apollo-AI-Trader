# -*- coding: utf-8 -*-
"""RiskManager 单元测试"""
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.risk_manager import RiskManager


class TestRiskManager(unittest.TestCase):

    def setUp(self):
        self.rm = RiskManager(os.path.join(ROOT, "config/risk_config.json"))

    def test_daily_loss_breaker(self):
        """日内亏损熔断"""
        self.rm.reset_daily(start_equity=100000.0)
        # 模拟亏损 3%
        self.rm.update_equity(97000.0)
        allowed = self.rm.check("NVDA", 100.0, 0, 1, 97000.0)
        self.assertFalse(allowed)
        self.assertTrue(self.rm.is_breached)

    def test_position_limit(self):
        """仓位上限"""
        self.rm.reset_daily(start_equity=100000.0)
        # 请求买入 60000 美元 = 60% > 50% 限制
        allowed = self.rm.check("NVDA", 100.0, 0, 600, 100000.0)
        self.assertFalse(allowed)

    def test_frequency_limit(self):
        """下单频率限制"""
        self.rm.reset_daily(start_equity=100000.0)
        # 模拟 15 次下单
        for i in range(15):
            self.rm.check("NVDA", 100.0, 0, 1, 100000.0)
        # 第 11 次应该被拒绝
        allowed = self.rm.check("NVDA", 100.0, 0, 1, 100000.0)
        self.assertFalse(allowed)

    def test_normal_trading(self):
        """正常交易放行"""
        self.rm.reset_daily(start_equity=100000.0)
        allowed = self.rm.check("NVDA", 100.0, 0, 10, 100000.0)
        self.assertTrue(allowed)

    def test_get_status(self):
        """状态查询"""
        self.rm.reset_daily(start_equity=50000.0)
        self.rm.update_equity(49000.0)  # -2%
        status = self.rm.get_status()
        self.assertEqual(status["daily_loss"], 1000.0)
        self.assertAlmostEqual(status["daily_loss_pct"], 2.0, places=1)


if __name__ == "__main__":
    unittest.main()
