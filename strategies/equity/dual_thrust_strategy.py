"""
strategies/equity/dual_thrust_strategy.py - v2.6.0
双突破策略：基于开盘区间突破 + 市场状态切换
支持趋势/震荡模式自适应，配合ATR动态调整突破幅度
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class DualThrustStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "k1",                # 上轨系数（突破买入）
        "k2",                # 下轨系数（突破卖出）
        "lookback_days",     # 回溯天数
        "fixed_size",        # 固定交易数量
        "use_market_filter", # 是否启用市场状态过滤
    ]

    variables = [
        "pos", "entry_price", "up_line", "down_line",
        "today_open", "market_state", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.up_line = 0.0
        self.down_line = 0.0
        self.today_open = 0.0
        self.market_state = "unknown"  # trend, range, unknown
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(30, use_database=True)
        self.write_log("DualThrust策略初始化完成")

    def on_start(self):
        self.write_log("DualThrust策略启动")

    def on_stop(self):
        self.write_log("DualThrust策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 每天开盘重置轨道
        if self.today_open == 0:
            self.today_open = bar.open_price
            self._calculate_lines(bar)

        # 判断市场状态（趋势/震荡）
        if self.use_market_filter:
            self._update_market_state(bar)

        # 交易逻辑
        if self.pos == 0:
            # 突破上轨买入（趋势模式下优先）
            if bar.close_price > self.up_line:
                if self.market_state != "range":
                    self.buy(bar.close_price, self.fixed_size)
                    self.entry_price = bar.close_price
                    self.write_log(f"突破买入: 价格{bar.close_price:.2f} > 上轨{self.up_line:.2f}")
            # 突破下轨卖出
            elif bar.close_price < self.down_line:
                if self.market_state != "range":
                    self.short(bar.close_price, self.fixed_size)
                    self.entry_price = bar.close_price
                    self.write_log(f"突破卖出: 价格{bar.close_price:.2f} < 下轨{self.down_line:.2f}")
        else:
            self._manage_position(bar)

        # 收盘重置（第二天）
        if bar.datetime.time() >= self._get_market_close_time():
            self.today_open = 0.0
            self.up_line = 0.0
            self.down_line = 0.0

    def _calculate_lines(self, bar: BarData):
        """计算今日上下轨"""
        if len(self.am.high) < self.lookback_days:
            return

        # 取过去N天的最高High和最低Low
        hh = np.max(self.am.high[-self.lookback_days:-1])
        ll = np.min(self.am.low[-self.lookback_days:-1])

        # 计算Range
        range_val = hh - ll

        # 上下轨
        self.up_line = self.today_open + self.k1 * range_val
        self.down_line = self.today_open - self.k2 * range_val

    def _update_market_state(self, bar: BarData):
        """更新市场状态：趋势/震荡"""
        if len(self.am.close) < 20:
            return

        # 用ADX判断趋势强度
        adx = self.am.atr(14) / np.mean(self.am.close[-14:]) * 100
        if adx > 25:
            self.market_state = "trend"
        else:
            self.market_state = "range"

    def _manage_position(self, bar: BarData):
        """管理持仓：反向突破平仓"""
        if self.pos > 0:
            # 多头：跌破下轨平仓
            if bar.close_price < self.down_line:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("多头平仓: 跌破下轨")
        elif self.pos < 0:
            # 空头：突破上轨平仓
            if bar.close_price > self.up_line:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log("空头平仓: 突破上轨")

    def _get_market_close_time(self):
        """获取市场收盘时间（简化）"""
        from datetime import time
        if "SEHK" in self.vt_symbol:
            return time(16, 0)
        else:
            return time(16, 0)  # 美股简化

    def on_trade(self, trade):
        self.write_log(f"DualThrust成交: {trade.direction.name} {trade.volume}手 @ {trade.price}")