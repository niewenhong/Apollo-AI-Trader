"""
strategies/options/sell_call_strategy.py - Apollo-AI-Trader v2.6.0
Sell Call：卖Call收权利金（看不涨/持股增强）
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class SellCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    target_delta = 0.20
    min_otm_prob = 0.65
    min_days_to_expiry = 7
    max_days_to_expiry = 45
    min_annual_roi = 0.30
    position_size = 1
    max_positions = 5
    roll_when_ditm = 0.30

    parameters = [
        "target_delta", "min_otm_prob", "min_days_to_expiry",
        "max_days_to_expiry", "min_annual_roi", "position_size",
        "max_positions", "roll_when_ditm",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs"]

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs:
            for name, leg in self.legs.items():
                delta = abs(leg.get("delta", 0))
                if delta > self.roll_when_ditm:
                    self._roll_positions(); return
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_call_to_sell(bar)

    def _find_call_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        calls = self._select_contracts(chain, "call")
        if not calls:
            self.write_log("[SellCall] 无符合条件Call合约"); return
        calls.sort(key=lambda x: (-x.get("otm_prob",0), x.get("annual_roi",0)))
        target = calls[0]
        target["name"] = "sold_call"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.write_log(f"[SellCall] ✅ 卖出Call {target['code']} "
                          f"行权={target.get('strike_price')} 权利金={target.get('premium')}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
