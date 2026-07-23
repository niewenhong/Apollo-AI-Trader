# -*- coding: utf-8 -*-
"""Shelly 算法单元测试"""
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from execution.allocation import (
    calculate_position_size,
    calculate_from_atr,
    allocate_across_accounts,
    round_to_lot
)


class TestShelly(unittest.TestCase):

    def test_basic_calculation(self):
        """基本仓位计算"""
        size = calculate_position_size(
            account_equity=100000.0,
            risk_pct=1.0,
            entry_price=100.0,
            stop_loss_price=95.0,
            lot_size=1,
            min_tick=0.01,
            max_position=100
        )
        # 风险金额 = 1000，每股风险 = 5，理论 200 股
        self.assertEqual(size, 200)

    def test_lot_alignment(self):
        """手数对齐"""
        size = calculate_position_size(
            account_equity=100000.0,
            risk_pct=1.0,
            entry_price=100.0,
            stop_loss_price=95.0,
            lot_size=100,  # 港股
            min_tick=0.05,
            max_position=10
        )
        # 200 股 → 对齐到 100 股 = 2 手
        self.assertEqual(size, 2)

    def test_max_position_limit(self):
        """最大持仓限制"""
        size = calculate_position_size(
            account_equity=10000000.0,  # 很大
            risk_pct=5.0,
            entry_price=100.0,
            stop_loss_price=50.0,  # 大风险
            lot_size=1,
            min_tick=0.01,
            max_position=10
        )
        self.assertEqual(size, 10)

    def test_zero_risk(self):
        """零风险（止损=入场价）"""
        size = calculate_position_size(
            account_equity=100000.0,
            risk_pct=1.0,
            entry_price=100.0,
            stop_loss_price=100.0,
            lot_size=1,
            min_tick=0.01,
            max_position=100
        )
        self.assertEqual(size, 0)

    def test_atr_calculation(self):
        """ATR 版本"""
        size = calculate_from_atr(
            account_equity=100000.0,
            risk_pct=1.0,
            entry_price=500.0,
            atr_value=5.0,
            atr_multiplier=2.0,
            lot_size=1,
            max_position=50
        )
        # 风险金额=1000，止损距离=10，理论 100 股
        self.assertEqual(size, 100)

    def test_allocate_across_accounts(self):
        """多账户分配"""
        accounts = {"acc1": 60000, "acc2": 40000}
        alloc = allocate_across_accounts(10, accounts, lot_size=1)
        self.assertEqual(alloc["acc1"], 6)
        self.assertEqual(alloc["acc2"], 4)

    def test_round_to_lot(self):
        """手数对齐"""
        self.assertEqual(round_to_lot(157, 100), 100)
        self.assertEqual(round_to_lot(250, 100), 200)
        self.assertEqual(round_to_lot(99, 100), 0)


if __name__ == "__main__":
    unittest.main()
