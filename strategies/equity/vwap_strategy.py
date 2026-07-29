"""
strategies/equity/vwap_strategy.py - v2.9.0
VWAP 均值回归策略 + 多周期确认 + Regime 感知
v2.9.0 优化：
- 继承 ApolloBaseStrategy
- 用 1M K线（已订阅）计算 VWAP，不依赖 tick 聚合
- 加入 5M 趋势过滤（避免逆势接刀）
- Keltner 通道 + ATR 动态带宽
- 超时平仓 + 止损
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class VWAPStrategy(ApolloBaseStrategy):
    """VWAP 均值回归策略（v2.9.0）"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "vwap_period",
        "deviation_entry",
        "deviation_exit",
        "use_keltner",
        "keltner_atr_multiplier",
        "use_5m_filter",
    ]
    variables = ApolloBaseStrategy.variables + [
        "vwap_val", "deviation_val",
        "upper_band", "lower_band",
        "_5m_trend",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "vwap_period": 20,
        "deviation_entry": 2.0,
        "deviation_exit": 0.5,
        "use_keltner": True,
        "keltner_atr_multiplier": 1.5,
        "use_5m_filter": True,
        "max_holding_bars": 30,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.vwap_val = 0.0
        self.deviation_val = 0.0
        self.upper_band = 0.0
        self.lower_band = 0.0
        self._5m_trend = 0

        from vnpy.trader.utility import ArrayManager
        self.am_5m = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"VWAP策略初始化 | 周期={self.vwap_period} 入场偏离={self.deviation_entry}σ")

    # ── 1M 层：核心逻辑 ──
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        if not self.am.inited:
            return

        close = bar.close_price
        am = self.am

        # VWAP
        self.vwap_val = self._calc_vwap(am)

        # 标准差
        period = min(self.vwap_period, len(am.close))
        if period >= 5:
            std = float(np.std(am.close[-period:]))
        else:
            std = 0.0
        self.deviation_val = (close - self.vwap_val) / std if std > 0 else 0.0

        # 通道
        atr = am.atr(self.atr_period if hasattr(self, 'atr_period') else 14, array=False)
        if self.use_keltner:
            self.upper_band = self.vwap_val + atr * self.keltner_atr_multiplier
            self.lower_band = self.vwap_val - atr * self.keltner_atr_multiplier
        else:
            self.upper_band = self.vwap_val + std * self.deviation_entry
            self.lower_band = self.vwap_val - std * self.deviation_entry

        # ── 持仓管理 ──
        if self.pos != 0:
            if self.pos > 0:
                if close <= self.vwap_val or self.bars_held >= self.max_holding_bars:
                    self.sell(close, abs(self.pos))
                    self.write_log(f"📊 VWAP回归平仓(多) | {close:.2f}→vwap={self.vwap_val:.2f}")
                    return
                if close <= self.entry_price * (1 - self.stop_loss_pct):
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ VWAP止损(多) @ {close:.2f}")
                    return
            else:
                if close >= self.vwap_val or self.bars_held >= self.max_holding_bars:
                    self.cover(close, abs(self.pos))
                    self.write_log(f"📊 VWAP回归平仓(空) | {close:.2f}→vwap={self.vwap_val:.2f}")
                    return
                if close >= self.entry_price * (1 + self.stop_loss_pct):
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ VWAP止损(空) @ {close:.2f}")
                    return

        # ── 开仓 ──
        else:
            # 5M 过滤
            if self.use_5m_filter and self._5m_trend == -1 and close <= self.lower_band:
                self.write_log(f"⏸ 5M趋势向下，跳过做多信号")
                return
            if self.use_5m_filter and self._5m_trend == 1 and close >= self.upper_band:
                self.write_log(f"⏸ 5M趋势向上，跳过做空信号")
                return

            # Regime 过滤
            if not self.is_regime_tradeable():
                return

            if close <= self.lower_band:
                self.buy(close, self.fixed_size)
                self.write_log(f"🟢 VWAP做多 | dev={self.deviation_val:.2f}σ lower={self.lower_band:.2f}")
            elif close >= self.upper_band:
                self.short(close, self.fixed_size)
                self.write_log(f"🔴 VWAP做空 | dev={self.deviation_val:.2f}σ upper={self.upper_band:.2f}")

    # ── 5M 层：趋势过滤 ──
    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        fast = self.am_5m.sma(10, array=False)
        slow = self.am_5m.sma(30, array=False)
        if fast > slow:
            self._5m_trend = 1
        elif fast < slow:
            self._5m_trend = -1
        else:
            self._5m_trend = 0

    # ── VWAP 计算 ──
    def _calc_vwap(self, am) -> float:
        period = min(self.vwap_period, len(am.close))
        if period < 2:
            return float(am.close[-1]) if len(am.close) > 0 else 0.0
        closes = am.close[-period:]
        volumes = am.volume[-period:]
        total_pv = 0.0
        total_v = 0.0
        for i in range(period):
            p = float(closes[i])
            v = float(volumes[i]) if i < len(volumes) else 0.0
            if v > 0:
                total_pv += p * v
                total_v += v
        return total_pv / total_v if total_v > 0 else float(closes[-1])

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 VWAP成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
