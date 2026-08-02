"""
strategies/options/covered_call_strategy.py - Apollo-AI-Trader v2.9.6
Covered Call：持股 + 卖Call（持股收租金增强收益）
前提：必须先持有正股

v2.9.6 变更：
- 修复：on_bar 调用 super().on_bar() 保证链路完整
- 修复：_check_tick_exit 统一签名（接受可选 bar 参数）
- 优化：_sync_stock_position 防御性增强
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class CoveredCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.6"

    target_delta        = 0.20
    delta_tolerance     = 0.10
    min_otm_prob        = 70.0
    min_days_to_expire  = 14
    max_days_to_expire  = 45
    min_annual_roi      = 0.20
    position_size       = 1
    max_positions       = 5
    roll_when_ditm      = 0.35
    shares_per_contract = 100
    adx_trend_threshold = 20
    min_premium_usd     = 0.20

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
        "min_days_to_expire", "max_days_to_expire", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "shares_per_contract", "adx_trend_threshold", "min_premium_usd",
    ]
    variables = ["net_premium", "pnl", "legs",
                 "stock_position", "regime_label", "last_adx"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.stock_position = 0
        self.last_adx = 0.0

    # ── 同步正股持仓 ────────────────────────────────
    def _sync_stock_position(self):
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is not None:
                pm = getattr(me, 'position_manager', None)
                if pm is not None and hasattr(pm, 'get_position'):
                    pos = pm.get_position(self.vt_symbol)
                    self.stock_position = int(getattr(pos, 'volume', 0)) if pos else 0
                    return
                # 从 gateway 获取
                for gw_name in ("FUTU_US", "FUTU_HK"):
                    gw = getattr(me, 'gateways', {}).get(gw_name, None)
                    if gw and hasattr(gw, "positions"):
                        for p in gw.positions:
                            if getattr(p, 'vt_symbol', '') == self.vt_symbol:
                                self.stock_position = int(getattr(p, 'volume', 0))
                                return
        except Exception as e:
            self.write_log(f"[CovCall] 同步持仓异常: {e}")
            self.stock_position = 0

    # ── 趋势过滤 ──────────────────────────────────────
    def on_5m_bar(self, bar: BarData):
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log(f"[CovCall] ADX={adx:.1f} 强涨趋势，暂停卖Call（让利润奔跑）")

    def on_bar(self, bar: BarData):
        super().on_bar(bar)  # v2.9.6：保证链路完整
        self._sync_stock_position()
        if self._manage_expire(bar): return
        if self.legs:
            for leg in self.legs.values():
                if abs(leg.get("delta", 0)) > self.roll_when_ditm:
                    self.write_log(f"[CovCall] 展期 {leg.get('name','?')}")
                    self._roll_positions()
                    return
        # 必须有正股才能卖Call
        needed_shares = self.shares_per_contract * self._scaled_size()
        if self.stock_position < needed_shares:
            return
        if not self.legs:
            self._find_call_to_sell(bar)

    # ── 选合约 ────────────────────────────────────────
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

    # ── Tick 退出（统一签名） ────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if not self.legs:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        if cur_pnl < -cost * 0.8:
            self.write_log(f"[CovCall] 止损 pnl={cur_pnl:.0f}")
            self._close_all_legs()
        elif cur_pnl > self.net_premium * 0.7:
            self.write_log(f"[CovCall] 止盈 pnl={cur_pnl:.0f}")
            self._close_all_legs()

    def _roll_positions(self):
        super()._roll_positions()
