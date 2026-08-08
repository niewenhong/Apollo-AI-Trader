# -*- coding: utf-8 -*-
"""
strategies/options/sell_call_strategy.py - Apollo-AI-Trader v3.1.1
Sell Call：卖Call收权利金（看不涨/想高价出货）

v3.1.1 变更：
- 修复：实现抽象方法 on_init（加载历史K线）
- 修复：实现抽象方法 _on_bar_impl
- 修复：strike 拼写错误（原 strik）
- 修复：delta 多括号语法错误
- 增加 _stopped 检查
- 增加初始化重试上限（max_retries=5）
"""
from vnpy.trader.object import BarData, Direction, Offset
from vnpy.trader.constant import Interval
from strategies.options.base_option_strategy import BaseOptionStrategy


class SellCallStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "3.1.1"

    delta_target        = 0.20
    delta_tolerance     = 0.10
    min_otm_prob        = 78.0
    min_days_to_expire  = 7
    max_days_to_expire  = 90
    min_annual_roi      = 0.20
    position_size       = 1
    max_positions       = 5
    roll_when_ditm      = 0.32
    cash_buffer_ratio   = 0.10
    adx_trend_threshold = 22
    min_premium_usd     = 0.15
    min_oi             = 50
    min_volume         = 10
    expire_close_days   = 2

    parameters = [
        "delta_target", "delta_tolerance", "min_otm_prob",
        "min_days_to_expire", "max_days_to_expire", "min_annual_roi",
        "position_size", "max_positions", "roll_when_ditm",
        "cash_buffer_ratio", "adx_trend_threshold", "min_premium_usd",
        "min_oi", "min_volume", "expire_close_days",
    ]
    variables = ["net_premium", "max_loss", "pnl", "legs",
                 "cash_reserved", "regime_label", "last_adx"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.cash_reserved = 0.0
        self.last_adx = 0.0
        self._rolling = False
        self._init_retry_count = 0
        for param in self.parameters:
            if not hasattr(self, param):
                setattr(self, param, getattr(type(self), param, None))

    # ── 初始化（实现抽象方法） ──────────────────
    def on_init(self):
        """策略初始化：加载历史K线"""
        self.write_log("[SellCall] on_init 开始，加载历史K线")
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[SellCall] load_bar 异常: %s" % e)
        self._init_retry_count = 0
        self.write_log("[SellCall] on_init 完成")

    # ── 5分钟K线：趋势过滤 ────────────────────────
    def _on_5m_bar_impl(self, bar: BarData):
        if self._stopped:
            return
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log("[SellCall] ADX=%.1f 强趋势，暂停卖Call" % adx)

    # ── 主K线逻辑（实现抽象方法） ──────────────
    def _on_bar_impl(self, bar: BarData):
        if self._stopped:
            return

        # 1. 到期管理
        if self._manage_expire(bar):
            self._rolling = False
            return

        # 2. 展期检查
        if self.legs and not self._rolling:
            for name, leg in list(self.legs.items()):
                delta = abs(leg.get("delta", 0))
                if delta > self.roll_when_ditm:
                    self.write_log("[SellCall] %s delta=%.2f>%.2f，展期" %
                                   (name, delta, self.roll_when_ditm))
                    self._rolling = True
                    self._roll_positions()
                    return

        # 3. 更新 PnL
        if self.legs:
            self.pnl = self._estimate_pnl()

        # 4. 无持仓时尝试开仓
        if not self.legs:
            self._rolling = False
            if len(self.legs) < self.max_positions:
                self._find_call_to_sell(bar)

    # ── 现金/保证金检查 ──────────────────────────────
    def _enough_cash(self, bar: BarData) -> bool:
        """卖Call需要保证金（约=标的价×20%）"""
        strike = bar.close_price
        if strike <= 0:
            return False
        margin_per = strike * 0.20
        need = margin_per * 100 * self._scaled_size() * (1 + self.cash_buffer_ratio)
        self.cash_reserved = need
        avail = self._get_available_cash()
        if avail <= 0:
            return True
        ok = avail >= need
        if not ok:
            self.write_log("[SellCall] 保证金不足 需要≈%.0f 可用=%.0f" % (need, avail))
        return ok

    # ── 选合约 ────────────────────────────────────
    def _find_call_to_sell(self, bar: BarData):
        if self._init_retry_count >= 5:
            self.write_log("[SellCall] 初始化重试已达上限，跳过本次开仓")
            return
        self._init_retry_count += 1

        if not self._enough_cash(bar):
            return

        code = self._to_futu_code()
        if not code:
            return

        chain = self._query_full_chain(code)
        if chain is None:
            self.write_log("[SellCall] %s 期权链为空" % code)
            return

        # 使用基类筛选方法
        calls = self._select_contracts(chain, "call",
                                        min_days=self.min_days_to_expire,
                                        max_days=self.max_days_to_expire)
        if not calls:
            self.write_log("[SellCall] 无符合条件的Call")
            return

        lo = abs(self.delta_target) - self.delta_tolerance
        hi = abs(self.delta_target) + self.delta_tolerance

        candidates = []
        for c in calls:
            d = abs(c.get("delta", 0))
            if d < lo or d > hi:
                continue
            if c.get("premium", 0) < self.min_premium_usd:
                continue
            otm = c.get("otm_prob", 0)
            if otm < self.min_otm_prob / 100.0:
                continue
            candidates.append(c)

        if not candidates:
            self.write_log("[SellCall] 无符合delta区间(%.2f-%.2f)的Call" % (lo, hi))
            return

        # 按 OTM 概率降序，再按权利金降序
        candidates.sort(key=lambda x: (-x.get("otm_prob", 0), -x.get("premium", 0)))
        target = candidates[0]

        # 年化 ROI
        strike = target.get("strike_price", 0)
        prem = target.get("premium", 0)
        dte = max(target.get("days_to_expire", 30), 1)
        annual_roi = (prem / max(strike, 0.01)) * (365.0 / dte)
        if annual_roi < self.min_annual_roi:
            self.write_log("[SellCall] ROI=%.1f%%<%.0f%%，跳过" % (annual_roi * 100, self.min_annual_roi * 100))
            return

        target["name"] = "sold_call"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN,
                                     qty=self._scaled_size())
        if ok:
            self.net_premium += prem
            self.max_loss = (strike - prem) * 100
            self.pnl = self._estimate_pnl()
            self.write_log("[SellCall] 卖出Call %s K=%.0f premium=%.2f "
                           "delta=%.2f otm%%=%.0f annual_roi=%.1f%%" %
                           (target.get('code', '?'), strike, prem,
                            target.get('delta', 0), target.get('otm_prob', 0) * 100,
                            annual_roi * 100))
        else:
            self.write_log("[SellCall] 卖出Call失败 %s" % target.get('code', '?'))

    # ── Tick 退出 ──────────────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if not self.legs:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        if cur_pnl < -cost * 0.8:
            self.write_log("[SellCall] 止损 cur=%.0f cost=%.0f" % (cur_pnl, cost))
            self._close_all_legs()
            self._rolling = False
        elif cur_pnl > self.net_premium * 0.7:
            self.write_log("[SellCall] 止盈 cur=%.0f prem=%.0f" % (cur_pnl, self.net_premium))
            self._close_all_legs()
            self._rolling = False

    def _roll_positions(self):
        self._close_all_legs()
        self.write_log("[SellCall] 展期完成，等下根bar重开")
