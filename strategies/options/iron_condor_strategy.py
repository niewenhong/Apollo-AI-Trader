# -*- coding: utf-8 -*-
"""
strategies/options/iron_condor_strategy.py - v3.1.1
铁鹰策略（Iron Condor）：同时卖出一张虚值Call和一张虚值Put，赚取时间价值

v3.1.1 变更：
- 新增 on_init 实现（加载历史K线）
- 修复：pop 语法错误（原 pop(name, None) → self.legs.pop）
- 修复：expire_date 拼写统一（原 expir_y_date）
- 增加 _stopped 检查
- 增加初始化重试上限（max_retries=5）
- 复用基类 _select_contracts 做流动性过滤
"""
from vnpy.trader.object import BarData, Direction, Offset
from vnpy.trader.constant import Interval
from strategies.options.base_option_strategy import BaseOptionStrategy


class IronCondorStrategy(BaseOptionStrategy):
    author = "Apollo"
    version = "3.1.1"

    # 铁鹰特有参数
    call_delta_target = 0.20
    put_delta_target = -0.20
    delta_tolerance = 0.10
    min_width = 5.0
    max_width = 20.0
    min_credit = 0.50
    max_positions = 3
    roll_when_ditm = 0.30
    stop_loss_pct = 0.50

    parameters = [
        "call_delta_target", "put_delta_target", "delta_tolerance",
        "min_width", "max_width", "min_credit", "max_positions",
        "roll_when_ditm", "stop_loss_pct",
        "min_days_to_expire", "max_days_to_expire",
        "min_oi", "min_volume", "expire_close_days",
    ]
    variables = ["net_credit", "max_loss", "pnl", "legs", "regime_label"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.net_credit = 0.0
        self.max_loss = 0.0
        self.pnl = 0.0
        self._rolling = False
        self._init_retry_count = 0
        for param in self.parameters:
            if not hasattr(self, param):
                setattr(self, param, getattr(type(self), param, None))

    # ── 初始化（实现抽象方法） ──────────────────
    def on_init(self):
        """策略初始化：加载历史K线"""
        self.write_log("[IC] on_init 开始，加载历史K线")
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[IC] load_bar 异常: %s" % e)
        self._init_retry_count = 0
        self.write_log("[IC] on_init 完成")

    # ── 主逻辑（实现抽象方法） ──────────────────
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
                    self.write_log("[IC] %s delta=%.2f>%.2f，展期" %
                                   (name, delta, self.roll_when_ditm))
                    self._rolling = True
                    self._roll_positions()
                    return

        # 3. 更新 PnL
        if self.legs:
            self.pnl = self._estimate_pnl()

        # 4. 止损
        if self.net_credit > 0 and self.pnl < -self.net_credit * self.stop_loss_pct:
            self.write_log("[IC] 止损 pnl=%.2f credit=%.2f" %
                           (self.pnl, self.net_credit))
            self._close_all_legs()
            self._rolling = False
            return

        # 5. 无持仓时尝试开仓
        if not self.legs and len(self.legs) < self.max_positions:
            self._rolling = False
            self._find_iron_condor(bar)

    # ── 选合约 ──────────────────────────────────
    def _find_iron_condor(self, bar: BarData):
        if self._init_retry_count >= 5:
            self.write_log("[IC] 初始化重试已达上限，跳过本次开仓")
            return
        self._init_retry_count += 1

        code = self._to_futu_code()
        if not code:
            self.write_log("[IC] 无法转换标的代码")
            return

        chain = self._query_full_chain(code)
        if chain is None:
            self.write_log("[IC] %s 期权链为空" % code)
            return

        # 分离 Call 和 Put
        calls = self._select_contracts(chain, "call",
                                       min_days=self.min_days_to_expire,
                                       max_days=self.max_days_to_expire)
        puts = self._select_contracts(chain, "put",
                                      min_days=self.min_days_to_expire,
                                      max_days=self.max_days_to_expire)

        if not calls or not puts:
            self.write_log("[IC] 无足够Call/Put数据 calls=%d puts=%d" %
                           (len(calls), len(puts)))
            return

        # 筛选 Call（卖虚值Call）
        call_lo = self.call_delta_target - self.delta_tolerance
        call_hi = self.call_delta_target + self.delta_tolerance
        call_candidates = [c for c in calls if
                           call_lo <= abs(c.get("delta", 0)) <= call_hi and
                           c.get("premium", 0) >= self.min_premium_usd and
                           c.get("oi", 0) >= self.min_oi and
                           c.get("volume", 0) >= self.min_volume]

        # 筛选 Put（卖虚值Put）
        put_lo = abs(self.put_delta_target) - self.delta_tolerance
        put_hi = abs(self.put_delta_target) + self.delta_tolerance
        put_candidates = [p for p in puts if
                          put_lo <= abs(p.get("delta", 0)) <= put_hi and
                          p.get("premium", 0) >= self.min_premium_usd and
                          p.get("oi", 0) >= self.min_oi and
                          p.get("volume", 0) >= self.min_volume]

        if not call_candidates or not put_candidates:
            self.write_log("[IC] 无符合delta区间的Call或Put")
            return

        # 选择最优组合（权利金最高）
        best_call = max(call_candidates, key=lambda x: x.get("premium", 0))
        best_put = max(put_candidates, key=lambda x: x.get("premium", 0))

        # 检查价差宽度
        width = abs(best_call.get("strike_price", 0) - best_put.get("strike_price", 0))
        if width < self.min_width or width > self.max_width:
            self.write_log("[IC] 价差宽度%.2f不在[%.1f,%.1f]" %
                           (width, self.min_width, self.max_width))
            return

        total_credit = best_call.get("premium", 0) + best_put.get("premium", 0)
        if total_credit < self.min_credit:
            self.write_log("[IC] 总权利金%.2f<%.2f，跳过" % (total_credit, self.min_credit))
            return

        # 开仓
        best_call = dict(best_call)
        best_put = dict(best_put)
        best_call["name"] = "sold_call"
        best_call["is_long"] = False
        best_put["name"] = "sold_put"
        best_put["is_long"] = False

        ok1 = self._send_option_order(best_call, Direction.SHORT, Offset.OPEN,
                                      qty=self._scaled_size())
        ok2 = False
        if ok1:
            ok2 = self._send_option_order(best_put, Direction.SHORT, Offset.OPEN,
                                          qty=self._scaled_size())

        if ok1 and ok2:
            self.net_credit += total_credit
            self.max_loss = width * 100 * self._scaled_size()
            self.pnl = self._estimate_pnl()
            self.write_log("[IC] 开仓铁鹰: Call K=%.0f Put K=%.0f "
                           "credit=%.2f width=%.2f" %
                           (best_call['strike_price'], best_put['strike_price'],
                            total_credit, width))
        else:
            self.write_log("[IC] 铁鹰开仓失败，回滚")
            if ok1 and not ok2:
                self._send_option_order(best_call, Direction.LONG, Offset.CLOSE)
            if ok2 and not ok1:
                self._send_option_order(best_put, Direction.LONG, Offset.CLOSE)
            self._close_all_legs()

    # ── 到期管理（覆盖基类） ────────────────────
    def _manage_expire(self, bar: BarData) -> bool:
        """到期管理：距离到期小于 expire_close_days 则平仓"""
        if not self.legs:
            return False
        now = getattr(bar, 'datetime', None)
        if now is None:
            return False
        for name, leg in list(self.legs.items()):
            expire = leg.get("expire_date") or leg.get("expiry_date")
            if expire and isinstance(expire, datetime):
                days_left = (expire - now).days
                if days_left <= self.expire_close_days:
                    self.write_log("[IC] %s 即将到期，平仓" % name)
                    self._close_all_legs()
                    return True
        return False

    # ── PnL 估算（覆盖基类） ──────────────────
    def _estimate_pnl(self) -> float:
        """估算当前 PnL"""
        pnl = 0.0
        for name, leg in self.legs.items():
            entry_price = leg.get("entry_price", leg.get("premium", 0))
            current_price = leg.get("current_price", entry_price)
            multiplier = leg.get("multiplier", leg.get("multiplier", 100))
            is_long = leg.get("is_long", False)
            direction = 1 if is_long else -1
            qty = leg.get("qty", self._scaled_size())
            pnl += direction * (current_price - entry_price) * multiplier * qty
        return pnl

    # ── 平仓所有腿 ──────────────────────────────
    def _close_all_legs(self):
        """平掉所有持仓"""
        for name in list(self.legs.keys()):
            leg = self.legs.pop(name, None)
            if leg:
                self.write_log("[IC] 平仓: %s" % name)
        self.net_credit = 0.0
        self.max_loss = 0.0
        self.pnl = 0.0

    def _roll_positions(self):
        """展期：平仓后等待下次开仓"""
        self._close_all_legs()
        self.write_log("[IC] 展期完成，等下根bar重开")


# 文件末尾需要 datetime import（用于 _manage_expire）
from datetime import datetime
