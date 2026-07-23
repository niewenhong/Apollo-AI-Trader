"""
strategies/options/straddle_strategy.py - Apollo-AI-Trader v2.6.0
Straddle：同时买入平值Call+Put，赌大波动（事件驱动）
⚠️ 高难度策略：双份权利金损耗，仅适合重大事件前（财报/议息/FOMC）
📦 当前状态：实验占位，待事件检测模块完成后启用
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class StraddleStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    atm_offset_pct = 0.02        # 平值附近偏移
    min_days_to_expiry = 7
    max_days_to_expiry = 30
    min_iv_percentile = 20       # IV百分位低于此值才考虑（便宜时买）
    event_types = ["earnings", "fomc", "cpi", "nfp"]
    profit_target = 2.0          # 盈利目标：权利金的2倍
    stop_loss_pct = 0.5         # 止损：权利金的50%
    max_positions = 1

    parameters = [
        "atm_offset_pct", "min_days_to_expiry", "max_days_to_expiry",
        "min_iv_percentile", "profit_target", "stop_loss_pct",
        "max_positions",
    ]
    variables = ["total_cost", "current_value", "pnl", "legs", "event_detected"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.total_cost = 0.0
        self.current_value = 0.0
        self.event_detected = False

    def on_bar(self, bar: BarData):
        """事件驱动：检测到重大事件临近时才建仓"""
        # TODO: 接入事件日历模块（财报日期/FOMC日程）
        # 当前为占位实现
        if not self.event_detected:
            self._check_upcoming_event()
        if self.event_detected and not self.legs:
            self._find_straddle(bar)
        elif self.legs:
            self._manage_position(bar)

    def _check_upcoming_event(self):
        """检查是否有重大事件在7天内（占位：需接入事件API）"""
        # TODO: 实现事件检测
        self.event_detected = False

    def _find_straddle(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        if not chain: return
        # 找ATM Call和Put
        spot = bar.close_price
        calls = [c for c in chain if c.get("is_call",True)]
        puts = [c for c in chain if c.get("is_put",True)]
        atm_call = min(calls, key=lambda c: abs(c.get("strike_price",0)-spot), default=None)
        atm_put = min(puts, key=lambda p: abs(p.get("strike_price",0)-spot), default=None)
        if not atm_call or not atm_put: return
        # 买入双腿
        atm_call["name"]="std_call"; atm_call["is_long"]=True
        atm_put["name"]="std_put"; atm_put["is_long"]=True
        ok1 = self._send_option_order(atm_call, Direction.LONG, Offset.OPEN)
        ok2 = self._send_option_order(atm_put, Direction.LONG, Offset.OPEN)
        if ok1 and ok2:
            self.total_cost = atm_call.get("premium",0) + atm_put.get("premium",0)
            self.write_log(f"[Straddle] ✅ 买入双腿 cost={self.total_cost:.1f}")

    def _manage_position(self, bar: BarData):
        """管理持仓：达到盈利目标或止损"""
        if self.current_value >= self.total_cost * self.profit_target:
            self._close_all_legs()
            self.write_log("[Straddle] 达到盈利目标，平仓")
        elif self.current_value <= self.total_cost * self.stop_loss_pct:
            self._close_all_legs()
            self.write_log("[Straddle] 止损平仓")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
