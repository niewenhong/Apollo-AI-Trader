"""
strategies/equity/multi_indicator_strategy.py - v2.6.0
多指标综合评分策略：融合均线、RSI、MACD、布林带、ATR、成交量等10维指标
根据综合评分决定买卖方向，支持从数据库读取AI优化的参数
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset, Interval
import numpy as np
import json
from pathlib import Path


class MultiIndicatorStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "ma_fast",           # 快线周期
        "ma_slow",           # 慢线周期
        "rsi_period",        # RSI周期
        "rsi_overbought",    # RSI超买阈值
        "rsi_oversold",      # RSI超卖阈值
        "atr_period",        # ATR周期
        "atr_multiplier",    # ATR倍数（止损/止盈）
        "fixed_size",        # 固定交易数量
    ]

    variables = [
        "pos", "entry_price", "score", "ma_fast_val", "ma_slow_val",
        "rsi_val", "atr_val", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.score = 0.0
        self.ma_fast_val = 0.0
        self.ma_slow_val = 0.0
        self.rsi_val = 50.0
        self.atr_val = 0.0
        self.pnl = 0.0

        self.bg_1min = BarGenerator(self.on_bar, 1, self.on_1min_bar)
        self.am_1min = ArrayManager(size=100)
        self.bg_5min = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am_5min = ArrayManager(size=100)

        # 尝试从数据库加载AI优化参数
        self._load_ai_params()

    def _load_ai_params(self):
        """从数据库加载AI建议的参数（如果存在）"""
        try:
            from core.db_manager import CustomDBManager
            db = CustomDBManager()
            vt_symbol = self.vt_symbol
            strategy_class = self.__class__.__name__
            ai_params = db.get_latest_params(vt_symbol, strategy_class)
            if ai_params:
                for key, value in ai_params.items():
                    if key in self.parameters:
                        setattr(self, key, value)
                self.write_log(f"已加载AI优化参数: {ai_params}")
        except Exception as e:
            self.write_log(f"加载AI参数失败（使用默认）: {e}")

    def on_init(self):
        self.load_bar(30, use_database=True)
        self.write_log("MultiIndicator策略初始化完成")

    def on_start(self):
        self.write_log("MultiIndicator策略启动")

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

        # 计算综合评分（0-100）
        self.score = self._calculate_score(bar)

        # 交易逻辑
        if self.pos == 0:
            if self.score > 65:
                self.buy(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.write_log(f"多头开仓: 评分{self.score:.1f}")
            elif self.score < 35:
                self.short(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.write_log(f"空头开仓: 评分{self.score:.1f}")
        else:
            self._manage_position(bar)

    def on_5min_bar(self, bar: BarData):
        """5分钟K线回调，用于趋势确认"""
        self.am_5min.update_bar(bar)

    def _calculate_score(self, bar: BarData) -> float:
        """10维指标综合评分"""
        score = 50.0  # 基准分

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
        elif 40 < self.rsi_val < 60:
            score += 5

        # 4. MACD（权重10）
        macd, signal, hist = self.am_1min.macd(
            self.ma_fast, self.ma_slow, 9, array=False
        )
        if hist > 0:
            score += 10
        elif hist < 0:
            score -= 10

        # 5. 布林带位置（权重10）
        upper, middle, lower = self.am_1min.boll(
            self.ma_slow, 2, array=False
        )
        if bar.close_price < lower:
            score += 10
        elif bar.close_price > upper:
            score -= 10

        # 6. ATR波动率（权重5）
        atr_ratio = self.atr_val / bar.close_price
        if atr_ratio < 0.02:
            score += 5  # 低波动，趋势延续概率大
        elif atr_ratio > 0.05:
            score -= 5  # 高波动，风险加大

        # 7. 成交量变化（权重10）
        vol_ma = np.mean(self.am_1min.volume[-20:])
        if self.am_1min.volume[-1] > vol_ma * 1.5:
            score += 10  # 放量配合
        elif self.am_1min.volume[-1] < vol_ma * 0.5:
            score -= 5   # 缩量

        # 8. 价格动量（权重5）
        mom = bar.close_price - self.am_1min.close[-5]
        if mom > 0:
            score += 5
        else:
            score -= 5

        # 9. 趋势强度（ADX简化，权重10）
        plus_di = self.am_1min.plus_di(self.atr_period, array=False)
        minus_di = self.am_1min.minus_di(self.atr_period, array=False)
        if plus_di > minus_di:
            score += 10
        else:
            score -= 10

        # 10. 波动率收缩（权重10）
        recent_high = np.max(self.am_1min.high[-10:])
        recent_low = np.min(self.am_1min.low[-10:])
        range_pct = (recent_high - recent_low) / recent_low
        if range_pct < 0.05:
            score += 10  # 收缩后可能爆发
        elif range_pct > 0.15:
            score -= 5

        return max(0, min(100, score))

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、反向平仓"""
        # 计算浮动盈亏
        if self.pos > 0:
            self.pnl = (bar.close_price - self.entry_price) * self.pos
        else:
            self.pnl = (self.entry_price - bar.close_price) * abs(self.pos)

        # 评分反转平仓
        if self.pos > 0 and self.score < 35:
            self.sell(bar.close_price, abs(self.pos))
            self.write_log(f"多头平仓: 评分降至{self.score:.1f}")
        elif self.pos < 0 and self.score > 65:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"空头平仓: 评分升至{self.score:.1f}")

        # ATR止损
        atr_stop = self.atr_val * self.atr_multiplier
        if self.pos > 0:
            if bar.close_price < self.entry_price - atr_stop:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("ATR止损: 多头")
        elif self.pos < 0:
            if bar.close_price > self.entry_price + atr_stop:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log("ATR止损: 空头")

    def on_trade(self, trade):
        self.write_log(f"成交: {trade.direction.name} {trade.volume}手 @ {trade.price}")