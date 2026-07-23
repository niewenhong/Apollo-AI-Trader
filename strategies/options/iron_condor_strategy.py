"""
strategies/options/iron_condor_strategy.py - Apollo-AI-Trader v2.6.0
Iron Condor：卖OTM Call + 卖OTM Put + 买更OTM Call/Put保护
预期震荡，双向收权利金，风险有限
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class IronCondorStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    delta_short_call = 0.15
    delta_short_put = -0.15
    wing_width_pct = 0.10      # 保护腿宽度（行权价偏离百分比）
    min_days_to_expiry = 21    # 铁鹰式通常需要更长到期
    max_days_to_expiry = 45
    min_credit_ratio = 0.40    # 净权利金/最大风险
    rolling_days = 10
    max_positions = 2

    parameters = [
        "delta_short_call", "delta_short_put", "wing_width_pct",
        "min_days_to_expiry", "max_days_to_expiry", "min_credit_ratio",
        "rolling_days", "max_positions",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl", "legs"]

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs and len(self.legs) >= 4:
            for leg in self.legs.values():
                if leg.get("days_to_expiry",999) <= self.rolling_days:
                    self._roll_positions(); return
        if len(self.legs) < 4:
            self._find_condor(bar)

    def _find_condor(self, bar: BarData):
        """寻找Iron Condor组合（4条腿）"""
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        if not chain: return
        calls = [c for c in chain if c.get("is_call", True)]
        puts = [c for c in chain if c.get("is_put", True)]
        # 简化：选Delta最接近目标的腿
        sc = self._find_nearest_delta(calls, self.delta_short_call, "call")
        sp = self._find_nearest_delta(puts, self.delta_short_put, "put")
        if not sc or not sp: return
        # 保护腿
        wing_call = self._find_wing(sc, calls, "call", self.wing_width_pct)
        wing_put = self._find_wing(sp, puts, "put", self.wing_width_pct)
        if not wing_call or not wing_put: return
        # 计算净权利金
        net = sc.get("premium",0) + sp.get("premium",0) \
              - wing_call.get("premium",0) - wing_put.get("premium",0)
        if net <= 0: return
        # 开仓4条腿
        sc["name"]="ic_sc"; sc["is_long"]=False
        sp["name"]="ic_sp"; sp["is_long"]=False
        wing_call["name"]="ic_wc"; wing_call["is_long"]=True
        wing_put["name"]="ic_wp"; wing_put["is_long"]=True
        for leg in [sc, sp, wing_call, wing_put]:
            self._send_option_order(leg,
                Direction.SHORT if not leg["is_long"] else Direction.LONG,
                Offset.OPEN)
        self.net_premium = net
        # 最大风险 = 翼宽 - 净权利金
        wing_width = min(
            abs(sc["strike_price"]-wing_call["strike_price"]),
            abs(sp["strike_price"]-wing_put["strike_price"]))
        self.max_loss = wing_width - net
        self.max_profit = net
        self.write_log(f"[IronCondor] ✅ 开仓 net_premium={net:.1f} max_loss={self.max_loss:.1f}")

    def _find_nearest_delta(self, chain, target_delta, opt_type):
        best = None; best_diff = 999
        for c in chain:
            d = abs(abs(c.get("delta",0))-abs(target_delta))
            if d < best_diff: best_diff=d; best=c
        return best

    def _find_wing(self, anchor, chain, opt_type, width_pct):
        """找保护腿：比anchor更OTM"""
        anchor_strike = anchor.get("strike_price",0)
        best = None; best_diff = 999
        for c in chain:
            if c.get("code") == anchor.get("code"): continue
            if opt_type == "call":
                if c.get("strike_price",0) <= anchor_strike: continue
                diff = abs(c["strike_price"]-anchor_strike-anchor_strike*width_pct)
            else:
                if c.get("strike_price",0) >= anchor_strike: continue
                diff = abs(anchor_strike-c["strike_price"]-anchor_strike*width_pct)
            if diff < best_diff: best_diff=diff; best=c
        return best

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
