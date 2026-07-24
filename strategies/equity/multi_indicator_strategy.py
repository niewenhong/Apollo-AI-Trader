"""
strategies/equity/multi_indicator_strategy.py - v2.6.0 Enhanced
MultiIndicator 正股策略：多指标共振 + 凯利仓位 + VWAP过滤 + ATR止损
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset
import numpy as np
import math


class MultiIndicatorStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        # ---- EMA ----
        "ema_fast_period", "ema_slow_period",
        # ---- MACD ----
        "macd_fast", "macd_slow", "macd_signal_period",
        # ---- RSI ----
        "rsi_period", "rsi_oversold", "rsi_overbought",
        # ---- KDJ ----
        "kdj_n", "kdj_m", "kdj_s", "kdj_oversold", "kdj_overbought",
        # ---- ATR / 止损 ----
        "atr_period", "atr_stop_mult", "stop_loss_pct",
        # ---- Bollinger / Keltner ----
        "boll_period", "boll_mult", "keltner_mult",
        # ---- VWAP ----
        "vwap_deviation_entry", "vwap_deviation_exit",
        # ---- 成交量 ----
        "vol_ma_period", "rvol_threshold",
        # ---- 评分权重 ----
        "ema_bullish_w", "keltner_above_w", "macd_hist_up_w",
        "macd_golden_cross_w", "rsi_oversold_w", "kdj_golden_cross_w",
        "rvol_confirm_w", "vwap_near_w",
        "score_threshold_long", "score_threshold_short",
        # ---- 凯利公式 ----
        "kelly_win_rate", "kelly_win_loss_ratio",
        "kelly_capital", "kelly_max_fraction",
        # ---- 时间过滤 ----
        "enable_time_filter", "avoid_open_minutes",
        # ---- 止盈 ----
        "profit_activation_pct", "profit_rollback_pct",
        # ---- 调试 ----
        "test_mode",
    ]

    variables = [
        "pos", "entry_price", "score", "inited_bars",
        "kelly_fraction", "current_stop", "current_target",
        "vwap", "atr_value", "rvol",
    ]

    # ============================================================
    # 初始化
    # ============================================================
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ---- EMA ----
        self.ema_fast_period = setting.get("ema_fast_period", 20)
        self.ema_slow_period = setting.get("ema_slow_period", 60)

        # ---- MACD ----
        self.macd_fast = setting.get("macd_fast", 12)
        self.macd_slow = setting.get("macd_slow", 26)
        self.macd_signal_period = setting.get("macd_signal_period", 9)

        # ---- RSI ----
        self.rsi_period = setting.get("rsi_period", 6)
        self.rsi_oversold = setting.get("rsi_oversold", 25)
        self.rsi_overbought = setting.get("rsi_overbought", 78)

        # ---- KDJ ----
        self.kdj_n = setting.get("kdj_n", 9)
        self.kdj_m = setting.get("kdj_m", 3)
        self.kdj_s = setting.get("kdj_s", 3)
        self.kdj_oversold = setting.get("kdj_oversold", 20)
        self.kdj_overbought = setting.get("kdj_overbought", 80)

        # ---- ATR / 止损 ----
        self.atr_period = setting.get("atr_period", 14)
        self.atr_stop_mult = setting.get("atr_stop_mult", 1.5)
        self.stop_loss_pct = setting.get("stop_loss_pct", 0.008)

        # ---- Bollinger / Keltner ----
        self.boll_period = setting.get("boll_period", 20)
        self.boll_mult = setting.get("boll_mult", 2.0)
        self.keltner_mult = setting.get("keltner_mult", 2.0)

        # ---- VWAP ----
        self.vwap_deviation_entry = setting.get("vwap_deviation_entry", 0.003)
        self.vwap_deviation_exit = setting.get("vwap_deviation_exit", 0.0015)

        # ---- 成交量 ----
        self.vol_ma_period = setting.get("vol_ma_period", 20)
        self.rvol_threshold = setting.get("rvol_threshold", 1.5)

        # ---- 评分权重 ----
        self.ema_bullish_w = setting.get("ema_bullish_w", 2)
        self.keltner_above_w = setting.get("keltner_above_w", 1)
        self.macd_hist_up_w = setting.get("macd_hist_up_w", 2)
        self.macd_golden_cross_w = setting.get("macd_golden_cross_w", 2)
        self.rsi_oversold_w = setting.get("rsi_oversold_w", 2)
        self.kdj_golden_cross_w = setting.get("kdj_golden_cross_w", 1)
        self.rvol_confirm_w = setting.get("rvol_confirm_w", 1)
        self.vwap_near_w = setting.get("vwap_near_w", 1)
        self.score_threshold_long = setting.get("score_threshold_long", 6)
        self.score_threshold_short = setting.get("score_threshold_short", -4)

        # ---- 凯利公式 ----
        self.kelly_win_rate = setting.get("kelly_win_rate", 0.55)
        self.kelly_win_loss_ratio = setting.get("kelly_win_loss_ratio", 1.8)
        self.kelly_capital = setting.get("kelly_capital", 100000.0)
        self.kelly_max_fraction = setting.get("kelly_max_fraction", 0.08)

        # ---- 时间过滤 ----
        self.enable_time_filter = setting.get("enable_time_filter", True)
        self.avoid_open_minutes = setting.get("avoid_open_minutes", 30)

        # ---- 止盈 ----
        self.profit_activation_pct = setting.get("profit_activation_pct", 0.020)
        self.profit_rollback_pct = setting.get("profit_rollback_pct", 0.005)

        # ---- 调试 ----
        self.test_mode = setting.get("test_mode", True)

        # ---- 别名 ----
        self.ma_fast = self.ema_fast_period
        self.ma_slow = self.ema_slow_period

        # ---- 状态变量 ----
        self.pos = 0
        self.entry_price = 0.0
        self.score = 0
        self.inited_bars = 0
        self.kelly_fraction = 0.0
        self.current_stop = 0.0
        self.current_target = 0.0
        self.vwap = 0.0
        self.atr_value = 0.0
        self.rvol = 1.0
        self._trailing_activated = False
        self._cumulative_volume = 0.0
        self._vwap_sum = 0.0

        # ---- K线管理 ----
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=200)

    # ============================================================
    # 生命周期
    # ============================================================
    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("MultiIndicator策略[增强版]初始化完成")

    def on_start(self):
        self.write_log("MultiIndicator策略[增强版]启动")

    def on_stop(self):
        self.write_log("MultiIndicator策略[增强版]停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    # ============================================================
    # 核心：1分钟K线回调
    # ============================================================
    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        self.inited_bars += 1

        if not self.am.inited:
            return

        # ---- 计算所有指标 ----
        ema_fast = self.am.ema(self.ma_fast, array=False)
        ema_slow = self.am.ema(self.ma_slow, array=False)
        macd, signal, hist = self.am.macd(
            self.macd_fast, self.macd_slow, self.macd_signal_period
        )
        rsi = self.am.rsi(self.rsi_period)
        atr = self.am.atr(self.atr_period)

        # KDJ
        k, d = self._calc_kdj()

        # 布林带
        boll_mid = self.am.sma(self.boll_period, array=False)
        boll_std = np.std(self.am.close[-self.boll_period:])
        boll_upper = boll_mid + self.boll_mult * boll_std
        boll_lower = boll_mid - self.boll_mult * boll_std

        # Keltner 通道
        typical_price = (bar.high_price + bar.low_price + bar.close_price) / 3
        atr_ma = atr  # 当前ATR值
        keltner_upper = ema_slow + self.keltner_mult * atr_ma
        keltner_lower = ema_slow - self.keltner_mult * atr_ma

        # VWAP（日内累计）
        self._cumulative_volume += bar.volume
        self._vwap_sum += typical_price * bar.volume
        self.vwap = self._vwap_sum / self._cumulative_volume if self._cumulative_volume > 0 else bar.close_price

        # 成交量比率
        vol_ma = np.mean(self.am.volume[-self.vol_ma_period:])
        self.rvol = bar.volume / vol_ma if vol_ma > 0 else 1.0

        self.atr_value = atr

        # ---- 共振评分 ----
        score = 0
        score_detail = []

        # 1. EMA 多头排列
        if ema_fast > ema_slow:
            score += self.ema_bullish_w
            score_detail.append(f"EMA多+{self.ema_bullish_w}")
        else:
            score -= self.ema_bullish_w
            score_detail.append(f"EMA空-{self.ema_bullish_w}")

        # 2. 价格在 Keltner 上轨之上（强势）
        if bar.close_price > keltner_upper:
            score += self.keltner_above_w
            score_detail.append(f"Kelt上+{self.keltner_above_w}")

        # 3. MACD柱状图向上
        if hist > 0:
            score += self.macd_hist_up_w
            score_detail.append(f"MACD柱+{self.macd_hist_up_w}")

        # 4. MACD 金叉
        if hist > 0 and hist > signal:
            score += self.macd_golden_cross_w
            score_detail.append(f"MACD金叉+{self.macd_golden_cross_w}")

        # 5. RSI 超卖反弹
        if rsi < self.rsi_oversold:
            score += self.rsi_oversold_w
            score_detail.append(f"RSI超卖+{self.rsi_oversold_w}")

        # 6. KDJ 金叉
        if k is not None and d is not None and k > d and k < 50:
            score += self.kdj_golden_cross_w
            score_detail.append(f"KDJ金叉+{self.kdj_golden_cross_w}")

        # 7. 放量确认
        if self.rvol > self.rvol_threshold:
            score += self.rvol_confirm_w
            score_detail.append(f"放量+{self.rvol_confirm_w}")

        # 8. 价格接近VWAP（不在极端位置）
        vwap_dev = abs(bar.close_price - self.vwap) / self.vwap if self.vwap > 0 else 0
        if vwap_dev < self.vwap_deviation_entry:
            score += self.vwap_near_w
            score_detail.append(f"VWAP近+{self.vwap_near_w}")

        self.score = score

        # ---- 时间过滤 ----
        bar_time = bar.datetime
        in_open_window = (bar_time.hour == 9 and bar_time.minute < self.avoid_open_minutes) or \
                         (bar_time.hour == 22 and bar_time.minute < 30)  # 美股开盘前30分钟
        time_ok = not self.enable_time_filter or not in_open_window

        # ---- 日志 ----
        self.write_log(
            f"[{self.strategy_name}] Bar#{self.inited_bars} "
            f"close={bar.close_price:.2f} EMA_F={ema_fast:.2f} EMA_S={ema_slow:.2f} "
            f"MACD={hist:.4f} RSI={rsi:.1f} KDJ({k:.0f},{d:.0f}) "
            f"ATR={atr:.3f} VWAP={self.vwap:.2f} RVOL={self.rvol:.2f} "
            f"Score={score} [{'|'.join(score_detail)}]"
        )

        # ============================================================
        # 交易决策
        # ============================================================
        if self.pos == 0:
            # ---- 空仓：寻找入场机会 ----
            if time_ok and score >= self.score_threshold_long:
                # VWAP 偏离过滤：价格不能离VWAP太远
                if vwap_dev > self.vwap_deviation_entry * 3:
                    self.write_log(f"[{self.strategy_name}] 跳过：VWAP偏离过大 {vwap_dev*100:.2f}%")
                    return

                # 凯利公式计算仓位
                kelly_f = self._kelly_fraction()
                self.kelly_fraction = kelly_f
                capital = self.kelly_capital
                price = bar.close_price
                max_shares = int(capital * kelly_f / price / 100) * 100  # 整百股
                qty = max(100, min(max_shares, 1000))  # 最少100股，最多1000股

                self.write_log(
                    f"[{self.strategy_name}] 🟢 买入信号! Score={score} "
                    f"Kelly={kelly_f*100:.1f}% Qty={qty} "
                    f"RSI={rsi:.0f} RVOL={self.rvol:.2f}"
                )

                if not self.test_mode:
                    self.buy(price, qty)
                self.entry_price = price
                self.current_stop = price - atr * self.atr_stop_mult
                self.current_target = price * (1 + self.profit_activation_pct)
                self._trailing_activated = False

        else:
            # ---- 持仓：管理止盈止损 ----
            pnl_pct = (bar.close_price - self.entry_price) / self.entry_price

            # ATR 动态止损
            atr_stop = bar.close_price - atr * self.atr_stop_mult

            # 固定止损
            hard_stop = self.entry_price * (1 - self.stop_loss_pct)

            # 取较宽松的止损
            stop_price = max(self.current_stop, atr_stop, hard_stop)

            # 移动止盈
            if pnl_pct >= self.profit_activation_pct:
                self._trailing_activated = True
                trail_target = bar.close_price * (1 - self.profit_rollback_pct)
                self.current_stop = max(self.current_stop, trail_target)
                self.write_log(
                    f"[{self.strategy_name}] 🔄 移动止盈激活 "
                    f"新止损={self.current_stop:.2f} 当前={bar.close_price:.2f} "
                    f"盈利={pnl_pct*100:.2f}%"
                )

            # 止损检查
            if bar.close_price <= stop_price:
                pnl = (bar.close_price - self.entry_price) * abs(self.pos)
                self.write_log(
                    f"[{self.strategy_name}] 🔴 止损 @ {bar.close_price:.2f} "
                    f"止损价={stop_price:.2f} 盈亏={pnl:.0f} "
                    f"类型={'ATR' if stop_price == atr_stop else '固定'}"
                )
                if not self.test_mode:
                    self.sell(bar.close_price, abs(self.pos))
                self._reset_state()
                return

            # 固定止盈
            if pnl_pct >= self.profit_activation_pct * 2:
                self.write_log(
                    f"[{self.strategy_name}] 🎯 止盈 @ {bar.close_price:.2f} "
                    f"盈利={pnl_pct*100:.2f}%"
                )
                if not self.test_mode:
                    self.sell(bar.close_price, abs(self.pos))
                self._reset_state()
                return

            # 信号反转平仓
            if score <= self.score_threshold_short:
                self.write_log(
                    f"[{self.strategy_name}] ⚠️ 信号反转 Score={score} 平仓"
                )
                if not self.test_mode:
                    self.sell(bar.close_price, abs(self.pos))
                self._reset_state()
                return

            # 更新当前止损（用于日志）
            self.current_stop = stop_price

    # ============================================================
    # 辅助方法
    # ============================================================
    def _calc_kdj(self):
        """计算 KDJ 指标"""
        try:
            n = self.kdj_n
            if len(self.am.close) < n:
                return None, None
            closes = self.am.close[-n:]
            highs = self.am.high[-n:]
            lows = self.am.low[-n:]
            rsv = (closes[-1] - min(lows)) / (max(highs) - min(lows)) * 100 if max(highs) > min(lows) else 50

            # 用指数移动平均模拟K和D
            if not hasattr(self, '_k_val'):
                self._k_val = 50.0
                self._d_val = 50.0

            self._k_val = 2/3 * self._k_val + 1/3 * rsv
            self._d_val = 2/3 * self._d_val + 1/3 * self._k_val
            return self._k_val, self._d_val
        except Exception:
            return None, None

    def _kelly_fraction(self) -> float:
        """凯利公式：f* = (p*b - q) / b"""
        p = self.kelly_win_rate
        b = self.kelly_win_loss_ratio
        q = 1 - p
        f_star = (p * b - q) / b if b > 0 else 0
        # 安全截断
        f_star = max(0.0, min(f_star, self.kelly_max_fraction))
        return f_star

    def _reset_state(self):
        """重置持仓状态"""
        self.pos = 0
        self.entry_price = 0.0
        self.current_stop = 0.0
        self.current_target = 0.0
        self._trailing_activated = False

    def on_trade(self, trade):
        self.put_event()

    def on_order(self, order):
        pass