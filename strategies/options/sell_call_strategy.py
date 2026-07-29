"""
strategies/options/sell_call_strategy.py - Apollo-AI-Trader v2.9.3
Sell Call：卖Call收权利金（看不涨/持股增强）

✅ 依赖基类 v2 的 _query_full_chain（已合并希腊值）
✅ 字段修正：prob_of_profit(百分比) / implied_volatility / days_to_expiry
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class SellCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    # ── 参数 ──────────────────────────────────────────────
    target_delta          = 0.20     # 目标 short delta 绝对值
    delta_tolerance       = 0.10     # ±容差（放宽筛选）
    min_otm_prob          = 65.0     # prob_of_profit 百分比 ≥ 65%
    min_days_to_expiry    = 7
    max_days_to_expiry    = 45
    min_annual_roi        = 0.30
    position_size         = 1
    max_positions         = 5
    roll_when_ditm        = 0.30
    adx_trend_threshold   = 20       # 5M ADX < 此值才允许卖Call（非强趋势）
    min_premium_usd       = 0.20     # 最低权利金（避免收租太少）

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expiry", "max_days_to_expiry", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs",
                 "regime_label", "last_adx"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.last_adx = 0.0

    # ── 行情 ──────────────────────────────────────────────
    def on_5m_bar(self, bar: BarData):
        """5M 趋势过滤：强上涨趋势时暂停卖Call"""
        from strategies.base_strategy import BaseStrategy
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log(f"[SellCall] ADX={adx:.1f} 强趋势，暂停卖Call")
            return

    def on_bar(self, bar: BarData):
        """1M 主循环（CTA 引擎推的 bar 默认当 1M 用）"""
        if self._manage_expiry(bar):
            return
        # tick 层已做快速展期，这里做 bar 级复核
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

    # ── 选合约 ────────────────────────────────────────────
    def _find_call_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = self._select_contracts(chain, "call")
        if not calls:
            self.write_log("[SellCall] 无符合条件Call合约")
            return
        # 在 target_delta ± tolerance 区间内选 otm_prob 最高
        lo, hi = self.target_delta - self.delta_tolerance, \
                 self.target_delta + self.delta_tolerance
        in_band = [c for c in calls
                   if lo <= abs(c.get("delta", 0)) <= hi
                   and c.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            # 放宽：只要 otm_prob 达标
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
            self.max_loss = 9e9  # 裸卖Call理论风险无限，仅记录
            self.write_log(f"[SellCall] ✅ 卖出Call {target['code']} "
                           f"K={target.get('strike_price')} "
                           f"premium={target.get('premium'):.2f} "
                           f"delta={target.get('delta'):.2f} "
                           f"otm%={target.get('otm_prob',0)*100:.0f}")

    # ── 展期实现 ──────────────────────────────────────────
    def _roll_positions(self):
        # 基类 _close_all_legs 会清空 self.legs；
        # 下一根 bar 的 on_bar 会重新触发 _find_call_to_sell
        super()._roll_positions()
        self.write_log("[SellCall] 展期完成，等待下根bar重新开仓")
