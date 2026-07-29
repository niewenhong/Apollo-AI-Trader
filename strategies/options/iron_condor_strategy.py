"""
strategies/options/iron_condor_strategy.py - Apollo-AI-Trader v2.9.3
Iron Condor：卖OTM Call + 卖OTM Put + 买更OTM Call/Put 保护
预期震荡，双向收权利金，风险有限
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class IronCondorStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    delta_short_call    = 0.15
    delta_short_put     = -0.15
    delta_tolerance     = 0.10
    wing_width_pct      = 0.10     # 保护腿相对 short 腿的行权价偏离（真实用行权价差）
    min_days_to_expiry  = 21
    max_days_to_expiry  = 45
    min_net_credit_pct  = 0.30     # 净权利金 / 最大风险
    rolling_days        = 10
    max_positions       = 2
    adx_neutral_max     = 20       # ADX 低于此值才认为震荡
    prob_otm_min        = 70.0

    parameters = [
        "delta_short_call", "delta_short_put", "delta_tolerance",
        "wing_width_pct", "min_days_to_expiry", "max_days_to_expiry",
        "min_net_credit_pct", "rolling_days", "max_positions",
        "adx_neutral_max", "prob_otm_min",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl",
                 "legs", "regime_label", "last_adx"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.last_adx = 0.0

    def on_5m_bar(self, bar: BarData):
        """震荡过滤：ADX 低 + 价格在中枢附近"""
        self.last_adx = getattr(self, "_adx_5m", 0.0)
        if self.last_adx > self.adx_neutral_max:
            self.write_log(f"[IC] ADX={self.last_adx:.1f} 非震荡，暂停")
            return
        if not self.legs:
            self._find_condor(bar)

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs and len(self.legs) >= 4:
            for leg in self.legs.values():
                if leg.get("days_to_expiry", 999) <= self.rolling_days:
                    self._roll_positions()
                    return
        if self.legs and len(self.legs) >= 4:
            self._check_tick_exit()

    # ── 找4条腿 ──────────────────────────────────────────
    def _find_condor(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = [c for c in chain if c.get("is_call")]
        puts  = [c for c in chain if c.get("is_put")]
        if len(calls) < 2 or len(puts) < 2:
            self.write_log("[IC] Call/Put 各需≥2条")
            return

        sc = self._find_nearest_delta(calls,  self.delta_short_call, "call")
        sp = self._find_nearest_delta(puts,   self.delta_short_put,  "put")
        if not sc or not sp:
            self.write_log("[IC] short 腿未找到")
            return

        # 保护腿：更OTM（行权价更高/更低）
        wc_cands = [c for c in calls
                    if c["strike_price"] > sc["strike_price"]
                    and c.get("premium", 0) > 0]
        wp_cands = [p for p in puts
                    if p["strike_price"] < sp["strike_price"]
                    and p.get("premium", 0) > 0]
        if not wc_cands or not wp_cands:
            self.write_log("[IC] 保护腿未找到")
            return
        # 选最便宜的保护腿（成本最低）
        wing_call = min(wc_cands, key=lambda c: c.get("premium", 9e9))
        wing_put  = min(wp_cands, key=lambda p: p.get("premium", 9e9))

        # 净权利金
        net = (sc.get("premium",0) + sp.get("premium",0)
             - wing_call.get("premium",0) - wing_put.get("premium",0))
        if net <= 0:
            self.write_log(f"[IC] 净权利金为负 {net:.2f}")
            return

        # 真实最大风险 = 短腿与保护腿的行权价差 - 净权利金
        call_spread = wing_call["strike_price"] - sc["strike_price"]
        put_spread  = sp["strike_price"] - wing_put["strike_price"]
        real_width  = min(call_spread, put_spread)
        max_risk    = (real_width - net) * 100
        if max_risk <= 0:
            self.write_log(f"[IC] 最大风险非正 {max_risk}")
            return
        credit_pct = net * 100 / max_risk
        if credit_pct < self.min_net_credit_pct:
            self.write_log(f"[IC] 净权利金占比 {credit_pct:.2%} < {self.min_net_credit_pct:.0%}")
            return

        # 开仓4腿（带回滚）
        legs_plan = [
            (sc,        "ic_sc", False),
            (sp,        "ic_sp", False),
            (wing_call, "ic_wc", True),
            (wing_put,  "ic_wp", True),
        ]
        opened = []
        for raw, name, is_long in legs_plan:
            leg = dict(raw)
            leg["name"]    = name
            leg["is_long"] = is_long
            direction = Direction.LONG if is_long else Direction.SHORT
            ok = self._send_option_order(leg, direction, Offset.OPEN)
            if ok:
                opened.append(name)
            else:
                self.write_log(f"[IC] {name} 开仓失败，回滚")
                for n in opened:
                    old = self.legs.pop(n, None)
                    if old:
                        d = Direction.SHORT if old.get("is_long") else Direction.LONG
                        self._send_option_order(old, d, Offset.CLOSE)
                return

        self.net_premium = net
        self.max_loss    = max_risk
        self.max_profit  = net * 100
        self.write_log(f"[IC] ✅ net={net:.2f} max_loss={self.max_loss:.0f} "
                       f"credit%={credit_pct:.1%}")

    # ── 工具 ──────────────────────────────────────────────
    def _find_nearest_delta(self, chain, target, leg_type):
        best, best_diff = None, 999
        for c in chain:
            if leg_type == "call" and not c.get("is_call"): continue
            if leg_type == "put"  and not c.get("is_put"):  continue
            d = abs(abs(c.get("delta",0)) - abs(target))
            if d < best_diff:
                best_diff, best = d, c
        return best

    # ── Tick 退出 ────────────────────────────────────────
    def _check_tick_exit(self):
        cur = self._estimate_pnl()
        if cur < -self.max_loss * 0.8:
            self.write_log(f"[IC] 止损 cur={cur:.0f}")
            self._close_all_legs()
        elif cur > self.max_profit * 0.7:
            self.write_log(f"[IC] 止盈 cur={cur:.0f}")
            self._close_all_legs()

    def _roll_positions(self):
        super()._roll_positions()
