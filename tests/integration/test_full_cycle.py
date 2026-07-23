# -*- coding: utf-8 -*-
"""
完整交易周期集成测试
模拟：Bar 数据 → 策略信号 → 下单 → 成交 → 风控检查
"""
import sys
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from backtest.engine import BacktestEngine, Bar
from execution.allocation import calculate_position_size


class MockAdapter:
    def __init__(self):
        self.orders = []
        self.vt_symbol = "TEST.SMART"
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


class TestFullCycle(unittest.TestCase):

    def _make_bars(self, n=100, base=100.0, trend=0.0, vol=0.5):
        """生成模拟 K 线"""
        import random
        random.seed(42)
        bars = []
        price = base
        for i in range(n):
            change = random.uniform(-vol, vol) + trend
            o = price
            c = price + change
            h = max(o, c) + random.uniform(0, vol*0.5)
            l = min(o, c) - random.uniform(0, vol*0.5)
            v = random.randint(1000, 5000)
            dt = datetime(2024, 1, 2, 9, 30)  # 简化时间
            bars.append(Bar(dt=dt, o=o, h=h, l=l, c=c, v=v))
            price = c
        return bars

    def test_vwap_full_cycle(self):
        """VWAP 策略完整周期"""
        adapter = MockAdapter()
        strategy = VwapStrategy(adapter, settings={
            "threshold_long": -2.0,
            "threshold_short": 2.0,
            "exit_band": 0.5,
            "vol_rank_limit": 0.0,  # 禁用成交量过滤
            "fixed_size": 1,
            "dry_run": True,  # 不实际下单
            "debug_mode": False,
        })

        bars = self._make_bars(n=80, base=100.0, trend=0.05, vol=1.0)

        # 逐 Bar 运行
        signals = []
        for bar in bars:
            strategy.on_bar(bar)
            signals.append(strategy.signal)

        # 应该产生一些信号
        unique_signals = set(signals)
        self.assertIn("hold", unique_signals)
        print(f"  VWAP 信号分布: { {s: signals.count(s) for s in unique_signals} ")

    def test_triple_filter_full_cycle(self):
        """三重过滤策略完整周期"""
        adapter = MockAdapter()
        strategy = TripleFilterScalpStrategy(adapter, settings={
            "ema_fast": 10,
            "ema_slow": 30,
            "rsi_period": 6,
            "rsi_oversold": 20,
            "rsi_overbought": 80,
            "fixed_size": 1,
            "dry_run": True,
            "debug_mode": False,
        })

        bars = self._make_bars(n=100, base=50.0, trend=0.02, vol=0.3)

        signals = []
        for bar in bars:
            strategy.on_bar(bar)
            signals.append(strategy.signal)

        unique = set(signals)
        print(f"  TripleFilter 信号: { {s: signals.count(s) for s in unique} ")

    def test_backtest_engine(self):
        """回测引擎端到端"""
        bars = self._make_bars(n=200, base=100.0, trend=0.03, vol=0.8)
        engine = BacktestEngine(capital=100000.0, commission=0.001, slippage=0.01)

        # 用 VWAP 策略
        from strategies.equity.vwap_strategy import VwapStrategy
        params = {
            "threshold_long": -2.0,
            "threshold_short": 2.0,
            "exit_band": 0.5,
            "vol_rank_limit": 0.0,
            "fixed_size": 1,
        }
        result = engine.run(VwapStrategy, bars, params, symbol="TEST")

        print(f"  回测结果: 收益={result['total_return_pct']:+.2f}% "
              f"胜率={result['win_rate_pct']:.0f}% "
              f"回撤={result['max_drawdown_pct']:.2f}% "
              f"交易={result['num_trades']}笔")
        self.assertIsInstance(result, dict)
        self.assertIn("total_return_pct", result)

    def test_shelly_integration(self):
        """Shelly 仓位计算集成测试"""
        size = calculate_position_size(
            account_equity=100000.0,
            risk_pct=1.0,
            entry_price=500.0,
            stop_loss_price=490.0,
            lot_size=1,
            min_tick=0.01,
            max_position=50
        )
        # 风险金额=1000，每股风险=10，理论 100 股，但 max=50
        self.assertEqual(size, 50)

        size2 = calculate_position_size(
            account_equity=100000.0,
            risk_pct=0.5,
            entry_price=100.0,
            stop_loss_price=99.0,
            lot_size=100,  # 港股
            min_tick=0.05,
            max_position=10
        )
        # 风险金额=500，每股风险=1，理论 500 股，对齐到 100 = 5 手
        self.assertEqual(size2, 5)


if __name__ == "__main__":
    unittest.main()
