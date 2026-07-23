# -*- coding: utf-8 -*-
"""订单管理（生命周期、超时自动撤单）"""
import time
import logging
from typing import Dict, Optional
from vnpy.trader.constant import Status
from vnpy.trader.object import OrderData, TradeData

logger = logging.getLogger("core.order_manager")

class OrderManager:
    """订单生命周期管理"""

    def __init__(self, timeout_seconds: float = 30.0):
        self._orders: Dict[str, OrderData] = {}
        self._trades: Dict[str, TradeData] = {}
        self._timeout = timeout_seconds
        self._lock = __import__("threading").Lock()

    def on_order(self, order: OrderData):
        """订单状态更新"""
        with self._lock:
            self._orders[order.orderid] = order
            if order.status in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
                logger.info(f"[Order] {order.orderid} 终态: {order.status.value}")

    def on_trade(self, trade: TradeData):
        """成交记录"""
        with self._lock:
            self._trades[trade.tradeid] = trade
            logger.info(f"[Trade] {trade.tradeid} {trade.direction.value} "
                        f"{trade.volume}手 @ {trade.price:.2f}")

    def check_timeout(self, adapter) -> int:
        """
        检查超时订单并撤单
        :param adapter: vnpy adapter（需有 cancel_order 方法）
        :return: 撤单数
        """
        now = time.time()
        cancelled = 0
        with self._lock:
            for oid, order in list(self._orders.items()):
                if order.status in (Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED):
                    create_time = getattr(order, "create_time", None)
                    age = 0
                    if create_time:
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(create_time, "%Y-%m-%d %H:%M:%S")
                            age = (datetime.now() - dt).total_seconds()
                        except Exception:
                            age = 0
                    else:
                        age = now  # fallback
                    if age >= self._timeout:
                        try:
                            adapter.cancel_order(oid)
                            cancelled += 1
                            logger.warning(f"[Order] 超时撤单: {oid} (age={age:.0f}s)")
                        except Exception as e:
                            logger.error(f"[Order] 撤单失败 {oid}: {e}")
        return cancelled

    def get_active_orders(self) -> Dict[str, OrderData]:
        """获取所有未完结订单"""
        with self._lock:
            return {k: v for k, v in self._orders.items()
                    if v.status in (Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED)}

    def get_today_trades(self) -> list:
        """获取今日成交"""
        from datetime import datetime, date
        today = date.today()
        with self._lock:
            return [t for t in self._trades.values()
                    if datetime.strptime(t.datetime, "%Y-%m-%d %H:%M:%S").date() == today]

    def clear(self):
        """清空（新交易日开始时调用）"""
        with self._lock:
            self._orders.clear()
            self._trades.clear()
