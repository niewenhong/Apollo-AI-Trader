"""
strategies/equity/multi_indicator_strategy.py - v2.7.0
多指标综合评分策略：融合均线、RSI、MACD、布林带、ATR、成交量等10维指标
v2.7.0 新增：从数据库加载参数 + 版本上报 + 修改来源记录
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset, Interval
import numpy as np
import json
import time
from pathlib import Path

try:
    from core.db_manager import CustomDBManager
    HAS_DB = True
except ImportError:
    HAS_DB = False


class MultiIndicatorStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "ma_fast",
        "ma_slow",
        "rsi_period",
        "rsi_overbought",
        "rsi_oversold",
        "atr_period",
        "atr_multiplier",
        "fixed_size",
        # v2.7.0 新增参数
        "score_threshold_long",
        "score_threshold_short",
        "vwap_deviation_entry",
        "rvol_threshold",
        "kelly_max_fraction",
        "profit_activation_pct",
        "trailing_stop_pct",
    ]

    variables = [
        "pos", "entry_price", "score",
        "ma_fast_val", "ma_slow_val",
        "rsi_val", "atr_val", "pnl",
        "current_stop", "current_target",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 默认参数
        self.ma_fast = setting.get("ma_fast", 5)
        self.ma_slow = setting.get("ma_slow", 20)
        self.rsi_period = setting.get("rsi_period", 14)
        self.rsi_overbought = setting.get("rsi_overbought", 75)
        self.rsi_oversold = setting.get("rsi_oversold", 30)
        self.atr_period = setting.get("atr_period", 14)
        self.atr_multiplier = setting.get("atr_multiplier", 2.0)
        self.fixed_size = setting.get("fixed_size", 100)

        # v2.7.0 新增参数（带默认值）
        self.score_threshold_long = setting.get("score_threshold_long", 65)
        self.score_threshold_short = setting.get("score_threshold_short", 35)
        self.vwap_deviation_entry = setting.get("vwap_deviation_entry", 0.003)
        self.rvol_threshold = setting.get("rvol_threshold", 1.5)
        self.kelly_max_fraction = setting.get("kelly_max_fraction", 0.06)
        self.profit_activation_pct = setting.get("profit_activation_pct", 0.008)
        self.trailing_stop_pct = setting.get("trailing_stop_pct", 0.005)

        # 状态变量
        self.pos = 0
        self.entry_price = 0.0
        self.score = 50.0
        self.ma_fast_val = 0.0
        self.ma_slow_val = 0.0
        self.rsi_val = 50.0
        self.atr_val = 0.0
        self.pnl = 0.0
        self.current_stop = 0.0
        self.current_target = 0.0
        self._trailing_activated = False

        # Bar 生成器
        self.bg_1min = BarGenerator(self.on_bar, 1, self.on_1min_bar)
        self.am_1min = ArrayManager(size=100)
        self.bg_5min = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am_5min = ArrayManager(size=100)

        # v2.7.0：从数据库加载 AI 优化参数
        self._load_ai_params()

        # 记录参数版本（用于上报）
        self._param_version = setting.get("_version", 1)

    def _load_ai_params(self):
        """从数据库加载 AI 建议的参数（如果存在）"""
        if not HAS_DB:
            return
        try:
            db = CustomDBManager()
            vt_symbol = self.vt_symbol
            strategy_class = self.__class__.__name__
            ai_params = db.get_latest_params(vt_symbol, strategy_class)
            if ai_params:
                for key, value in ai_params.items():
                    if hasattr(self, key):
                        setattr(self, key, value)
                self.write_log(f"已加载AI优化参数: {list(ai_params.keys())}")
        except Exception as e:
            self.write_log(f"加载AI参数失败（使用默认）: {e}")

    def on_init(self):
        self.load_bar(30, use_database=True)
        self.write_log(f"MultiIndicator策略初始化完成 (v{self._param_version})")

    def on_start(self):
        self.write_log(f"MultiIndicator策略启动 | 参数版本 v{self._param_version}")

    def on_stop(self):
        self.write_log("MultiIndicator策略停止")

    def on_tick(self, tick: TickData):
        self.bg_1min.update_tick(tick)
        self.bg_5min.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg_1min.update_bar(bar)
        self.bg_5min.update_bar(bar)

    def on_1min_bar(self, bar: BarData):
        """1分钟K线回调，更新指标"""
        self.am_1min.update_bar(bar)
        if not self.am_1min.inited:
            return

        # 计算各项指标
        self.ma_fast_val = self.am_1min.sma(self.ma_fast, array=False)
        self.ma_slow_val = self.am_1min.sma(self.ma_slow, array=False)
        self.rsi_val = self.am_1min.rsi(self.rsi_period, array=False)
        self.atr_val = self.am_1min.atr(self.atr_period, array=False)

        # 计算综合评分
        self.score = self._calculate_score(bar)

        # 交易逻辑
        if self.pos == 0:
            if self.score >= self.score_threshold_long:
                self.buy(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.current_stop = bar.close_price - self.atr_val * self.atr_multiplier
                self.current_target = bar.close_price * (1 + self.profit_activation_pct)
                self._trailing_activated = False
                self.write_log(
                    f"🟢 多头开仓: 评分{self.score:.1f} "
                    f"price={bar.close_price:.2f} qty={self.fixed_size}"
                )
            elif self.score <= self.score_threshold_short:
                self.short(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.current_stop = bar.close_price + self.atr_val * self.atr_multiplier
                self.write_log(
                    f"🔴 空头开仓: 评分{self.score:.1f} "
                    f"price={bar.close_price:.2f} qty={self.fixed_size}"
                )
        else:
            self._manage_position(bar)

    def on_5min_bar(self, bar: BarData):
        """5分钟K线回调（备用）"""
        self.am_5min.update_bar(bar)

    def _calculate_score(self, bar: BarData) -> float:
        """10维指标综合评分 (0-100)"""
        score = 50.0

        # 1. 均线关系（权重15）
        if self.ma_fast_val > self.ma_slow_val:
            score += 15
        else:
            score -= 15

        # 2. 价格相对均线位置（权重10）
        if bar.close_price > self.ma_fast_val:
            score += 10
        elif bar.close_price < self.ma_slow_val:
            score -= 10

        # 3. RSI（权重15）
        if self.rsi_val < self.rsi_oversold:
            score += 15
        elif self.rsi_val > self.rsi_overbought:
            score -= 15

        # 4. ATR波动率（权重5）
        atr_ratio = self.atr_val / bar.close_price if bar.close_price > 0 else 0
        if atr_ratio < 0.02:
            score += 5
        elif atr_ratio > 0.05:
            score -= 5

        # 5. 成交量变化（权重10）
        if len(self.am_1min.volume) > 20:
            vol_ma = np.mean(self.am_1min.volume[-20:])
            if vol_ma > 0 and self.am_1min.volume[-1] > vol_ma * 1.5:
                score += 10
            elif vol_ma > 0 and self.am_1min.volume[-1] < vol_ma * 0.5:
                score -= 5

        # 6. 趋势强度 ADX简化（权重10）
        if self.am_1min.inited and len(self.am_1min.close) >= 14:
            plus_di = self.am_1min.plus_di(self.atr_period, array=False)
            minus_di = self.am_1min.minus_di(self.atr_period, array=False)
            if plus_di > minus_di:
                score += 10
            else:
                score -= 10

        # 7. 波动率收缩（权重10）
        if len(self.am_1min.high) >= 10 and len(self.am_1min.low) >= 10:
            recent_high = np.max(self.am_1min.high[-10:])
            recent_low = np.min(self.am_1min.low[-10:])
            range_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0
            if range_pct < 0.05:
                score += 10
            elif range_pct > 0.15:
                score -= 5

        # 8. MACD 柱状图方向（权重10）
        if self.am_1min.inited and len(self.am_1min.close) >= 26:
            macd, signal, hist = self.am_1min.macd(12, 26, 9, array=True)
            if len(hist) > 1:
                if hist[-1] > 0 and hist[-1] > hist[-2]:
                    score += 10
                elif hist[-1] < 0 and hist[-1] < hist[-2]:
                    score -= 10

        # 9. 布林带位置（权重10）
        if self.am_1min.inited and len(self.am_1min.close) >= 20:
            bb_up, bb_mid, bb_low = self.am_1min.bollinger(20, 2, array=True)
            if len(bb_up) > 0 and len(bb_low) > 0:
                if bar.close_price > bb_up[-1]:
                    score -= 10
                elif bar.close_price < bb_low[-1]:
                    score += 10

        # 10. 连续涨跌（权重5）
        if len(self.am_1min.close) >= 3:
            closes = self.am_1min.close[-3:]
            if all(closes[i] > closes[i-1] for i in range(1, 3)):
                score += 5
            elif all(closes[i] < closes[i-1] for i in range(1, 3)):
                score -= 5

        return max(0.0, min(100.0, score))

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、反向平仓"""
        # 计算浮动盈亏
        if self.pos > 0:
            self.pnl = (bar.close_price - self.entry_price) * self.pos
        else:
            self.pnl = (self.entry_price - bar.close_price) * abs(self.pos)

        # 跟踪止损
        if self._trailing_activated and self.pos > 0:
            new_stop = bar.close_price * (1 - self.trailing_stop_pct)
            if new_stop > self.current_stop:
                self.current_stop = new_stop
                self.write_log(f"跟踪止损上移至 {self.current_stop:.2f}")

        # 激活跟踪止损
        if not self._trailing_activated and self.pos > 0:
            pnl_pct = (bar.close_price - self.entry_price) / self.entry_price
            if pnl_pct >= self.profit_activation_pct:
                self._trailing_activated = True
                self.current_stop = bar.close_price * (1 - self.trailing_stop_pct)
                self.write_log(f"跟踪止损激活 @ {self.current_stop:.2f}")

        # ATR 止损
        atr_stop = self.atr_val * self.atr_multiplier

        # 多头止损
        if self.pos > 0:
            if bar.close_price <= self.current_stop and self._trailing_activated:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log(f"🛡️ 跟踪止损: 多头 @ {bar.close_price:.2f} PnL={self.pnl:.2f}")
                self._reset_position()
            elif bar.close_price <= self.entry_price - atr_stop:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log(f"🛡️ ATR止损: 多头 @ {bar.close_price:.2f} PnL={self.pnl:.2f}")
                self._reset_position()

            # 固定止盈
            if bar.close_price >= self.entry_price * (1 + self.profit_activation_pct * 2):
                self.sell(bar.close_price, abs(self.pos))
                self.write_log(f"🎯 止盈: 多头 @ {bar.close_price:.2f} PnL={self.pnl:.2f}")
                self._reset_position()

            # 评分反转平仓
            if self.score <= self.score_threshold_short:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log(f"🔁 评分反转平仓: 多头 score={self.score:.1f}")
                self._reset_position()

        # 空头止损
        elif self.pos < 0:
            if bar.close_price >= self.entry_price + atr_stop:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log(f"🛡️ ATR止损: 空头 @ {bar.close_price:.2f} PnL={self.pnl:.2f}")
                self._reset_position()

            if self.score >= self.score_threshold_long:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log(f"🔁 评分反转平仓: 空头 score={self.score:.1f}")
                self._reset_position()

    def _reset_position(self):
        """重置持仓状态"""
        self.pos = 0
        self.entry_price = 0.0
        self.current_stop = 0.0
        self.current_target = 0.0
        self._trailing_activated = False

    def on_trade(self, trade):
        self.write_log(
            f"💰 成交: {trade.direction.name} {trade.volume}手 @ {trade.price:.2f}"
        )
