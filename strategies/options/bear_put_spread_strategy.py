# -*- coding: utf-8 -*-
"""
strategies/options/bear_put_spread_strategy.py - Apollo-AI-Trader v3.1.1
Bear Put Spread：买高K Put + 卖低K Put，温和看跌

v3.1.1 变更：
- 新增 on_init 实现（加载历史K线）
- 复用基类 _select_contracts 做流动性过滤
- 复用基类 _open_spread 做价差开仓（含回滚）
- 增加 _stopped 检查
- 增加初始化重试上限（max_retries=5）
"""
from vnpy.trader.object import BarData, Direction, Offset
from vnpy.trader.constant import Interval
from strategies.options.base_option_strategy import BaseOptionStrategy


class BearPutSpreadStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "3.1.1"

    delta_long         = -0.35
    delta_short        = -0.15
    delta_tolerance    = 0.15
    min_days_to_expire = 14
    max_days_to_expire = 45
    min_credit_ratio   = 0.30
    min_net_debit_pct  = 0.005
    rolling_days       = 7
    max_positions      = 3
    adx_downtrend_min  = 18
    ema_fast_period    = 5
    ema_slow_period    = 20
    min_oi             = 50
    min_volume         = 10
    expire_close_days   = 2

    parameters = [
        "delta_long", "delta_short", "delta_tolerance",
        "min_days_to_expire", "max_days_to_expire",
        "min_credit_ratio", "min_net_debit_pct",
        "rolling_days", "max_positions",
        "adx_downtrend_min", "ema_fast_period", "ema_slow_period",
        "min_oi", "min_volume", "expire_close_days",
    ]
    variables = ["net_premium", "max_loss", "max_profit", "pnl",
                 "legs", "regime_label", "last_adx"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.last_adx = 0.0
        self._rolling = False
        self._init_retry_count = 0
        for param in self.parameters:
            if not hasattr(self, param):
                setattr(self, param, getattr(type(self), param, None))

    # ── 初始化（实现抽象方法） ──────────────────
    def on_init(self):
        """策略初始化：加载历史K线"""
        self.write_log("[BPS] on_init 开始，加载历史K线")
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[BPS] load_bar 异常: %s" % e)
        self._init_retry_count = 0
        self.write_log("[BPS] on_init 完成")

    # ── 5分钟K线：趋势过滤 ────────────────────────
    def _on_5m_bar_impl(self, bar: BarData):
        if self._stopped:
            return
        self.last_adx = getattr(self, "_adx_5m", 0.0)
        ema_fast = getattr(self, "_ema_5m_fast", None)
        ema_slow = getattr(self, "_ema_5m_slow", None)
        if ema_fast is None or ema_slow is None:
            return
        if ema_fast >= ema_slow:
            return
        if self.last_adx < self.adx_downtrend_min:
            return
        if not self.legs and not self._rolling:
            self._find_spread(bar)

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
            for leg in self.legs.values():
                dte = leg.get("days_to_expire", 999)
                if dte <= self.rolling_days:
                    self.write_log("[BPS] 临近到期%d天，展期" % dte)
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
                self._find_spread(bar)

    # ── 选合约 ────────────────────────────────────
    def _find_spread(self, bar: BarData):
        if self._init_retry_count >= 5:
            self.write_log("[BPS] 初始化重试已达上限，跳过本次开仓")
            return
        self._init_retry_count += 1

        code = self._to_futu_code()
        if not code:
            return

        chain = self._query_full_chain(code)
        if chain is None:
            self.write_log("[BPS] %s 期权链为空" % code)
            return

        puts = self._select_contracts(chain, "put",
                                       min_days=self.min_days_to_expire,
                                       max_days=self.max_days_to_expire)
        if len(puts) < 2:
            self.write_log("[BPS] Put 合约不足2条 (got %d)" % len(puts))
            return

        long_cands = [p for p in puts
                      if abs(abs(p.get("delta", 0)) - abs(self.delta_long)) <= self.delta_tolerance
                      and p.get("oi", 0) >= self.min_oi
                      and p.get("volume", 0) >= self.min_volume]
        short_cands = [p for p in puts
                       if abs(abs(p.get("delta", 0)) - abs(self.delta_short)) <= self.delta_tolerance
                       and p.get("oi", 0) >= self.min_oi
                       and p.get("volume", 0) >= self.min_volume]

        if not long_cands or not short_cands:
            self.write_log("[BPS] 严格delta筛选无结果，放宽")
            long_cands = sorted(puts,
                                key=lambda p: abs(abs(p.get("delta", 0)) - abs(self.delta_long)))[:5]
            short_cands = sorted(puts,
                                 key=lambda p: abs(abs(p.get("delta", 0)) - abs(self.delta_short)))[:5]

        if not long_cands or not short_cands:
            self.write_log("[BPS] 无合适合约")
            return

        best = None
        for lc in long_cands:
            long_p = lc.get("premium", 0)
            if long_p <= 0:
                continue
            for sc in short_cands:
                if sc.get("strike_price", 0) >= lc.get("strike_price", 0):
                    continue
                short_p = sc.get("premium", 0)
                if short_p <= 0:
                    continue
                credit = short_p / long_p
                if credit < self.min_credit_ratio:
                    continue
                width = lc["strike_price"] - sc["strike_price"]
                net = long_p - short_p
                if net >= width * (1 - self.min_net_debit_pct):
                    continue
                if best is None or net < best[2]:
                    best = (lc, sc, net, width)

        if not best:
            self.write_log("[BPS] 无合适价差组合")
            return

        long_leg, short_leg, net, width = best
        long_leg = dict(long_leg)
        short_leg = dict(short_leg)
        long_leg["name"] = "bps_long"
        short_leg["name"] = "bps_short"

        ok = self._open_spread(long_leg, short_leg)
        if ok:
            self.max_loss = net * 100
            self.max_profit = (width - net) * 100
            self.pnl = self._estimate_pnl()
            self.write_log("[BPS] 开仓成功 long@%.0f short@%.0f "
                           "net=%.2f width=%.0f "
                           "max_loss=%.0f max_profit=%.0f" %
                           (long_leg['strike_price'], short_leg['strike_price'],
                            net, width, self.max_loss, self.max_profit))
        else:
            self.write_log("[BPS] 价差开仓失败")

    # ── Tick 退出 ────────────────────────────────────
    def _check_tick_exit(self, bar: BarData = None):
        if len(self.legs) < 2:
            return
        cur_pnl = self._estimate_pnl()
        cost = abs(self.max_loss) + 0.01
        if cost <= 0:
            return
        if cur_pnl < -cost * 0.8:
            self.write_log("[BPS] 止损 pnl=%.0f cost=%.0f" % (cur_pnl, cost))
            self._close_all_legs()
            self._rolling = False
        elif cur_pnl > self.max_profit * 0.7:
            self.write_log("[BPS] 止盈 pnl=%.0f target=%.0f" % (cur_pnl, self.max_profit))
            self._close_all_legs()
            self._rolling = False

    def _roll_positions(self):
        self._close_all_legs()
        self.write_log("[BPS] 展期完成，等下根bar重开")
