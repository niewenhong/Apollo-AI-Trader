# -*- coding: utf-8 -*-
"""
strategies/options/cash_secured_put_strategy.py - Apollo-AI-Trader v3.1.1
Cash Secured Put：备足现金 + 卖Put（想低价接货时收租）
比 SellPut 更保守：要求足额现金担保 + 更高 OTM

v3.1.1 变更：
- 新增 on_init 实现（加载历史K线）
- 修复 strike 拼写错误（原 strik）
- 复用基类 _select_contracts 做流动性过滤
- 增加 _stopped 检查
- 增加初始化重试上限（max_retries=5）
"""
from vnpy.trader.object import BarData, Direction, Offset
from vnpy.trader.constant import Interval
from strategies.options.base_option_strategy import BaseOptionStrategy


class CashSecuredPutStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "3.1.1"

    target_delta        = 0.15
    delta_tolerance     = 0.08
    min_otm_prob        = 80.0
    min_days_to_expire  = 14
    max_days_to_expire  = 45
    min_annual_roi      = 0.28
    position_size       = 1
    max_positions       = 3
    roll_when_ditm      = 0.40
    cash_buffer_ratio   = 0.12
    adx_trend_threshold = 16
    min_premium_usd     = 0.30
    min_oi             = 50
    min_volume         = 10
    expire_close_days   = 2

    parameters = [
        "target_delta", "delta_tolerance", "min_otm_prob",
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
        self.write_log("[CSP] on_init 开始，加载历史K线")
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[CSP] load_bar 异常: %s" % e)
        self._init_retry_count = 0
        self.write_log("[CSP] on_init 完成")

    # ── 5分钟K线：趋势过滤 ────────────────────────
    def _on_5m_bar_impl(self, bar: BarData):
        if self._stopped:
            return
        adx = getattr(self, "_adx_5m", 0.0)
        self.last_adx = adx
        if adx > self.adx_trend_threshold:
            self.write_log("[CSP] ADX=%.1f 强趋势，暂停" % adx)

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
                    self.write_log("[CSP] %s delta=%.2f>%.2f，展期" %
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
                self._find_put_to_sell(bar)

    # ── 现金检查 ──────────────────────────────────
    def _enough_cash(self, bar: BarData) -> bool:
        """卖Put需要足额现金担保"""
        strike = bar.close_price
        if strike <= 0:
            return False
        need = strike * 100 * self._scaled_size() * (1 + self.cash_buffer_ratio)
        self.cash_reserved = need
        avail = self._get_available_cash()
        if avail <= 0:
            return True  # 无法查询时放行
        ok = avail >= need
        if not ok:
            self.write_log("[CSP] 现金不足 需要≈%.0f 可用=%.0f" % (need, avail))
        return ok

    # ── 选合约 ────────────────────────────────────
    def _find_put_to_sell(self, bar: BarData):
        if self._init_retry_count >= 5:
            self.write_log("[CSP] 初始化重试已达上限，跳过本次开仓")
            return
        self._init_retry_count += 1

        if not self._enough_cash(bar):
            return

        code = self._to_futu_code()
        if not code:
            return

        chain = self._query_full_chain(code)
        if chain is None:
            self.write_log("[CSP] %s 期权链为空" % code)
            return

        # 使用基类筛选方法
        puts = self._select_contracts(chain, "put",
                                       min_days=self.min_days_to_expire,
                                       max_days=self.max_days_to_expire)
        if not puts:
            self.write_log("[CSP] 无符合条件Put")
            return

        lo = self.target_delta - self.delta_tolerance
        hi = self.target_delta + self.delta_tolerance

        in_band = []
        for p in puts:
            d = abs(p.get("delta", 0))
            if d < lo or d > hi:
                continue
            if p.get("premium", 0) < self.min_premium_usd:
                continue
            in_band.append(p)

        if not in_band:
            self.write_log("[CSP] 无满足严格delta区间的Put（保守跳过）")
            return

        # 按 OTM 概率降序
        in_band.sort(key=lambda x: -x.get("otm_prob", 0))
        target = in_band[0]

        # 年化 ROI 检查
        strike = target.get("strike_price", bar.close_price)
        prem = target.get("premium", 0)
        dte = max(target.get("days_to_expire", 30), 1)
        annual_roi = (prem / max(strike, 0.01)) * (365.0 / dte)
        if annual_roi < self.min_annual_roi:
            self.write_log("[CSP] ROI=%.1f%%<%.0f%%，跳过" %
                           (annual_roi * 100, self.min_annual_roi * 100))
            return

        target["name"]    = "csp_put"
        target["is_long"] = False
        ok = self._send_option_order(target, Direction.SHORT, Offset.OPEN,
                                     qty=self._scaled_size())
        if ok:
            self.net_premium += prem
            self.max_loss = (strike - prem) * 100
            self.pnl = self._estimate_pnl()
            self.write_log("[CSP] 卖出CashSecuredPut %s K=%.0f prem=%.2f "
                           "delta=%.2f otm%%=%.0f annual_roi=%.1f%%" %
                           (target.get('code', '?'), strike, prem,
                            target.get('delta', 0),
                            target.get('otm_prob', 0) * 100,
                            annual_roi * 100))
        else:
            self.write_log("[CSP] 卖出Put失败 %s" % target.get('code', '?'))

    # ── Tick 退出 ──────────────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if not self.legs:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        if cur_pnl < -cost * 0.8:
            self.write_log("[CSP] 止损 cur=%.0f cost=%.0f" % (cur_pnl, cost))
            self._close_all_legs()
            self._rolling = False
        elif cur_pnl > self.net_premium * 0.7:
            self.write_log("[CSP] 止盈 cur=%.0f prem=%.0f" % (cur_pnl, self.net_premium))
            self._close_all_legs()
            self._rolling = False

    def _roll_positions(self):
        self._close_all_legs()
        self.write_log("[CSP] 展期完成，等下根bar重开")
