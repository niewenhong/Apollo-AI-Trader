"""
strategies/options/bull_call_spread_strategy.py - Apollo-AI-Trader v2.6.0
Bull Call Spread：买低行权价Call + 卖高行权价Call
温和看涨，风险有限（最大亏损=净权利金），收益有限
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class BullCallSpreadStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    delta_long = 0.35           # 买入腿Delta
    delta_short = 0.15          # 卖出腿Delta
    min_days_to_expiry = 14
    max_days_to_expiry = 45
    min_credit_ratio = 0.30     # 卖出腿权利金/买入腿权利金最低比
    rolling_days = 7            # 剩余天数<此值时展期
    max_positions = 3

    parameters = [
        "delta_long", "delta_short", "min_days_to_expiry",
        "max_days_to_expiry", "min_credit_ratio", "rolling_days",
        "max_positions",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl", "legs"]

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        # 展期
        if self.legs and len(self.legs) >= 2:
            for leg in self.legs.values():
                days = leg.get("days_to_expiry", 999)
                if days <= self.rolling_days:
                    self.write_log("[BCS] 临近到期，展期")
                    self._roll_positions(); return
        # 开仓
        if len(self.legs) < 2:
            self._find_spread(bar)

    def _find_spread(self, bar: BarData):
        """寻找Bull Call Spread组合"""
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        if not chain: return
        # 分离Call
        calls = [c for c in chain if c.get("is_call", True)]
        if len(calls) < 2: return
        # 按Delta筛选
        long_candidates = [c for c in calls if abs(c.get("delta",0)-self.delta_long)<=0.1]
        short_candidates = [c for c in calls if abs(c.get("delta",0)-self.delta_short)<=0.1]
        if not long_candidates or not short_candidates: return
        # 选最优组合
        best = None
        for lc in long_candidates:
            for sc in short_candidates:
                if sc.get("strike_price",0) <= lc.get("strike_price",0): continue
                net = lc.get("premium",0) - sc.get("premium",0)
                ratio = sc.get("premium",0) / (lc.get("premium",0)+1e-6)
                if ratio >= self.min_credit_ratio:
                    spread_width = sc["strike_price"] - lc["strike_price"]
                    if best is None or net < best[2]:  # 选净成本最低的
                        best = (lc, sc, net, spread_width)
        if best:
            long_leg, short_leg, net, width = best
            long_leg["name"] = "bcs_long"; long_leg["is_long"] = True
            short_leg["name"] = "bcs_short"; short_leg["is_long"] = False
            ok = self._open_spread(long_leg, short_leg)
            if ok:
                self.max_loss = net
                self.max_profit = width - net
                self.write_log(f"[BCS] ✅ 开仓 long@{long_leg['strike_price']} "
                              f"short@{short_leg['strike_price']} net={net:.1f}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
