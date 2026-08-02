"""
strategies/equity/multi_indicator_strategy.py - v3.1.4
10维共振策略 + 多周期确认 + Regime感知
继承 BaseStrategy，启动交易保护
"""
import numpy as np

from vnpy.trader.constant import Direction
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import ArrayManager

from strategies.base_strategy import BaseStrategy


class MultiIndicatorStrategy(BaseStrategy):
    """10+维共振策略（v3.1.4）"""

    author = "Apollo-AI-Trader"

    parameters = BaseStrategy.parameters + [
        "fast_window", "slow_window", "rsi_window",
        "macd_fast", "macd_slow", "macd_signal",
        "boll_period", "boll_dev",
        "atr_period",
        "adx_period",
        "volume_ma_period", "volume_spike_mult",
        "score_threshold", "sell_threshold",
        "use_5m_confirm",
        "use_regime_filter",
    ]
    variables = BaseStrategy.variables + [
        "composite_score", "cumulative_pnl",
        "macd_val", "rsi_val", "boll_upper", "boll_lower",
        "vol_ratio", "adx_val",
    ]

    DEFAULTS = {
        **BaseStrategy.DEFAULTS,
        "fast_window": 5,
        "slow_window": 20,
        "rsi_window": 14,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "boll_period": 20,
        "boll_dev": 2.0,
        "atr_period": 14,
        "adx_period": 14,
        "volume_ma_period": 20,
        "volume_spike_mult": 2.0,
        "score_threshold": 6,
        "sell_threshold": 2,
        "use_5m_confirm": True,
        "use_regime_filter": True,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.composite_score = 5
        self.cumulative_pnl = 0.0
        self.macd_val = 0.0
        self.rsi_val = 0.0
        self.boll_upper = 0.0
        self.boll_lower = 0.0
        self.vol_ratio = 1.0
        self.adx_val = 0.0

        # 5M 确认状态
        self._5m_agree = 0

        # 5M ArrayManager
        self.am_5m = ArrayManager(100)

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | 阈值买={self.score_threshold} 卖={self.sell_threshold}")

    def on_init(self):
        super().on_init()
        self.write_log(f"[on_init] ✅ 多指标策略初始化完成")

    def on_start(self):
        super().on_start()

    def on_stop(self):
        super().on_stop()

    # ────────────────────────
    #  1M 层：主决策
    # ────────────────────────
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        if not self.am.inited:
            return

        close = bar.close_price

        # 指标计算
        fast_ma = self.am.sma(self.fast_window, array=False)
        slow_ma = self.am.sma(self.slow_window, array=False)
        self.rsi_val = self.am.rsi(self.rsi_window, array=False)
        macd, macd_sig, macd_hist = self.am.macd(self.macd_fast, self.macd_slow, self.macd_signal)
        self.macd_val = macd_hist
        boll_up, boll_mid, boll_dn = self.am.boll(self.boll_period, self.boll_dev)
        self.boll_upper = boll_up
        self.boll_lower = boll_dn
        atr = self.am.atr(self.atr_period, array=False)

        # ADX
        if len(self.am.close) >= self.adx_period:
            self.adx_val = self.am.adx(self.adx_period, array=False)
        else:
            self.adx_val = 0.0

        # 成交量比率
        if len(self.am.volume) >= self.volume_ma_period:
            vol_ma = float(np.mean(self.am.volume[-self.volume_ma_period:]))
            current_vol = float(self.am.volume[-1]) if len(self.am.volume) > 0 else 0.0
            self.vol_ratio = (current_vol / vol_ma) if vol_ma > 0 else 1.0
        else:
            self.vol_ratio = 1.0

        # ── 10维评分 ──
        score = 5
        if fast_ma > slow_ma: score += 2
        elif fast_ma < slow_ma: score -= 2
        if self.rsi_val > 60: score += 1
        elif self.rsi_val < 40: score -= 1
        if macd_hist > 0: score += 1
        elif macd_hist < 0: score -= 1
        if close > boll_up: score += 1
        elif close < boll_dn: score -= 1
        if self.vol_ratio > self.volume_spike_mult: score += 1
        if self.adx_val > 25: score += 1
        if self.use_5m_confirm:
            if self._5m_agree == 1 and fast_ma > slow_ma: score += 1
            elif self._5m_agree == -1 and fast_ma < slow_ma: score -= 1
        if len(self.am.close) >= 10:
            if close > self.am.close[-10]: score += 0.5
            else: score -= 0.5

        self.composite_score = score

        # 时间窗口
        allow_open, must_close = self.check_time_window(bar.datetime)

        # 强制清仓
        if must_close and self.pos != 0:
            if self.pos > 0:
                self.sell(close, abs(self.pos))
            else:
                self.cover(close, abs(self.pos))
            self.write_log(f"🛑 尾盘清仓 | score={score:.1f}")
            return

        # Regime 过滤
        regime_ok = self.is_regime_tradeable() if self.use_regime_filter else True

        # 买入
        if score >= self.score_threshold and self.pos == 0 and regime_ok and allow_open:
            if getattr(self, '_trading_allowed', False):
                self.write_log(f"[SIGNAL] BUY | score={score:.1f} close={close:.2f} RSI={self.rsi_val:.0f}")
                self.on_buy_signal(self.fixed_size, close)

        # 卖出
        elif self.pos > 0 and (score <= self.sell_threshold or fast_ma < slow_ma):
            self.write_log(f"[SIGNAL] SELL | score={score:.1f} close={close:.2f}")
            self.on_sell_signal(abs(self.pos), close)

    # ────────────────────────
    #  5M 层：确认
    # ────────────────────────
    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        fast = self.am_5m.sma(self.fast_window, array=False)
        slow = self.am_5m.sma(self.slow_window, array=False)
        if fast > slow:
            self._5m_agree = 1
        elif fast < slow:
            self._5m_agree = -1
        else:
            self._5m_agree = 0

    # ────────────────────────
    #  下单
    # ────────────────────────
    def on_buy_signal(self, size: int, price: float):
        if not getattr(self, '_trading_allowed', False) or self.pos > 0 or self.is_ordering:
            return
        self.is_ordering = True
        try:
            vt = self.buy(price, size, stop=False, lock=False)
            self.write_log(f"[BUY] size={size} price={price:.2f} vt={vt}")
        except Exception as e:
            self.is_ordering = False
            self.write_log(f"[BUY] 异常: {e}")

    def on_sell_signal(self, size: int, price: float):
        if not getattr(self, '_trading_allowed', False) or self.pos <= 0 or self.is_ordering:
            return
        self.is_ordering = True
        try:
            vt = self.sell(price, size, stop=False, lock=False)
            self.write_log(f"[SELL] size={size} price={price:.2f} vt={vt}")
        except Exception as e:
            self.is_ordering = False
            self.write_log(f"[SELL] 异常: {e}")

    def on_trade(self, trade: TradeData):
        super().on_trade(trade)
        if trade.direction == Direction.LONG:
            self.cumulative_pnl -= trade.price * trade.volume
        else:
            self.cumulative_pnl += trade.price * trade.volume
        self.write_log(f"[on_trade] ✅ {trade.direction.name} {trade.volume}@{trade.price:.2f} cum_pnl={self.cumulative_pnl:.2f}")
