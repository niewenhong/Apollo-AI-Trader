# -*- coding: utf-8 -*-
"""
模拟执行器（用于回测 / 模拟盘）
模拟订单撮合、滑点、手续费
"""
import time
import uuid
import logging
from typing import List, Optional
from vnpy.trader.constant import Direction, Offset, Status, OrderType
from vnpy.trader.object import OrderData, TradeData

logger = logging.getLogger("execution.simulator")


class SimulatedOrder:
    """模拟订单"""
    def __init__(self, vt_symbol: str, direction: Direction,
                 offset: Offset, price: float, volume: int):
        self.orderid = f"SIM-{uuid.uuid4().hex[:8]}"
        self.vt_symbol = vt_symbol
        self.direction = direction
        self.offset = offset
        self.price = price
        self.volume = volume
        self.traded = 0
        self.status = Status.NOTTRADED
        self.create_time = time.time()


class Simulator:
    """撮合引擎（简化版）"""

    def __init__(self, slippage: float = 0.01, commission: float = 0.001):
        self.slippage = slippage
        self.commission = commission
        self._orders: dict = {}
        self._trades: dict = {}
        self._callbacks = {"order": [], "trade": []}

    def send_order(self, vt_symbol: str, direction: Direction,
                   offset: Offset, price: float,
                   volume: int) -> str:
        """模拟下单（立即部分/全部成交）"""
        order = SimulatedOrder(vt_symbol, direction, offset, price, volume)
        self._orders[order.orderid] = order

        # 模拟撮合（回测中由 Bar 价格驱动，此处立即成交）
        fill_price = price
        if direction == Direction.LONG:
            fill_price += self.slippage
        else:
            fill_price -= self.slippage

        trade = TradeData(
            tradeid=f"TR-{uuid.uuid4().hex[:8]}",
            orderid=order.orderid,
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=fill_price,
            volume=volume,
            datetime=time.time()
        )
        order.traded = volume
        order.status = Status.ALLTRADED
        self._trades[trade.tradeid] = trade

        # 回调
        for cb in self._callbacks["order"]:
            cb(order)
        for cb in self._callbacks["trade"]:
            cb(trade)

        logger.info(f"[Sim] 成交: {direction.value} {vt_symbol} {volume}@{fill_price:.4f}")
        return order.orderid

    def cancel_order(self, orderid: str):
        order = self._orders.get(orderid)
        if order and order.status == Status.NOTTRADED:
            order.status = Status.CANCELLED
            for cb in self._callbacks["order"]:
                cb(order)
            logger.info(f"[Sim] 撤单: {orderid}")

    def register_callback(self, event_type: str, callback):
        if event_type in self._callbacks:
            self._callbacks[event_type].append(callback)

    def get_open_orders(self) -> list:
        return [o for o in self._orders.values()
                if o.status in (Status.SUBMITTING, Status.NOTTRADED)]

    def clear(self):
        self._orders.clear()
        self._trades.clear()
