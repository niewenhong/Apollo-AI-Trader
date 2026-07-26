"""
strategies/equity/multi_indicator_strategy.py - v2.8.0
多指标综合评分策略：融合均线、RSI、MACD、布林带、ATR、成交量等10维指标
v2.8.0 优化：
  - 修复 _reset_position 导致的仓位不同步（vnpy 框架自动管理 pos）
  - 性能优化：减少重复计算、使用局部变量
  - 健壮性：除零保护、数组边界检查
  - 支持 vnpy_ctastrategy.CtaTemplate 完整生命周期
  - AI 参数热加载、版本上报
"""
from typing import Dict, Any, Optional
import numpy as np
import time

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset

try:
    from core.db_manager import CustomDBManager
    HAS_DB = True
except ImportError:
    HAS_DB = False


# ========== 评分权重常量 ==========
W_MA_CROSS      = 15.0   # 均线金叉/死叉
W_PRICE_REL     = 10.0   # 价格相对均线位置
W_RSI           = 15.0   # RSI 超买超卖
W_ATR           = 5.0    # ATR 波动率
W_VOLUME        = 10.0   # 成交量放大
W_ADX           = 10.0   # 趋势方向强度
W_RANGE        = 10.0   # 波动率收缩/扩张
W_MACD          = 10.0   # MACD 柱状图方向
W_BOLL          = 10.0   # 布林带位置
W_CONSECUTIVE   = 5.0    # 连续涨跌

# ========== 默认参数 ==========
DEFAULT_PARAMS: Dict[str, Any] = {
    "ma_fast": 5,
    "ma_slow": 20,
    "rsi_period": 14,
    "rsi_overbought": 75,
    "rsi_oversold": 30,
    "atr_period": 14,
    "atr_multiplier": 2.0,
    "fixed_size": 100,
    "score_threshold_long": 65,
    "score_threshold_short": 35,
    "vwap_deviation_entry": 0.003,
    "rvol_threshold": 1.5,
    "kelly_max_fraction": 0.06,
    "profit_activation_pct": 0.008,
    "trailing_stop_pct": 0.005,
    "use_long_only": False,       # 是否仅做多
    "max_positions": 1,           # 最大持仓数
}


class MultiIndicatorStrategy(CtaTemplate):
    """10维共振多指标策略（vnpy CtaTemplate 标准实现）"""

    author = "Apollo"

    parameters = list(DEFAULT_PARAMS.keys())
    variables = [
        "pos", "entry_price", "score",
        "ma_fast_val", "ma_slow_val",
        "rsi_val", "atr_val", "pnl",
        "current_stop", "current_target",
        "_trailing_activated",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 合并默认参数与传入设置
        merged = {**DEFAULT_PARAMS, **setting}
        for key, value in merged.items():
            setattr(self, key, value)

        # 运行时状态（不持久化到 variables 的私有变量）
        self._trailing_activated = False
        self._param_version = setting.get("_version", 1)

        # K线工具
        self.bg_1m = self.create_bar_generator(1, self.on_1m_bar)
        self.am_1m = self.create_array_manager(100)
        self.bg_5m = self.create_bar_generator(5, self.on_5m_bar)
        self.am_5m = self.create_array_manager(100)

        # AI 参数加载
        self._load_ai_params()

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | v{self._param_version}")

    # ──────────────────────────────
    #  辅助方法
    # ──────────────────────────────
    def create_bar_generator(self, window: int, callback):
        """兼容不同 vnpy 版本的 BarGenerator 创建"""
        from vnpy.trader.utility import BarGenerator
        return BarGenerator(self.on_bar, window, callback)

    def create_array_manager(self, size: int):
        """兼容不同 vnpy 版本的 ArrayManager 创建"""
        from vnpy.trader.utility import ArrayManager
        return ArrayManager(size)

    def _load_ai_params(self):
        """从数据库加载 AI 优化参数"""
        if not HAS_DB:
            return
        try:
            db = CustomDBManager()
            ai_params = db.get_latest_params(self.vt_symbol, self.__class__.__name__)
            if ai_params:
                for key, value in ai_params.items():
                    if hasattr(self, key) and key in DEFAULT_PARAMS:
                        setattr(self, key, value)
                self.write_log(f"已加载AI优化参数: {list(ai_params.keys())}")
        except Exception as e:
            self.write_log(f"加载AI参数失败（使用默认）: {e}")

    # ──────────────────────────────
    #  生命周期
    # ──────────────────────────────
    def on_init(self):
        """策略初始化：加载历史K线"""
        self.load_bar(30, use_database=True)
        self.write_log(f"✅ 初始化完成 (v{self._param_version})")

    def on_start(self):
        """策略启动"""
        self._trailing_activated = False
        self.write_log(f"▶️ 策略启动 | 阈值: 多≥{self.score_threshold_long} 空≤{self.score_threshold_short}")

    def on_stop(self):
        """策略停止"""
        self.write_log(f"⏸ 策略停止 | pos={self.pos} pnl={self.pnl:.2f}")

    # ──────────────────────────────
    #  Tick / Bar 分发
    # ──────────────────────────────
    def on_tick(self, tick: TickData):
        self.bg_1m.update_tick(tick)
        self.bg_5m.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg_1m.update_bar(bar)
        self.bg_5m.update_bar(bar)

    # ──────────────────────────────
    #  1分钟K线：核心交易逻辑
    # ──────────────────────────────
    def on_1m_bar(self, bar: BarData):
        am = self.am_1m
        am.update_bar(bar)
        if not am.inited:
            return

        close = bar.close_price

        # 一次性计算所有指标（避免重复调用）
        self.ma_fast_val = am.sma(self.ma_fast, array=False)
        self.ma_slow_val = am.sma(self.ma_slow, array=False)
        self.rsi_val = am.rsi(self.rsi_period, array=False)
        self.atr_val = am.atr(self.atr_period, array=False)

        # 综合评分
        self.score = self._calc_score(close, am)

        # ── 交易决策 ──
        if self.pos == 0:
            self._try_open(close)
        else:
            self._manage_position(close)

    def on_5m_bar(self, bar: BarData):
        """5分钟K线（备用，可用于更长周期确认）"""
        self.am_5m.update_bar(bar)

    # ──────────────────────────────
    #  开仓逻辑
    # ──────────────────────────────
    def _try_open(self, close: float):
        """根据评分尝试开仓"""
        if self.score >= self.score_threshold_long:
            self.buy(close, self.fixed_size)
            self.entry_price = close
            self.current_stop = close - self.atr_val * self.atr_multiplier
            self.current_target = close * (1 + self.profit_activation_pct * 2)
            self._trailing_activated = False
            self.write_log(
                f"🟢 多头开仓: score={self.score:.0f} "
                f"price={close:.2f} qty={self.fixed_size}"
            )

        elif not self.use_long_only and self.score <= self.score_threshold_short:
            self.short(close, self.fixed_size)
            self.entry_price = close
            self.current_stop = close + self.atr_val * self.atr_multiplier
            self.write_log(
                f"🔴 空头开仓: score={self.score:.0f} "
                f"price={close:.2f} qty={self.fixed_size}"
            )

    # ──────────────────────────────
    #  持仓管理
    # ──────────────────────────────
    def _manage_position(self, close: float):
        """
        止盈/止损/跟踪止损/评分反转
        注意：self.pos 由 vnpy 框架自动维护，此处不手动修改
        """
        atr = self.atr_val
        entry = self.entry_price

        # 更新浮动盈亏
        if self.pos > 0:
            self.pnl = (close - entry) * self.pos
        else:
            self.pnl = (entry - close) * abs(self.pos)

        # ── 多头逻辑 ──
        if self.pos > 0:
            # 激活跟踪止损
            if not self._trailing_activated:
                pnl_pct = ((close - entry) / entry) if entry > 0 else 0
                if pnl_pct >= self.profit_activation_pct:
                    self._trailing_activated = True
                    self.current_stop = close * (1 - self.trailing_stop_pct)
                    self.write_log(f"🎯 跟踪止损激活 @ {self.current_stop:.2f}")

            # 跟踪止损上移
            if self._trailing_activated:
                new_stop = close * (1 - self.trailing_stop_pct)
                if new_stop > self.current_stop:
                    self.current_stop = new_stop

            # 1) 跟踪止损触发
            if self._trailing_activated and close <= self.current_stop:
                self.sell(close, abs(self.pos))
                self.write_log(f"🛡️ 跟踪止损: @ {close:.2f} PnL={self.pnl:.2f}")
                return

            # 2) ATR 硬止损
            if close <= entry - atr * self.atr_multiplier:
                self.sell(close, abs(self.pos))
                self.write_log(f"🛡️ ATR止损: @ {close:.2f} PnL={self.pnl:.2f}")
                return

            # 3) 固定止盈
            if close >= self.current_target:
                self.sell(close, abs(self.pos))
                self.write_log(f"🎯 止盈: @ {close:.2f} PnL={self.pnl:.2f}")
                return

            # 4) 评分反转
            if self.score <= self.score_threshold_short:
                self.sell(close, abs(self.pos))
                self.write_log(f"🔁 评分反转平仓: score={self.score:.0f}")
                return

        # ── 空头逻辑 ──
        elif self.pos < 0:
            # ATR 止损
            if close >= entry + atr * self.atr_multiplier:
                self.cover(close, abs(self.pos))
                self.write_log(f"🛡️ ATR止损(空): @ {close:.2f} PnL={self.pnl:.2f}")
                return

            # 评分反转
            if self.score >= self.score_threshold_long:
                self.cover(close, abs(self.pos))
                self.write_log(f"🔁 评分反转平仓(空): score={self.score:.0f}")
                return

    # ──────────────────────────────
    #  10维评分
    # ──────────────────────────────
    def _calc_score(self, close: float, am) -> float:
        """综合评分 (0~100)"""
        score = 50.0

        # 1. 均线关系
        if self.ma_fast_val > self.ma_slow_val:
            score += W_MA_CROSS
        else:
            score -= W_MA_CROSS

        # 2. 价格相对均线
        if close > self.ma_fast_val:
            score += W_PRICE_REL
        elif close < self.ma_slow_val:
            score -= W_PRICE_REL

        # 3. RSI
        if self.rsi_val < self.rsi_oversold:
            score += W_RSI
        elif self.rsi_val > self.rsi_overbought:
            score -= W_RSI

        # 4. ATR 波动率
        if close > 0:
            atr_ratio = self.atr_val / close
            if atr_ratio < 0.02:
                score += W_ATR
            elif atr_ratio > 0.05:
                score -= W_ATR

        # 5. 成交量
        vol_arr = am.volume
        if len(vol_arr) >= 20:
            vol_ma = float(np.mean(vol_arr[-20:]))
            if vol_ma > 0:
                ratio = vol_arr[-1] / vol_ma
                if ratio > 1.5:
                    score += W_VOLUME
                elif ratio < 0.5:
                    score -= W_VOLUME * 0.5

        # 6. ADX 趋势强度
        if am.inited and len(am.close) >= 14:
            plus_di = am.plus_di(self.atr_period, array=False)
            minus_di = am.minus_di(self.atr_period, array=False)
            if plus_di > minus_di:
                score += W_ADX
            else:
                score -= W_ADX

        # 7. 波动率收缩
        high_arr = am.high
        low_arr = am.low
        if len(high_arr) >= 10 and len(low_arr) >= 10:
            recent_h = float(np.max(high_arr[-10:]))
            recent_l = float(np.min(low_arr[-10:]))
            if recent_l > 0:
                rng = (recent_h - recent_l) / recent_l
                if rng < 0.05:
                    score += W_RANGE
                elif rng > 0.15:
                    score -= W_RANGE * 0.5

        # 8. MACD
        if am.inited and len(am.close) >= 26:
            _, _, hist = am.macd(12, 26, 9, array=True)
            if len(hist) > 1:
                if hist[-1] > 0 and hist[-1] > hist[-2]:
                    score += W_MACD
                elif hist[-1] < 0 and hist[-1] < hist[-2]:
                    score -= W_MACD

        # 9. 布林带
        if am.inited and len(am.close) >= 20:
            bb_up, _, bb_low = am.bollinger(20, 2, array=True)
            if len(bb_up) > 0 and len(bb_low) > 0:
                if close > bb_up[-1]:
                    score -= W_BOLL
                elif close < bb_low[-1]:
                    score += W_BOLL

        # 10. 连续涨跌
        close_arr = am.close
        if len(close_arr) >= 3:
            c1, c2, c3 = close_arr[-3], close_arr[-2], close_arr[-1]
            if c2 > c1 and c3 > c2:
                score += W_CONSECUTIVE
            elif c2 < c1 and c3 < c2:
                score -= W_CONSECUTIVE

        return max(0.0, min(100.0, score))

    # ──────────────────────────────
    #  成交回调
    # ──────────────────────────────
    def on_trade(self, trade):
        self.write_log(
            f"💰 成交: {trade.direction.name} {trade.volume}手 @ {trade.price:.2f}"
        )
        # pos 由 vnpy 框架自动更新，无需手动修改
        self.put_event()
