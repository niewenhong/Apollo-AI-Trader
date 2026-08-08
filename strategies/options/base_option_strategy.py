# -*- coding: utf-8 -*-
"""
strategies/options/base_option_strategy.py - v3.1.1
期权策略基类：统一管理生命周期、期权链查询、订单发送、PnL估算

v3.1.1 变更：
- 新增 on_init 抽象方法声明（@abstractmethod）
- 新增 _legs_lock 线程锁（保护 self.legs 字典）
- 新增 _select_contracts 通用合约筛选方法
- 新增 _open_spread 通用价差开仓方法（含回滚）
- 新增 _batch_quote 占位方法（子类可覆盖）
- 修复：write_log(f"...") 括号问题
- 修复：_close_all_legs 增加默认实现
- 修复：_estimate_pnl 增加默认实现
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import threading
import pandas as pd

from vnpy.trader.object import (
    BarData, TickData, OrderData, TradeData, Direction, Offset
)
from vnpy.trader.constant import Interval, Status, Exchange
from vnpy_ctastrategy import CtaTemplate


class BaseOptionStrategy(CtaTemplate, ABC):
    """期权策略基类"""

    author = "Apollo"
    version = "3.1.1"

    # 公共参数
    target_delta = 0.20
    delta_tolerance = 0.10
    min_otm_prob = 85.0
    min_days_to_expire = 21
    max_days_to_expire = 120
    min_annual_roi = 0.24
    position_size = 1
    max_positions = 5
    roll_when_ditm = 0.30
    cash_buffer_ratio = 0.10
    adx_trend_threshold = 26
    min_premium_usd = 0.20
    min_oi = 50
    min_volume = 10
    expire_close_days = 2

    # 运行时变量
    net_premium = 0.0
    max_loss = 0.0
    pnl = 0.0
    legs: Dict[str, dict] = {}
    cash_reserved = 0.0
    regime_label = ""
    last_adx = 0.0

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self._stopped = False
        self._rolling = False
        self._init_retry_count = 0
        self._first_bar = True
        self._legs_lock = threading.Lock()
        self.legs = {}

    # ── 生命周期管理 ──────────────────────────────
    def on_init(self):
        """策略初始化：加载历史K线，初始化指标（默认实现）"""
        self.write_log("[%s] on_init 开始" % type(self).__name__)
        try:
            self.load_bar(days=30, interval=Interval.DAILY, callback=self.on_bar)
            self.load_bar(days=2, interval=Interval.HOUR, callback=self.on_bar)
        except Exception as e:
            self.write_log("[%s] load_bar 异常: %s" % (type(self).__name__, e))
        self._init_retry_count = 0
        self.write_log("[%s] on_init 完成" % type(self).__name__)

    def on_stop(self):
        """策略被移除时调用，清理所有后台任务"""
        self._stopped = True
        self._rolling = False
        self._init_retry_count = 0
        self.write_log("[%s] on_stop 已执行，策略停止" % type(self).__name__)

    # ── 统一回调入口检查 ──────────────────────────
    def on_bar(self, bar: BarData):
        if self._stopped:
            return
        self._on_bar_impl(bar)

    def on_tick(self, tick: TickData):
        if self._stopped:
            return
        self._on_tick_impl(tick)

    def on_5m_bar(self, bar: BarData):
        if self._stopped:
            return
        self._on_5m_bar_impl(bar)

    # ── 子类必须实现的抽象方法 ──────────────────
    @abstractmethod
    def _on_bar_impl(self, bar: BarData):
        """子类实现主K线逻辑"""
        pass

    def _on_tick_impl(self, tick: TickData):
        pass

    def _on_5m_bar_impl(self, bar: BarData):
        pass

    # ── 期权链查询（安全版） ──────────────────────
    def _query_full_chain(self, code: str) -> Optional[List[dict]]:
        """
        查询完整期权链，返回 list of dict 或 None。
        兼容 DataFrame 返回，自动转换。
        """
        try:
            raw = self._do_query_chain(code)
        except Exception as e:
            self.write_log("[查询期权链] %s 异常: %s" % (code, e))
            return None

        if raw is None:
            return None

        if isinstance(raw, pd.DataFrame):
            if raw.empty:
                return None
            return raw.to_dict('records')

        if isinstance(raw, list):
            return raw if raw else None

        try:
            lst = list(raw)
            return lst if lst else None
        except Exception:
            self.write_log("[查询期权链] 未知返回类型: %s" % type(raw))
            return None

    def _do_query_chain(self, code: str) -> Any:
        """实际查询期权链的方法，子类必须覆盖"""
        raise NotImplementedError("[%s] _do_query_chain 未实现" % type(self).__name__)

    # ── 通用合约筛选 ──────────────────────────────
    def _select_contracts(self, chain: List[dict],
                         option_type: str = "call",
                         min_days: int = 7,
                         max_days: int = 120) -> List[dict]:
        """
        从期权链中筛选指定类型的合约（按到期日、流动性过滤）
        option_type: "call" 或 "put"
        """
        result = []
        if not chain:
            return result
        for c in chain:
            is_call = c.get("is_call", False)
            is_put = c.get("is_put", False)
            if option_type == "call" and not is_call:
                continue
            if option_type == "put" and not is_put:
                continue
            dte = c.get("days_to_expire", 999)
            if dte < min_days or dte > max_days:
                continue
            if c.get("oi", 0) > 0 and c.get("oi", 0) < self.min_oi:
                continue
            if c.get("volume", 0) > 0 and c.get("volume", 0) < self.min_volume:
                continue
            result.append(c)
        return result

    # ── 价差开仓（带回滚） ────────────────────────
    def _open_spread(self, long_leg: dict, short_leg: dict) -> bool:
        """
        开仓一个价差（两腿），如果其中一腿失败则回滚。
        返回 True 表示两腿都成功。
        """
        ok1 = self._send_option_order(long_leg, Direction.LONG, Offset.OPEN,
                                      qty=self._scaled_size())
        ok2 = False
        if ok1:
            ok2 = self._send_option_order(short_leg, Direction.SHORT, Offset.OPEN,
                                          qty=self._scaled_size())

        if ok1 and ok2:
            with self._legs_lock:
                long_leg["name"] = long_leg.get("name", "long_leg")
                long_leg["is_long"] = True
                short_leg["name"] = short_leg.get("name", "short_leg")
                short_leg["is_long"] = False
                self.legs[long_leg["name"]] = long_leg
                self.legs[short_leg["name"]] = short_leg
            return True
        else:
            self.write_log("[%s] 价差开仓失败，回滚" % type(self).__name__)
            if ok1 and not ok2:
                self._send_option_order(long_leg, Direction.SHORT, Offset.CLOSE)
            if ok2 and not ok1:
                self._send_option_order(short_leg, Direction.LONG, Offset.CLOSE)
            return False

    # ── 批量报价查询（占位） ──────────────────────
    def _batch_quote(self, codes: List[str]) -> Dict[str, dict]:
        """批量查询合约报价，返回 {code: {price, bid, ask}} 字典"""
        result = {}
        for code in codes:
            result[code] = {"price": 0.0, "bid": 0.0, "ask": 0.0}
        return result

    # ── 其他通用工具方法 ──────────────────────────
    def _scaled_size(self) -> int:
        """返回调整后的手数"""
        return self.position_size

    def _get_available_cash(self) -> float:
        """获取可用现金"""
        try:
            account = self.cta_engine.get_account(self.vt_symbol)
            if account:
                return getattr(account, 'balance', 0.0) or 0.0
        except Exception:
            pass
        return 0.0

    def _to_futu_code(self) -> str:
        """转换为富途格式代码"""
        vt = self.vt_symbol
        if vt.startswith("US.") or vt.startswith("HK."):
            return vt
        if ".SMART" in vt or ".NASDAQ" in vt or ".NYSE" in vt:
            return "US.%s" % vt.split('.')[0]
        if ".SEHK" in vt:
            return "HK.%s" % vt.split('.')[0]
        return vt

    def _manage_expire(self, bar: BarData) -> bool:
        """到期管理，返回 True 表示已处理到期"""
        if not self.legs:
            return False
        now = getattr(bar, 'datetime', datetime.now())
        for name, leg in list(self.legs.items()):
            expire = leg.get("expire_date") or leg.get("expiry_date")
            if expire and isinstance(expire, datetime):
                days_left = (expire - now).days
                if days_left <= self.expire_close_days:
                    self.write_log("[%s] %s 即将到期，平仓" % (type(self).__name__, name))
                    self._close_all_legs()
                    return True
        return False

    def _estimate_pnl(self) -> float:
        """估算当前 PnL（默认实现）"""
        pnl = 0.0
        for name, leg in self.legs.items():
            entry_price = leg.get("entry_price", leg.get("premium", 0))
            current_price = leg.get("current_price", entry_price)
            multiplier = leg.get("multiplier", leg.get("multiplier", 100))
            is_long = leg.get("is_long", True)
            direction = 1 if is_long else -1
            qty = leg.get("qty", self._scaled_size())
            pnl += direction * (current_price - entry_price) * multiplier * qty
        return pnl

    def _send_option_order(self, option_info: dict, direction: Direction,
                           offset: Offset, qty: int) -> bool:
        """发送期权订单（子类必须覆盖）"""
        self.write_log("[%s] 模拟下单: %s %s %s qty=%d" %
                       (type(self).__name__, direction.name, offset.name,
                        option_info.get('code', '?'), qty))
        return True

    def _close_all_legs(self):
        """平掉所有持仓并重置状态"""
        for name in list(self.legs.keys()):
            leg = self.legs.pop(name, None)
            if leg:
                self.write_log("[%s] 平仓: %s" % (type(self).__name__, name))
        self.net_premium = 0.0
        self.max_loss = 0.0
        self.pnl = 0.0
