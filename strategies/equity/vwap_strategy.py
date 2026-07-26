"""
strategies/equity/vwap_strategy.py - v2.8.0
VWAP 均值回归策略：价格偏离 VWAP 时反向交易
v2.8.0 优化：继承 ApolloBaseStrategy，统一接口规范
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class VWAPStrategy(ApolloBaseStrategy):
    """VWAP 均值回归策略"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "vwap_period",          # VWAP 计算周期（分钟）
        "deviation_entry",      # 入场偏离倍数（标准差）
        "deviation_exit",       # 出场偏离倍数
        "max_holding_bars",     # 最大持仓Bar数
        "use_keltner",          # 使用 Keltner 通道代替固定倍数
        "keltner_atr_multiplier", # Keltner ATR 倍数
    ]
    variables = ApolloBaseStrategy.variables + [
        "vwap_val", "deviation_val",
        "bars_held", "upper_band", "lower_band",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "vwap_period": 20,
        "deviation_entry": 2.0,
        "deviation_exit": 0.5,
        "max_holding_bars": 30,
        "use_keltner": True,
        "keltner_atr_multiplier": 1.5,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.vwap_val = 0.0
        self.deviation_val = 0.0
        self.bars_held = 0
        self.upper_band = 0.0
        self.lower_band = 0.0

        from vnpy.trader.utility import BarGenerator, ArrayManager
        self.bg = BarGenerator(self.on_bar, 1, self.on_1m_bar)
        self.am = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"VWAP策略初始化 | 周期={self.vwap_period} 入场偏离={self.deviation_entry}σ")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg.update_bar(bar)

    def on_1m_bar(self, bar: BarData):
        """1分钟K线核心逻辑"""
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        close = bar.close_price

        # 计算 VWAP
        self.vwap_val = self._calc_vwap(am)

        # 计算偏离度（标准差）
        if len(am.close) >= self.vwap_period:
            std = float(np.std(am.close[-self.vwap_period:]))
            if std > 0:
                self.deviation_val = (close - self.vwap_val) / std
            else:
                self.deviation_val = 0.0
        else:
            std = 0.0
            self.deviation_val = 0.0

        # 计算通道
        if self.use_keltner:
            atr = am.atr(self.atr_period if hasattr(self, 'atr_period') else 14, array=False)
            self.upper_band = self.vwap_val + atr * self.keltner_atr_multiplier
            self.lower_band = self.vwap_val - atr * self.keltner_atr_multiplier
        else:
            self.upper_band = self.vwap_val + std * self.deviation_entry
            self.lower_band = self.vwap_val - std * self.deviation_entry

        # ── 持仓管理 ──
        if self.pos != 0:
            self.bars_held += 1

            if self.pos > 0:
                # 多头：回归VWAP或超时平仓
                if close <= self.vwap_val or self.bars_held >= self.max_holding_bars:
                    self.sell(close, abs(self.pos))
                    self.write_log(f"📊 VWAP回归平仓(多) | close={close:.2f} vwap={self.vwap_val:.2f}")
                    self.bars_held = 0
                    return

                # 止损
                if close <= self.entry_price * (1 - self.stop_loss_pct):
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ VWAP止损(多) @ {close:.2f}")
                    self.bars_held = 0
                    return
            else:
                # 空头：回归VWAP或超时平仓
                if close >= self.vwap_val or self.bars_held >= self.max_holding_bars:
                    self.cover(close, abs(self.pos))
                    self.write_log(f"📊 VWAP回归平仓(空) | close={close:.2f} vwap={self.vwap_val:.2f}")
                    self.bars_held = 0
                    return

                if close >= self.entry_price * (1 + self.stop_loss_pct):
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ VWAP止损(空) @ {close:.2f}")
                    self.bars_held = 0
                    return

        # ── 开仓信号 ──
        else:
            # 价格跌破下轨 → 做多（均值回归）
            if close <= self.lower_band:
                self.buy(close, self.fixed_size)
                self.bars_held = 0
                self.write_log(
                    f"🟢 VWAP做多 | close={close:.2f} vwap={self.vwap_val:.2f} "
                    f"dev={self.deviation_val:.2f}σ"
                )
            # 价格突破上轨 → 做空
            elif close >= self.upper_band:
                self.short(close, self.fixed_size)
                self.bars_held = 0
                self.write_log(
                    f"🔴 VWAP做空 | close={close:.2f} vwap={self.vwap_val:.2f} "
                    f"dev={self.deviation_val:.2f}σ"
                )

    def _calc_vwap(self, am) -> float:
        """计算 VWAP（成交量加权均价）"""
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

        if total_v > 0:
            return total_pv / total_v
        return float(closes[-1])

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 VWAP成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
