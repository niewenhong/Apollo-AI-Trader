"""
strategies/options/straddle_strategy.py - Apollo-AI-Trader v2.9.3
Straddle：同时买入平值Call+Put，赌大波动（事件驱动）
✅ 事件检测真实化：基于5M ATR vs 历史ATR百分位
✅ 止损/止盈用真实报价
"""
from vnpy.trader.object import BarData, Direction, Offset
from strategies.options.base_option_strategy import BaseOptionStrategy


class StraddleStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "v2.9.3"

    atm_offset_pct      = 0.02
    min_days_to_expiry  = 7
    max_days_to_expiry  = 30
    min_iv_percentile    = 30       # 当前IV低于此百分位才买入（便宜）
    iv_lookback_bars    = 60       # 用最近60根5M bar估IV百分位
    event_adx_jump      = 10       # ADX 5M 较前一根上涨>=此值视为事件临近
    event_atr_mult      = 1.5      # 当前ATR > 近期ATR均值×此值视为事件临近
    profit_target_mult  = 2.0      # 盈利目标 = 净成本 × 倍数
    stop_loss_pct       = 0.5      # 止损 = 净成本 × 百分比
    max_positions       = 1

    parameters = [
        "atm_offset_pct", "min_days_to_expiry", "max_days_to_expiry",
        "min_iv_percentile", "iv_lookback_bars",
        "event_adx_jump", "event_atr_mult",
        "profit_target_mult", "stop_loss_pct", "max_positions",
    ]
    variables = ["total_cost", "current_value", "pnl", "legs",
                 "event_detected", "iv_percentile_now", "regime_label"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.total_cost    = 0.0
        self.current_value = 0.0
        self.event_detected = False
        self.iv_percentile_now = 0.0
        self._atr_history: list = []
        self._adx_prev = 0.0

    # ── 事件检测（5M） ───────────────────────────────────
    def on_5m_bar(self, bar: BarData):
        """用5M ATR扩张 + ADX跳升 近似检测事件临近"""
        # ATR 估计（用 high-low 近似）
        atr = getattr(self, "_atr_5m", 0.0)
        if atr > 0:
            self._atr_history.append(atr)
            if len(self._atr_history) > self.iv_lookback_bars:
                self._atr_history.pop(0)
        adx = getattr(self, "_adx_5m", 0.0)
        adx_jump = adx - self._adx_prev
        self._adx_prev = adx

        # IV 百分位（用ATR近似）
        if len(self._atr_history) >= 10:
            avg_atr = sum(self._atr_history) / len(self._atr_history)
            cur_atr = atr if atr > 0 else avg_atr
            self.iv_percentile_now = sum(
                1 for x in self._atr_history if x < cur_atr
            ) / len(self._atr_history) * 100

        # 事件判定
        atr_expand = (atr > avg_atr * self.event_atr_mult) if atr > 0 and avg_atr > 0 else False
        adx_surge  = (adx_jump >= self.event_adx_jump)
        iv_cheap   = (self.iv_percentile_now < self.min_iv_percentile)

        if (atr_expand or adx_surge) and iv_cheap and not self.event_detected:
            self.event_detected = True
            self.write_log(f"[Straddle] 事件信号! ADX跳={adx_jump:.1f} "
                           f"ATR扩张={atr_expand} IV%={self.iv_percentile_now:.0f}")
            if not self.legs:
                self._find_straddle(bar)
        elif self.event_detected and not self.legs:
            self._find_straddle(bar)

        # 持仓管理
        if self.legs:
            self._manage_position(bar)

    def on_bar(self, bar: BarData):
        if self._manage_expiry(bar): return

    # ── 找ATM双腿 ────────────────────────────────────────
    def _find_straddle(self, bar: BarData):
        code = self._to_futu_code()
        chain = self._query_full_chain(code)
        calls = [c for c in chain if c.get("is_call")]
        puts  = [c for c in chain if c.get("is_put")]
        spot  = bar.close_price
        if not calls or not puts:
            self.write_log("[Straddle] 链为空")
            return
        atm_call = min(calls, key=lambda c: abs(c.get("strike_price",0)-spot))
        atm_put  = min(puts,  key=lambda p: abs(p.get("strike_price",0)-spot))
        # 距离ATM不超过 offset
        if abs(atm_call["strike_price"]-spot)/spot > self.atm_offset_pct:
            self.write_log(f"[Straddle] ATM Call偏离过大 "
                           f"{atm_call['strike_price']} vs {spot:.1f}")
            return
        if abs(atm_put["strike_price"]-spot)/spot > self.atm_offset_pct:
            return

        atm_call["name"]    = "std_call"
        atm_call["is_long"] = True
        atm_put["name"]     = "std_put"
        atm_put["is_long"]  = True

        ok1 = self._send_option_order(atm_call, Direction.LONG, Offset.OPEN)
        ok2 = self._send_option_order(atm_put,  Direction.LONG, Offset.OPEN)
        if ok1 and ok2:
            self.total_cost = (atm_call.get("premium",0)
                             + atm_put.get("premium",0))
            self.write_log(f"[Straddle] ✅ 买入双腿 cost={self.total_cost:.2f} "
                           f"call_K={atm_call['strike_price']} "
                           f"put_K={atm_put['strike_price']}")
        else:
            # 部分成交回滚
            if ok1 and not ok2:
                self._send_option_order(atm_call, Direction.SHORT, Offset.CLOSE)
            if ok2 and not ok1:
                self._send_option_order(atm_put, Direction.SHORT, Offset.CLOSE)
            self.write_log("[Straddle] 双腿开仓失败，已回滚")

    # ── 持仓管理（用真实报价） ───────────────────────────
    def _manage_position(self, bar: BarData):
        quotes = self._batch_quote(list(self.legs.keys()))
        total_val = 0.0
        for name, leg in self.legs.items():
            q = quotes.get(leg["code"], {})
            total_val += q.get("price", leg.get("premium", 0))
        self.current_value = total_val
        self.pnl = total_val - self.total_cost

        target = self.total_cost * self.profit_target_mult
        stop   = self.total_cost * self.stop_loss_pct
        if self.current_value >= target:
            self.write_log(f"[Straddle] 止盈 {self.current_value:.1f}>={target:.1f}")
            self._close_all_legs()
            self.event_detected = False
        elif self.current_value <= stop:
            self.write_log(f"[Straddle] 止损 {self.current_value:.1f}<={stop:.1f}")
            self._close_all_legs()
            self.event_detected = False

    def _roll_positions(self):
        super()._roll_positions()
        self.event_detected = False
