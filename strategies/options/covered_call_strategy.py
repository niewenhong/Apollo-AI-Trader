"""
strategies/options/covered_call_strategy.py - Apollo-AI-Trader v2.9.3
Covered Call：持股 + 卖Call（持股收租金增强收益）
前提：必须先持有正股
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CoveredCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    target_delta        = 0.20
    delta_tolerance     = 0.10
    min_otm_prob        = 70.0
    min_days_to_expiry  = 14
    max_days_to_expiry  = 45
    min_annual_roi      = 0.20
    position_size       = 1
    max_positions       = 5
    roll_when_ditm      = 0.35
    shares_per_contract = 100
    adx_trend_threshold = 20
    min_premium_usd     = 0.20

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expiry", "max_days_to_expiry", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "shares_per_contract", "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "pnl", "legs",
                 "stock_position", "regime_label", "last_adx"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.stock_position = 0

    # ── 同步正股持仓（真实读取） ─────────────────────────
    def _sync_stock_position(self):
        try:
            pos_mgr = getattr(self.cta_engine.main_engine, "position_manager", None)
            if pos_mgr:
                pos = pos_mgr.get_position(self.vt_symbol)
                self.stock_position = int(pos.volume) if pos else 0
                return
            # 备选：从 gateway 的 position 列表
            for gw_name in ("FUTU_US", "FUTU_HK"):
                gw = self.cta_engine.main_engine.get_gateway(gw_name)
                if gw and hasattr(gw, "positions"):
                    for p in gw.positions:
                        if p.vt_symbol == self.vt_symbol:
                            self.stock_position = int(p.volume)
                            return
        except Exception as e:
            self.write_log(f"[CovCall] 同步持仓异常: {e}")
            self.stock_position = 0

    # ── 趋势过滤 ──────────────────────────────────────────
    def on_5m_bar(self, bar: BarData):
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log(f"[CovCall] ADX={adx:.1f} 强涨趋势，暂停卖Call（让利润奔跑）")

    def on_bar(self, bar: BarData):
        self._sync_stock_position()
        if self._manage_expiry(bar): return
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta", 0)) > self.roll_when_ditm:
                    self._roll_positions()
                    return
        # 必须有正股才能卖Call
        needed_shares = self.shares_per_contract * self._scaled_size()
        if self.stock_position < needed_shares:
            return
        if not self.legs:
            self._find_call_to_sell(bar)

    # ── 选合约 ────────────────────────────────────────────
    def _find_call_to_sell(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = self._select_contracts(chain, "call")
        if not calls:
            self.write_log("[CovCall] 无符合条件Call")
            return
        lo = self.target_delta - self.delta_tolerance
        hi = self.target_delta + self.delta_tolerance
        in_band = [c for c in calls
                   if lo <= abs(c.get("delta", 0)) <= hi
                   and c.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            in_band = [c for c in calls if c.get("premium", 0) >= self.min_premium_usd]
        if not in_band:
            return
        in_band.sort(key=lambda x: -x.get("otm_prob", 0))
        target = in_band[0]
        target["name"]    = "covered_call"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN)
        if ok:
            self.net_premium += target.get("premium", 0)
            self.write_log(f"[CovCall] ✅ 卖出CoveredCall {target['code']} "
                           f"K={target.get('strike_price')} "
                           f"prem={target.get('premium'):.2f} "
                           f"otm%={target.get('otm_prob',0)*100:.0f} "
                           f"正股={self.stock_position}")

    def _roll_positions(self):
        super()._roll_positions()
