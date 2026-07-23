"""
strategies/equity/vwap_strategy.py - Apollo-AI-Trader v2.6.0
VWAP均值回归策略 - 双模式：
  Mode A: 用1分钟K线近似VWAP（不消耗额外订阅额度）
  Mode B: 用实时TICKER逐笔数据精确计算VWAP（需订阅TICKER）
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
import numpy as np


class VWAPStrategy(CtaTemplate):
    author = "Apollo"
    version = "v2.6.0"

    mode = "A"                 # A=K线近似, B=Tick精确
    vwap_period_min = 30       # VWAP计算周期（分钟）
    deviation_pct = 0.003     # 偏离VWAP触发阈值
    fixed_size = 100
    stop_loss_pct = 0.015
    take_profit_pct = 0.015
    use_tick = False           # 是否订阅TICKER

    parameters = ["mode","vwap_period_min","deviation_pct","fixed_size",
                  "stop_loss_pct","take_profit_pct","use_tick"]
    variables = ["pos","vwap","cum_pv","cum_vol","tick_count"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.vwap = 0.0
        self.cum_pv = 0.0      # 累计price*volume
        self.cum_vol = 0
        self.tick_count = 0
        self._bar_pv = []      # 存储近期Bar的(pv,vol)
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log(f"[VWAP] on_init mode={self.mode} use_tick={self.use_tick}")

    def on_start(self):
        self.write_log(f"[VWAP] start {self.vt_symbol}")
        # 订阅Tick（如果启用）
        if self.use_tick:
            try:
                gw = self.cta_engine.main_engine.get_gateway("FUTU_US") or \
                      self.cta_engine.main_engine.get_gateway("FUTU_HK")
                if gw and hasattr(gw, "subscribe_tick"):
                    code = self._to_futu_code()
                    gw.subscribe_tick(code)
                    self.write_log(f"[VWAP] ✅ 订阅Tick: {code}")
            except Exception as e:
                self.write_log(f"[VWAP] Tick订阅失败: {e}")

    def on_stop(self):
        self.write_log(f"[VWAP] stop")
        self._reset_day()

    def on_tick(self, tick: TickData):
        """Mode B: 用Tick精确计算VWAP"""
        if not self.use_tick: return
        if tick.volume > 0:
            self.cum_pv += tick.price * tick.volume
            self.cum_vol += tick.volume
            self.vwap = self.cum_pv / self.cum_vol if self.cum_vol > 0 else tick.price
            self.tick_count += 1
            self._check_signal(tick.price)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited: return
        if self.use_tick:
            # Mode B: Bar只做止损止盈检查
            self._check_exit(bar.close_price)
            return
        # Mode A: 用K线近似VWAP
        pv = bar.close_price * bar.volume
        self._bar_pv.append((pv, bar.volume))
        # 只保留最近N分钟
        max_bars = self.vwap_period_min
        if len(self._bar_pv) > max_bars:
            self._bar_pv = self._bar_pv[-max_bars:]
        total_pv = sum(x[0] for x in self._bar_pv)
        total_vol = sum(x[1] for x in self._bar_pv)
        self.vwap = total_pv / total_vol if total_vol > 0 else bar.close_price
        self._check_signal(bar.close_price)
        self._check_exit(bar.close_price)

    def _check_signal(self, price):
        """检查VWAP偏离信号"""
        if self.vwap <= 0 or self.pos != 0: return
        dev = (price - self.vwap) / self.vwap
        if dev < -self.deviation_pct:
            self.buy(price + 0.01, self.fixed_size)
            self.write_log(f"[VWAP] 买入: 价{price:.2f} VWAP{self.vwap:.2f} 偏离{dev*100:.2f}%")
        elif dev > self.deviation_pct:
            self.short(price - 0.01, self.fixed_size)
            self.write_log(f"[VWAP] 做空: 价{price:.2f} VWAP{self.vwap:.2f} 偏离{dev*100:.2f}%")

    def _check_exit(self, price):
        if self.pos > 0:
            if price >= self.vwap * (1 + self.take_profit_pct):
                self.sell(price-0.01, abs(self.pos))
            elif price <= self.vwap * (1 - self.stop_loss_pct):
                self.sell(price-0.01, abs(self.pos))
        elif self.pos < 0:
            if price <= self.vwap * (1 - self.take_profit_pct):
                self.cover(price+0.01, abs(self.pos))
            elif price >= self.vwap * (1 + self.stop_loss_pct):
                self.cover(price+0.01, abs(self.pos))

    def on_order(self, order):
        if order.traded > 0:
            self.pos += order.traded if order.direction.name=="LONG" else -order.traded
        self.put_event()

    def on_new_day(self):
        self._reset_day()

    def _reset_day(self):
        self.cum_pv = 0.0; self.cum_vol = 0; self.vwap = 0.0
        self.tick_count = 0; self._bar_pv.clear()

    def _to_futu_code(self):
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
