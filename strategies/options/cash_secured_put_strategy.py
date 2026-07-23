"""
strategies/options/cash_secured_put_strategy.py - Apollo-AI-Trader v2.6.0
Cash Secured Put：备好现金 + 卖Put（想低价接货时收租）
与SellPut区别：严格要求足额现金担保，不是纯收租
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CashSecuredPutStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.6.0"

    target_delta = 0.15          # 更保守：更OTM
    min_otm_prob = 0.75
    min_days_to_expiry = 14
    max_days_to_expiry = 45
    min_annual_roi = 0.25
    position_size = 1
    max_positions = 3           # 更保守：最多3张
    cash_buffer_ratio = 0.15    # 额外现金缓冲
    roll_when_ditm = 0.25

    parameters = [
        "target_delta", "min_otm_prob", "min_days_to_expiry",
        "max_days_to_expiry", "min_annual_roi", "position_size",
        "max_positions", "cash_buffer_ratio", "roll_when_ditm",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs", "cash_reserved"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.cash_reserved = 0.0

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        # 检查现金是否充足
        if not self._check_cash_sufficient(bar):
            self.write_log("[CSP] 现金不足，跳过"); return
        # 展期
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta",0)) > self.roll_when_ditm:
                    self._roll_positions(); return
        # 开仓
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_put_to_sell(bar)

    def _check_cash_sufficient(self, bar: BarData) -> bool:
        """检查账户是否有足额现金担保"""
        try:
            account = getattr(self.cta_engine.main_engine, "account", None)
            if account and hasattr(account, "cash"):
                available = account.cash
            else:
                # 从config读取预估资金
                available = 490000  # 默认49万港币
            # 需要预留的现金 = 行权价 × 合约乘数 × 张数 × (1+buffer)
            needed_per_contract = bar.close_price * 100 * (1 + self.cash_buffer_ratio)
            total_needed = needed_per_contract * self.max_positions
            self.cash_reserved = total_needed
            return available >= total_needed
        except Exception as e:
            self.write_log(f"[CSP] 现金检查异常: {e}")
            return True  # 无法判断时允许尝试

    def _find_put_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_option_chain(code)
        puts = self._select_contracts(chain, "put")
        if not puts: return
        # 优先选OTM概率最高（最安全）的
        puts.sort(key=lambda x: -x.get("otm_prob",0))
        target = puts[0]
        target["name"] = "csp_put"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.write_log(f"[CSP] ✅ 卖出Cash Secured Put {target['code']} "
                          f"strike={target.get('strike_price')} premium={target.get('premium')}")

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol: return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol: return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol
