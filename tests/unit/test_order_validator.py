# -*- coding: utf-8 -*-
"""OrderValidator 单元测试"""
import sys
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from execution.order_validator import (
    validate_order_size,
    validate_price,
    validate_order
)


class TestOrderValidator(unittest.TestCase):

    def test_hk_stock_lot(self):
        """港股 100 股对齐"""
        size = validate_order_size("0700", "HKEX", 150)
        self.assertEqual(size, 100)

    def test_us_stock_lot(self):
        """美股 1 股"""
        size = validate_order_size("NVDA", "SMART", 7)
        self.assertEqual(size, 7)

    def test_warrant_lot(self):
        """涡轮 10000 份"""
        size = validate_order_size("0700.CALL.WT", "HKEX", 25000)
        self.assertEqual(size, 20000)

    def test_too_small_order(self):
        """手数太小拒绝"""
        size = validate_order_size("0700", "HKEX", 50)
        self.assertEqual(size, 0)

    def test_price_alignment_hk(self):
        """港股价格对齐"""
        price = validate_price(350.27, "HKEX")
        self.assertEqual(price, 350.25)

    def test_price_alignment_us(self):
        """美股价格对齐"""
        price = validate_price(152.347, "SMART")
        self.assertEqual(price, 152.35)

    def test_full_validation(self):
        """完整校验"""
        price, size = validate_order("NVDA", "SMART", 152.33, 5)
        self.assertEqual(price, 152.33)
        self.assertEqual(size, 5)

    def test_invalid_price(self):
        """无效价格"""
        price, size = validate_order("NVDA", "SMART", 0.001, 5)
        self.assertEqual(price, 0.0)
        self.assertEqual(size, 0)


if __name__ == "__main__":
    unittest.main()
