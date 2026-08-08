# -*- coding: utf-8 -*-
"""
strategies/options/straddle_strategy.py - Apollo-AI-Trader v3.1.1
Straddle：同时买入平值Call+Put，赌大波动（事件驱动）

v3.1.1 变更：
- 新增 on_init 实现（加载历史K线）
- 修复：expire_date 拼写统一（原 expir_y_date）
- 复用基类 _select_contracts 做流动性过滤
- 复用基类 _batch_quote 做批量报价
- 增加 _stopped 检查
- 增加初始化重试上限（max_retries=5）
"""
from datetime import datetime
from vnpy.trader.object import BarData, Direction, Offset
from vnpy.trader.constant import Interval
from strategies.options.base_option_strategy import BaseOptionStrategy


class StraddleStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "3.1.1"

    atm_offset_pct      = 0.02
    min_days_to_expiry  = 7
    max_days_to_expiry  = 30
    min_iv_percentile    = 30
    iv_lookback_bars    = 60
    event_adx_jump      = 10
    event_atr_mult      = 1.5
    profit_target_mult  = 2.0
    stop_loss_pct       = 0.5
    max_positions       = 1
    min_oi             = 50
    min_volume         = 10
    expire_close_days   = 2

    parameters = [
        "atm_offset_pct", "min_days_to_expiry", "max_days_to_expiry",
        "min_iv_percentile", "iv_lookback_bars",
        "event_adx_jump", "event_atr_mult",
        "profit_target_mult", "stop_loss_pct", "max_positions",
        "min_oi", "min_volume", "expire_close_days",
    ]
    variables = ["total_cost", "current_value", "pnl", "legs",
                 "event_detected", "iv_percentile_now", "regime_label"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.total_cost    = 0.0
        self.current_value = 0.0
        self.event_detected = False
        self.iv_percentile_now = 0.0
        self._atr_history = []
        self._adx_prev = 0.0
        self._init_retry_count = 0
        for param in self.parameters:
            if not hasattr(self, param):
                setattr(self, param, getattr(type(self), param, None))

    # ── 初始化（实现抽象方法） ──────────────────
    def on_init(self):
        """策略初始化：加载历史K线"""
        self.write_log("[Straddle] on_init 开始，加载历史K线")
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[Straddle] load_bar 异常: %s" % e)
        self._init_retry_count = 0
        self.write_log("[Straddle] on_init 完成")

    # ── 事件检测（5M） ──────────────────────────────────
    def _on_5m_bar_impl(self, bar: BarData):
        if self._stopped:
            return

        atr = getattr(self, "_atr_5m", 0.0)
        avg_atr = 0.0

        if atr > 0:
            self._atr_history.append(atr)
            if len(self._atr_history) > self.iv_lookback_bars:
                self._atr_history.pop(0)

        hist_len = len(self._atr_history)
        if hist_len >= 10:
            avg_atr = sum(self._atr_history) / hist_len
            cur_atr = atr if atr > 0 else avg_atr
            below_count = sum(1 for x in self._atr_history if x < cur_atr)
            self.iv_percentile_now = below_count / hist_len * 100
        elif hist_len > 0:
            avg_atr = sum(self._atr_history) / hist_len

        adx = getattr(self, "_adx_5m", 0.0)
        adx_jump = adx - self._adx_prev
        self._adx_prev = adx

        atr_expand = (atr > avg_atr * self.event_atr_mult) if avg_atr > 0 else False
        adx_surge  = (adx_jump >= self.event_adx_jump)
        iv_cheap   = (self.iv_percentile_now < self.min_iv_percentile)

        if (atr_expand or adx_surge) and iv_cheap and not self.event_detected:
            self.event_detected = True
            self.write_log("[Straddle] 事件信号! ADX跳=%.1f "
                           "ATR扩张=%s(%.2f vs avg%.2f) "
                           "IV%%=%.0f" %
                           (adx_jump, atr_expand, atr, avg_atr, self.iv_percentile_now))
            if not self.legs:
                self._find_straddle(bar)
        elif self.event_detected and not self.legs:
            self._find_straddle(bar)

        if self.legs:
            self._manage_position(bar)

    # ── 主K线逻辑（实现抽象方法） ──────────────
    def _on_bar_impl(self, bar: BarData):
        if self._stopped:
            return

        if self._manage_expire(bar):
            self.event_detected = False
            return

        if self.legs:
            self._manage_position(bar)

    # ── 找ATM双腿 ────────────────────────────────────
    def _find_straddle(self, bar: BarData):
        if self._init_retry_count >= 5:
            self.write_log("[Straddle] 初始化重试已达上限，跳过本次开仓")
            return
        self._init_retry_count += 1

        code = self._to_futu_code()
        if not code:
            return

        chain = self._query_full_chain(code)
        if chain is None:
            self.write_log("[Straddle] %s 期权链为空" % code)
            return

        # 使用基类筛选方法
        calls = self._select_contracts(chain, "call",
                                        min_days=self.min_days_to_expiry,
                                        max_days=self.max_days_to_expiry)
        puts  = self._select_contracts(chain, "put",
                                        min_days=self.min_days_to_expiry,
                                        max_days=self.max_days_to_expiry)

        if not calls or not puts:
            self.write_log("[Straddle] 链为空(筛选后) calls=%d puts=%d" %
                           (len(calls), len(puts)))
            return

        spot = bar.close_price
        if spot <= 0:
            self.write_log("[Straddle] 标的价格无效")
            return

        atm_call = min(calls, key=lambda c: abs(c.get("strike_price", 0) - spot))
        atm_put  = min(puts,  key=lambda p: abs(p.get("strike_price", 0) - spot))

        call_off = abs(atm_call["strike_price"] - spot) / spot
        put_off  = abs(atm_put["strike_price"] - spot) / spot

        if call_off > self.atm_offset_pct:
            self.write_log("[Straddle] ATM Call偏离过大 %.0f vs %.1f (%.1f%%>%.1f%%)" %
                           (atm_call['strike_price'], spot, call_off * 100,
                            self.atm_offset_pct * 100))
            return
        if put_off > self.atm_offset_pct:
            self.write_log("[Straddle] ATM Put偏离过大 %.0f vs %.1f (%.1f%%>%.1f%%)" %
                           (atm_put['strike_price'], spot, put_off * 100,
                            self.atm_offset_pct * 100))
            return

        if atm_call.get("expiry_date") != atm_put.get("expiry_date"):
            self.write_log("[Straddle] Call/Put 到期日不一致，跳过")
            return

        atm_call = dict(atm_call)
        atm_put = dict(atm_put)
        atm_call["name"]    = "std_call"
        atm_call["is_long"] = True
        atm_put["name"]     = "std_put"
        atm_put["is_long"]  = True

        ok1 = self._send_option_order(atm_call, Direction.LONG, Offset.OPEN,
                                      qty=self._scaled_size())
        ok2 = False
        if ok1:
            ok2 = self._send_option_order(atm_put, Direction.LONG, Offset.OPEN,
                                          qty=self._scaled_size())

        if ok1 and ok2:
            prem_c = atm_call.get("premium", 0)
            prem_p = atm_put.get("premium", 0)
            self.total_cost = prem_c + prem_p
            self.current_value = self.total_cost
            self.pnl = 0.0
            with self._legs_lock:
                self.legs["std_call"] = atm_call
                self.legs["std_put"]  = atm_put
            self.write_log("[Straddle] 买入双腿 cost=%.2f "
                           "call_K=%.0f(prem=%.2f) "
                           "put_K=%.0f(prem=%.2f) "
                           "exp=%s" %
                           (self.total_cost,
                            atm_call['strike_price'], prem_c,
                            atm_put['strike_price'], prem_p,
                            atm_call.get('expiry_date', '?')))
        else:
            self.write_log("[Straddle] 双腿开仓失败，回滚")
            if ok1 and not ok2:
                self._send_option_order(atm_call, Direction.SHORT, Offset.CLOSE)
            if ok2 and not ok1:
                self._send_option_order(atm_put, Direction.SHORT, Offset.CLOSE)
            self.event_detected = False

    # ── 持仓管理 ──────────────────────────────────
    def _manage_position(self, bar: BarData):
        codes = []
        for leg in self.legs.values():
            c = leg.get("code", "")
            if c:
                codes.append(c)

        quotes = self._batch_quote(codes) if codes else {}

        total_val = 0.0
        for name, leg in self.legs.items():
            code = leg.get("code", "")
            q = quotes.get(code, {})
            if q and q.get("price", 0) > 0:
                price = q["price"]
            elif q and q.get("bid", 0) > 0:
                price = q["bid"]
            else:
                price = leg.get("premium", 0)
            total_val += price

        self.current_value = total_val
        self.pnl = total_val - self.total_cost

        target = self.total_cost * self.profit_target_mult
        stop   = self.total_cost * self.stop_loss_pct

        if self.current_value >= target:
            self.write_log("[Straddle] 止盈 %.1f>=%.1f pnl=%.1f" %
                           (self.current_value, target, self.pnl))
            self._close_all_legs()
            self.event_detected = False
            self.total_cost = 0.0
            self.current_value = 0.0
        elif self.current_value <= stop:
            self.write_log("[Straddle] 止损 %.1f<=%.1f pnl=%.1f" %
                           (self.current_value, stop, self.pnl))
            self._close_all_legs()
            self.event_detected = False
            self.total_cost = 0.0
            self.current_value = 0.0

    # ── 到期管理（覆盖基类） ──────────────────────
    def _manage_expire(self, bar: BarData) -> bool:
        """到期管理：距离到期小于 expire_close_days 则平仓"""
        if not self.legs:
            return False
        now = getattr(bar, 'datetime', datetime.now())
        for name, leg in list(self.legs.items()):
            expire = leg.get("expiry_date") or leg.get("expire_date")
            if expire and isinstance(expire, datetime):
                days_left = (expire - now).days
                if days_left <= self.expire_close_days:
                    self.write_log("[Straddle] %s 即将到期，平仓" % name)
                    self._close_all_legs()
                    return True
        return False

    def _roll_positions(self):
        self._close_all_legs()
        self.event_detected = False
        self.total_cost = 0.0
        self.current_value = 0.0
        self.write_log("[Straddle] 展期完成，等事件信号重开")
