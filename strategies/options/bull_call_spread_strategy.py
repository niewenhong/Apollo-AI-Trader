"""
strategies/options/bull_call_spread_strategy.py - Apollo-AI-Trader v2.9.3
Bull Call Spread：买低K Call + 卖高K Call，温和看涨，风险有限
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class BullCallSpreadStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    delta_long         = 0.35
    delta_short        = 0.15
    delta_tolerance    = 0.15     # ±放宽（旧版±0.1 太窄常落空）
    min_days_to_expiry = 14
    max_days_to_expiry = 45
    min_credit_ratio   = 0.30     # short.premium / long.premium
    min_net_debit_pct  = 0.005    # 净成本 < 价差宽度 × 此比例才合格
    rolling_days       = 7
    max_positions      = 3
    adx_uptrend_min    = 18       # 5M ADX 至少此值（确认多头）
    ema_fast_period    = 5
    ema_slow_period    = 20

    parameters = [
        "delta_long", "delta_short", "delta_tolerance",
        "min_days_to_expiry", "max_days_to_expiry",
        "min_credit_ratio", "min_net_debit_pct",
        "rolling_days", "max_positions",
        "adx_uptrend_min", "ema_fast_period", "ema_slow_period",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl",
                 "legs", "regime_label", "last_adx"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.last_adx = 0.0

    def on_5m_bar(self, bar: BarData):
        """5M 趋势确认：EMA快>EMA慢 + ADX>阈值"""
        self.last_adx = getattr(self, "_adx_5m", 0.0)
        ema_fast = getattr(self, "_ema_5m_fast", bar.close_price)
        ema_slow = getattr(self, "_ema_5m_slow", bar.close_price)
        if ema_fast <= ema_slow:
            return  # 不在多头排列，不开仓
        if self.last_adx < self.adx_uptrend_min:
            return
        # 趋势确认通过，触发开仓
        if not self.legs:
            self._find_spread(bar)

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return
        if self.legs and len(self.legs) >= 2:
            for leg in self.legs.values():
                if leg.get("days_to_expiry", 999) <= self.rolling_days:
                    self.write_log("[BCS] 临近到期，展期")
                    self._roll_positions()
                    return
        # tick 快速回撤平仓
        if self.legs and len(self.legs) >= 2:
            self._check_tick_exit(bar)

    # ── 选价差 ────────────────────────────────────────────
    def _find_spread(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = [c for c in chain if c.get("is_call")]
        if len(calls) < 2:
            self.write_log("[BCS] Call 合约不足2条")
            return

        long_cands  = [c for c in calls
                       if abs(abs(c.get("delta",0))-self.delta_long) <= self.delta_tolerance]
        short_cands = [c for c in calls
                       if abs(abs(c.get("delta",0))-self.delta_short) <= self.delta_tolerance]
        if not long_cands or not short_cands:
            # 放宽：直接按 delta 排序取最近
            long_cands  = sorted(calls, key=lambda c: abs(abs(c.get("delta",0))-self.delta_long))[:5]
            short_cands = sorted(calls, key=lambda c: abs(abs(c.get("delta",0))-self.delta_short))[:5]

        best = None
        for lc in long_cands:
            for sc in short_cands:
                if sc.get("strike_price", 0) <= lc.get("strike_price", 0):
                    continue
                long_p  = lc.get("premium", 0)
                short_p = sc.get("premium", 0)
                if long_p <= 0:
                    continue
                credit = short_p / long_p
                if credit < self.min_credit_ratio:
                    continue
                width = sc["strike_price"] - lc["strike_price"]
                net   = long_p - short_p
                if net >= width * (1 - self.min_net_debit_pct):
                    continue  # 净成本太高，放弃
                if best is None or net < best[2]:
                    best = (lc, sc, net, width)
        if not best:
            self.write_log("[BCS] 无合适价差组合")
            return
        long_leg, short_leg, net, width = best
        long_leg["name"]    = "bcs_long"
        long_leg["is_long"] = True
        short_leg["name"]    = "bcs_short"
        short_leg["is_long"] = False
        ok = self._open_spread(long_leg, short_leg)
        if ok:
            self.max_loss   = net * 100
            self.max_profit = (width - net) * 100
            self.write_log(f"[BCS] ✅ long@{long_leg['strike_price']} "
                           f"short@{short_leg['strike_price']} "
                           f"net={net:.2f} width={width} "
                           f"max_loss={self.max_loss:.0f}")

    # ── Tick 回撤平仓 ────────────────────────────────────
    def _check_tick_exit(self, bar: BarData):
        if len(self.legs) < 2:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        # 已亏损超过最大风险的 80% → 止损
        if cur_pnl < -cost * 0.8:
            self.write_log(f"[BCS] 回撤止损 pnl={cur_pnl:.0f}")
            self._close_all_legs()
        # 已盈利超过最大收益的 70% → 止盈
        elif cur_pnl > self.max_profit * 0.7:
            self.write_log(f"[BCS] 止盈平仓 pnl={cur_pnl:.0f}")
            self._close_all_legs()

    def _roll_positions(self):
        super()._roll_positions()
