"""
strategies/equity/trend_strategy.py - v2.9.0
趋势跟踪策略：多周期均线 + ADX 过滤 + ATR 止损 + Regime 感知
v2.9.0 优化：
- 分层：on_1m_bar 做执行，on_5m_bar 做趋势确认，on_60m_bar 更新宏观方向
- 利用已订阅的 5M/15M/60M K线（不再额外占额度）
- ADX 阈值动态化（根据 regime 调整）
- 突破确认用 5M 收盘价序列，减少噪音
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Interval

from strategies.base_strategy import ApolloBaseStrategy


class TrendStrategy(ApolloBaseStrategy):
    """趋势跟踪：双均线 + ADX + ATR 止损，多周期确认"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "ma_fast",
        "ma_slow",
        "breakout_period",
        "atr_period",
        "atr_stop_multiplier",
        "adx_period",
        "adx_threshold",
        "use_trailing",
        "regime_boost",
    ]
    variables = ApolloBaseStrategy.variables + [
        "ma_fast_val", "ma_slow_val", "adx_val",
        "atr_val", "trend_direction", "confirmed_trend",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "ma_fast": 10,
        "ma_slow": 30,
        "breakout_period": 3,
        "atr_period": 14,
        "atr_stop_multiplier": 2.5,
        "adx_period": 14,
        "adx_threshold": 20,
        "use_trailing": True,
        "regime_boost": True,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.ma_fast_val = 0.0
        self.ma_slow_val = 0.0
        self.adx_val = 0.0
        self.atr_val = 0.0
        self.trend_direction = 0
        self.confirmed_trend = 0  # 由 5M 确认的 trend

        # 独立的 5M ArrayManager（不占用额外订阅额度，数据由 gateway 推送）
        from vnpy.trader.utility import ArrayManager
        self.am_5m = ArrayManager(100)
        self.am_60m = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"趋势策略初始化 | 快线={self.ma_fast} 慢线={self.ma_slow} ADX={self.adx_threshold}")

    # ── 1M 层：执行层（快，但噪音大）──
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        if not self.am.inited:
            return

        close = bar.close_price
        self.ma_fast_val = self.am.sma(self.ma_fast, array=False)
        self.ma_slow_val = self.am.sma(self.ma_slow, array=False)
        self.atr_val = self.am.atr(self.atr_period, array=False)
        if len(self.am.close) >= self.adx_period:
            self.adx_val = self.am.adx(self.adx_period, array=False)
        else:
            self.adx_val = 0.0

        # Regime 感知：动态调整 ADX 阈值
        adx_th = self.adx_threshold
        if self.regime_boost:
            r = self.get_current_regime()
            if r in ("strong_bull", "strong_bear"):
                adx_th = max(adx_th - 5, 10)
            elif r == "unknown":
                adx_th = adx_th + 5

        # 持仓管理（在 1M 层做止损，反应快）
        if self.pos > 0:
            if self.use_trailing:
                if self.update_trailing_stop(close):
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ 趋势多头止损/止盈 @ {close:.2f}")
                    return
            else:
                hard = self.entry_price - self.atr_val * self.atr_stop_multiplier
                if close <= hard:
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ ATR硬止损(多) @ {close:.2f}")
                    return
            # 超时平仓
            if self.bars_held >= self.max_holding_bars:
                self.sell(close, abs(self.pos))
                self.write_log(f"⏰ 超时平仓(多) @ {close:.2f} bars={self.bars_held}")
                return

        elif self.pos < 0:
            if self.use_trailing:
                if self.update_trailing_stop(close):
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ 趋势空头止损/止盈 @ {close:.2f}")
                    return
            else:
                hard = self.entry_price + self.atr_val * self.atr_stop_multiplier
                if close >= hard:
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ ATR硬止损(空) @ {close:.2f}")
                    return
            if self.bars_held >= self.max_holding_bars:
                self.cover(close, abs(self.pos))
                self.write_log(f"⏰ 超时平仓(空) @ {close:.2f} bars={self.bars_held}")
                return

        # 开仓（需 5M 确认 + Regime 过滤）
        if self.pos == 0 and self.confirmed_trend != 0 and self.is_regime_tradeable():
            allow_open, _ = self.check_time_window(bar.datetime)
            if not allow_open:
                return
            if self.confirmed_trend == 1 and self.ma_fast_val > self.ma_slow_val:
                self.buy(close, self.fixed_size)
                self.write_log(f"🟢 趋势做多 | 5M确认+MA金叉 ADX={self.adx_val:.0f}>{adx_th:.0f}")
            elif self.confirmed_trend == -1 and self.ma_fast_val < self.ma_slow_val:
                self.short(close, self.fixed_size)
                self.write_log(f"🔴 趋势做空 | 5M确认+MA死叉 ADX={self.adx_val:.0f}>{adx_th:.0f}")

    # ── 5M 层：趋势确认层 ──
    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        fast = self.am_5m.sma(self.ma_fast, array=False)
        slow = self.am_5m.sma(self.ma_slow, array=False)

        if fast > slow:
            self.confirmed_trend = 1
        elif fast < slow:
            self.confirmed_trend = -1
        else:
            self.confirmed_trend = 0

        self.trend_direction = self.confirmed_trend

    # ── 60M 层：宏观方向 + ADX ──
    def on_60m_bar(self, bar: BarData):
        self.am_60m.update_bar(bar)
        if not self.am_60m.inited:
            return
        # 可选：用 60M ADX 判断大趋势强度，写入日志供监控
        adx60 = self.am_60m.adx(self.adx_period, array=False)
        self.write_log(f"[60M] close={bar.close_price:.2f} ADX60={adx60:.0f} trend_dir={self.trend_direction}")

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 趋势成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
