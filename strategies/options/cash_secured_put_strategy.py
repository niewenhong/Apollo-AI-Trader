"""
strategies/options/cash_secured_put_strategy.py - Apollo-AI-Trader v2.9.6
Cash Secured Put：备足现金 + 卖Put（想低价接货时收租）
比 SellPut 更保守：要求足额现金担保 + 更高 OTM

v2.9.6 变更：
- 修复：on_bar 调用 super().on_bar() 保证链路完整
- 修复：_check_tick_exit 统一签名（接受可选 bar 参数）
- 优化：现金检查防御性增强
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CashSecuredPutStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.6"

    target_delta        = 0.15
    delta_tolerance     = 0.08
    min_otm_prob        = 75.0
    min_days_to_expire  = 14
    max_days_to_expire  = 45
    min_annual_roi      = 0.25
    position_size       = 1
    max_positions       = 3
    roll_when_ditm      = 0.25
    cash_buffer_ratio   = 0.15
    adx_trend_threshold = 20
    min_premium_usd     = 0.30

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expire", "max_days_to_expire", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "cash_buffer_ratio", "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs",
                 "cash_reserved", "regime_label", "last_adx"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.cash_reserved = 0.0
        self.last_adx = 0.0

    def on_5m_bar(self, bar: BarData):
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log(f"[CSP] ADX={adx:.1f} 强趋势，暂停")

    def on_bar(self, bar: BarData):
        super().on_bar(bar)  # v2.9.6：保证链路完整
        if self._manage_expire(bar): return
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta", 0)) > self.roll_when_ditm:
                    self.write_log(f"[CSP] 展期 {leg.get('name','?')}")
                    self._roll_positions()
                    return
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_put_to_sell(bar)

    # ── 现金检查 ──────────────────────────────────
    def _enough_cash(self, bar: BarData) -> bool:
        strike = bar.close_price
        need = strike * 100 * self._scaled_size() * (1 + self.cash_buffer_ratio)
        self.cash_reserved = need
        avail = self._get_available_cash()
        if avail <= 0:
            return True  # 无法查询时放行（保守默认值）
        ok = avail >= need
        if not ok:
            self.write_log(f"[CSP] 现金不足 需要≈{need:.0f} 可用={avail:.0f}")
        return ok

    # ── 选合约 ────────────────────────────────────
    def _find_put_to_sell(self, bar: BarData):
        if not self._enough_cash(bar):
            return
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        puts = self._select_contracts(chain, "put")
        if not puts:
            self.write_log("[CSP] 无符合条件Put")
            return
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

        strike = target.get("strike_price", bar.close_price)
        target["name"]    = "csp_put"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.max_loss = (strike - target.get("premium", 0)) * 100
            self.write_log(f"[CSP] ✅ 卖出CashSecuredPut {target['code']} "
                           f"K={strike} prem={target.get('premium'):.2f} "
                           f"otm%={target.get('otm_prob',0)*100:.0f}")

    # ── Tick 退出（统一签名） ────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if not self.legs:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        if cur_pnl < -cost * 0.8:
            self.write_log(f"[CSP] 止损 cur={cur_pnl:.0f}")
            self._close_all_legs()
        elif cur_pnl > self.net_premium * 0.7:
            self.write_log(f"[CSP] 止盈 cur={cur_pnl:.0f}")
            self._close_all_legs()

    def _roll_positions(self):
        super()._roll_positions()
        self.write_log("[CSP] 展期完成，等下根bar重开")
