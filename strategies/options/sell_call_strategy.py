"""
strategies/options/sell_call_strategy.py - Apollo-AI-Trader v2.9.6
Sell Call：卖Call收权利金（看不涨/持股增强）

v2.9.6 变更：
- 删除：on_5m_bar 中未使用的 `from strategies.base_strategy import BaseStrategy`
- 修复：on_bar 调用 super().on_bar() 保证链路完整
- 修复：_check_tick_exit 签名统一（接受可选 bar 参数）
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class SellCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.6"

    target_delta          = 0.20
    delta_tolerance       = 0.10
    min_otm_prob         = 65.0
    min_days_to_expire    = 7
    max_days_to_expire    = 45
    min_annual_roi        = 0.30
    position_size         = 1
    max_positions         = 5
    roll_when_ditm        = 0.30
    adx_trend_threshold   = 20
    min_premium_usd       = 0.20

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expire", "max_days_to_expire", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs",
                 "regime_label", "last_adx"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.last_adx = 0.0

    # ── 行情 ──────────────────────────────────────
    def on_5m_bar(self, bar: BarData):
        """5M 趋势过滤：强上涨趋势时暂停卖Call"""
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log(f"[SellCall] ADX={adx:.1f} 强趋势，暂停卖Call")

    def on_bar(self, bar: BarData):
        """1M 主循环"""
        super().on_bar(bar)  # v2.9.6：保证链路完整
        if self._manage_expire(bar):
            return
        # 展期检查
        if self.legs:
            for name, leg in list(self.legs.items()):
                if abs(leg.get("delta", 0)) > self.roll_when_ditm:
                    self.write_log(f"[SellCall] bar复核展期 {name} "
                                   f"delta={leg.get('delta',0):.2f}")
                    self._roll_positions()
                    return
        # 开仓
        if not self.legs and len(self.legs) < self.max_positions:
            self._find_call_to_sell(bar)

    # ── 选合约 ────────────────────────────────────────
    def _find_call_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = self._select_contracts(chain, "call")
        if not calls:
            self.write_log("[SellCall] 无符合条件Call合约")
            return
        lo = self.target_delta - self.delta_tolerance
        hi = self.target_delta + self.delta_tolerance
        in_band = [c for c in calls
                   if lo <= abs(c.get("delta", 0)) <= hi
                   and c.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            in_band = [c for c in calls
                       if c.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            self.write_log("[SellCall] 无满足 delta/权利金 区间的Call")
            return
        in_band.sort(key=lambda x: -x.get("otm_prob", 0))
        target = in_band[0]
        target["name"]    = "sold_call"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.max_loss = 9e9  # 裸卖Call理论风险无限
            self.write_log(f"[SellCall] ✅ 卖出Call {target['code']} "
                           f"K={target.get('strike_price')} "
                           f"premium={target.get('premium'):.2f} "
                           f"delta={target.get('delta'):.2f} "
                           f"otm%={target.get('otm_prob',0)*100:.0f}")

    # ── Tick 退出（统一签名） ────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if len(self.legs) < 1:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0 or cost > 1e8:  # 裸卖风险无限时跳过
            return
        if cur_pnl < -cost * 0.8:
            self.write_log(f"[SellCall] 回撤止损 pnl={cur_pnl:.0f}")
            self._close_all_legs()
        elif cur_pnl > self.net_premium * 0.7:
            self.write_log(f"[SellCall] 止盈平仓 pnl={cur_pnl:.0f}")
            self._close_all_legs()

    def _roll_positions(self):
        super()._roll_positions()
        self.write_log("[SellCall] 展期完成，等待下根bar重新开仓")
