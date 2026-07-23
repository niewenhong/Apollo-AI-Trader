"""
strategies/options/covered_call_strategy.py - Apollo-AI-Trader v2.6.0
Covered Call：持股 + 卖Call（持股收租金增强收益）
前提：必须先持有正股，才能卖对应数量的Call
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CoveredCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    target_delta = 0.20
    min_otm_prob = 0.70         # Covered Call 要求更高OTM（更不容易被行权）
    min_days_to_expiry = 14
    max_days_to_expiry = 45
    min_annual_roi = 0.20
    position_size = 1
    max_positions = 5
    underlying_shares_per_contract = 100  # 每张期权对应正股数
    roll_when_ditm = 0.35

    parameters = [
        "target_delta", "min_otm_prob", "min_days_to_expiry",
        "max_days_to_expiry", "min_annual_roi", "position_size",
        "max_positions", "underlying_shares_per_contract", "roll_when_ditm",
    ]
    variables = ["net_premium", "pnl", "legs", "stock_position"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.stock_position = 0  # 持有的正股数量

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        # 检查正股持仓是否足够
        self._sync_stock_position()
        if self.stock_position < self.underlying_shares_per_contract:
            return  # 持仓不足，不开Covered Call
        # 展期
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta",0)) > self.roll_when_ditm:
                    self._roll_positions(); return
        # 开仓
        if not self.legs:
            self._find_call_to_sell(bar)

    def _sync_stock_position(self):
        """从账户同步正股持仓"""
        # 通过cta_engine查询持仓
        try:
            pos_manager = getattr(self.cta_engine.main_engine, "position_manager", None)
            if pos_manager:
                pos = pos_manager.get_position(self.vt_symbol)
                self.stock_position = pos.volume if pos else 0
        except: pass

    def _find_call_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        calls = self._select_contracts(chain, "call")
        if not calls: return
        calls.sort(key=lambda x: (-x.get("otm_prob",0), x.get("annual_roi",0)))
        target = calls[0]
        target["name"] = "covered_call"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.write_log(f"[CovCall] ✅ 卖出Covered Call {target['code']} "
                          f"premium={target.get('premium')}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
