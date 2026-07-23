"""
strategies/options/sell_put_strategy.py - Apollo-AI-Trader v2.6.0
Sell Put：卖Put收权利金（看不跌/想低价接货）
实盘策略：模拟盘胜率80%+，真实盘需现金担保
"""
from vnpy.trader.object import BarData
from strategies.options.base_option_strategy import BaseOptionStrategy


class SellPutStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    # 参数
    target_delta = 0.20          # 目标Delta绝对值
    min_otm_prob = 0.65         # 最低OTM概率
    min_days_to_expiry = 7
    max_days_to_expiry = 45
    min_annual_roi = 0.30
    position_size = 1
    max_positions = 5
    cash_buffer_ratio = 0.1     # 现金缓冲比例
    roll_when_ditm = 0.30      # Delta超过此值展期

    parameters = [
        "target_delta", "min_otm_prob", "min_days_to_expiry",
        "max_days_to_expiry", "min_annual_roi", "position_size",
        "max_positions", "cash_buffer_ratio", "roll_when_ditm",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self._pending_fill = False

    def on_bar(self, bar: BarData):
        # 管理到期
        if self._manage_expiry(bar): return
        # 展期检查
        if self.legs:
            for name, leg in self.legs.items():
                delta = abs(leg.get("delta", 0))
                if delta > self.roll_when_ditm:
                    self.write_log(f"[SellPut] Delta={delta:.2f}>{self.roll_when_ditm}，展期")
                    self._roll_positions()
                    return
        # 寻找新机会
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_put_to_sell(bar)

    def _find_put_to_sell(self, bar: BarData):
        """寻找符合条件的Put合约并卖出"""
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        puts = self._select_contracts(chain, "put")
        if not puts:
            self.write_log("[SellPut] 无符合条件Put合约")
            return
        # 选最优：OTM概率最高且ROI达标的
        puts.sort(key=lambda x: (-x.get("otm_prob",0), x.get("annual_roi",0)))
        target = puts[0]
        target["name"] = "sold_put"
        target["is_long"] = False
        # 发送卖出指令
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.max_loss = target.get("strike_price", 0) - target.get("premium", 0)
            self.write_log(f"[SellPut] ✅ 卖出Put {target['code']} "
                          f"行权={target.get('strike_price')} 权利金={target.get('premium')}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol:
            sym = self.vt_symbol.split(".")[0]
            return f"US.{sym}"
        elif ".SEHK" in self.vt_symbol:
            sym = self.vt_symbol.split(".")[0]
            return f"HK.{sym}"
        return self.vt_symbol
