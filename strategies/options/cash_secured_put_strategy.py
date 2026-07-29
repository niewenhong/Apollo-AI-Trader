"""
strategies/options/cash_secured_put_strategy.py - Apollo-AI-Trader v2.9.3
Cash Secured Put：备足现金 + 卖Put（想低价接货时收租）
比 SellPut 更保守：要求足额现金担保 + 更高 OTM
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CashSecuredPutStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    target_delta        = 0.15     # 更OTM
    delta_tolerance     = 0.08
    min_otm_prob        = 75.0
    min_days_to_expiry  = 14
    max_days_to_expiry  = 45
    min_annual_roi      = 0.25
    position_size       = 1
    max_positions       = 3        # 更保守
    roll_when_ditm      = 0.25
    cash_buffer_ratio   = 0.15     # 更高缓冲
    adx_trend_threshold = 20
    min_premium_usd     = 0.30

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expiry", "max_days_to_expiry", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "cash_buffer_ratio", "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs",
                 "cash_reserved", "regime_label"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.cash_reserved = 0.0

    def on_5m_bar(self, bar: BarData):
        adx = getattr(self, "_adx_5m", 0.0)
        if adx > self.adx_trend_threshold:
            self.write_log(f"[CSP] ADX={adx:.1f} 强趋势，暂停")

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta", 0)) > self.roll_when_ditm:
                    self._roll_positions()
                    return
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_put_to_sell(bar)

    def _find_put_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        puts = self._select_contracts(chain, "put")
        if not puts:
            self.write_log("[CSP] 无符合条件Put")
            return
        # 更保守：严格按 delta 区间
        lo = self.target_delta - self.delta_tolerance
        hi = self.target_delta + self.delta_tolerance
        in_band = [p for p in puts
                   if lo <= abs(p.get("delta", 0)) <= hi
                   and p.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            self.write_log("[CSP] 无满足严格delta区间的Put（保守跳过）")
            return
        in_band.sort(key=lambda x: -x.get("otm_prob", 0))
        target = in_band[0]

        # 现金充足检查
        strike = target.get("strike_price", bar.close_price)
        need = strike * 100 * self._scaled_size() * (1 + self.cash_buffer_ratio)
        self.cash_reserved = need
        avail = self._get_available_cash()
        if avail > 0 and avail < need:
            self.write_log(f"[CSP] 现金不足 需≈{need:.0f} 可用={avail:.0f} 跳过")
            return

        target["name"]    = "csp_put"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.write_log(f"[CSP] ✅ 卖出CashSecuredPut {target['code']} "
                           f"K={strike} prem={target.get('premium'):.2f} "
                           f"otm%={target.get('otm_prob',0)*100:.0f}")

    def _roll_positions(self):
        super()._roll_positions()
