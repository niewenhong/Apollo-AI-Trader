"""
strategies/options/bear_put_spread_strategy.py - Apollo-AI-Trader v2.6.0
Bear Put Spread：买高行权价Put + 卖低行权价Put
温和看跌，风险有限
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class BearPutSpreadStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    delta_long = -0.35          # 买入腿Delta（负值）
    delta_short = -0.15         # 卖出腿Delta（负值，更接近ATM）
    min_days_to_expiry = 14
    max_days_to_expiry = 45
    min_credit_ratio = 0.30
    rolling_days = 7
    max_positions = 3

    parameters = [
        "delta_long", "delta_short", "min_days_to_expiry",
        "max_days_to_expiry", "min_credit_ratio", "rolling_days",
        "max_positions",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl", "legs"]

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs and len(self.legs) >= 2:
            for leg in self.legs.values():
                if leg.get("days_to_expiry",999) <= self.rolling_days:
                    self._roll_positions(); return
        if len(self.legs) < 2:
            self._find_spread(bar)

    def _find_spread(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        if not chain: return
        puts = [c for c in chain if c.get("is_put", True)]
        if len(puts) < 2: return
        # long: 高行权价(Delta更负), short: 低行权价(Delta更接近0)
        long_cands = [p for p in puts if abs(abs(p.get("delta",0))-abs(self.delta_long))<=0.1]
        short_cands = [p for p in puts if abs(abs(p.get("delta",0))-abs(self.delta_short))<=0.1]
        if not long_cands or not short_cands: return
        best = None
        for lc in long_cands:
            for sc in short_cands:
                if sc.get("strike_price",0) >= lc.get("strike_price",0): continue
                net = lc.get("premium",0) - sc.get("premium",0)
                if net > 0 and net < (lc["strike_price"]-sc["strike_price"]):
                    if best is None or net < best[2]:
                        best = (lc, sc, net, lc["strike_price"]-sc["strike_price"])
        if best:
            long_leg, short_leg, net, width = best
            long_leg["name"] = "bps_long"; long_leg["is_long"] = True
            short_leg["name"] = "bps_short"; short_leg["is_long"] = False
            ok = self._open_spread(long_leg, short_leg)
            if ok:
                self.max_loss = net
                self.max_profit = width - net
                self.write_log(f"[BPS] ✅ 开仓 long@{long_leg['strike_price']} "
                              f"short@{short_leg['strike_price']} net={net:.1f}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
