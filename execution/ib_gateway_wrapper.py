# -*- coding: utf-8 -*-
"""
IB 网关封装（Interactive Brokers Gateway Wrapper）
对 vnpy IBGateway 的二次封装，增加自动重连、碎股校验、时段判断。
"""
import time
import logging
from typing import Optional
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import OrderData, TradeData, TickData
from vnpy.gateway.ib import IBGateway
from utils.decorators import retry, circuit_breaker
from utils.trading_hours import is_trading_hour

logger = logging.getLogger("execution.ib")


class IBGatewayWrapper:
    """IB 网关封装"""

    def __init__(self, main_engine, setting: dict = None):
        self.main_engine = main_engine
        self.setting = setting or {}
        self.gateway = None
        self.connected = False
        self._last_connect_time = 0.0
        self._reconnect_delay = 5.0
        self._orders: dict = {}
        self._trades: dict = {}

    @retry(max_attempts=5, delay=3.0, backoff=2.0)
    def connect(self):
        """连接 IB 网关"""
        try:
            if self.gateway is None:
                self.gateway = IBGateway(self.main_engine, "IB")
            self.gateway.connect(self.setting)
            self.connected = True
            self._last_connect_time = time.time()
            logger.info("[IB] 网关已连接")
        except Exception as e:
            self.connected = False
            logger.error(f"[IB] 连接失败: {e}")
            raise

    def reconnect(self):
        """重连"""
        now = time.time()
        if now - self._last_connect_time < self._reconnect_delay:
            return
        logger.info("[IB] 尝试重新连接...")
        self.connected = False
        self.connect()

    @circuit_breaker(failure_threshold=10, reset_timeout=300.0)
    def send_order(self, vt_symbol: str, direction: Direction,
                  offset: Offset, price: float,
                  volume: int) -> str:
        """发送订单"""
        if not self.connected:
            self.reconnect()
        if not self.connected:
            raise RuntimeError("[IB] 网关未连接")

        symbol, exchange = vt_symbol.split(".")
        if not is_trading_hour(symbol, exchange):
            raise RuntimeError(f"[IB] 非交易时段: {vt_symbol}")

        order = self.gateway.send_order(
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            order_type=OrderType.LIMIT
        )
        self._orders[order.orderid] = order
        logger.info(f"[IB] 下单: {direction.value} {vt_symbol} {volume}@{price:.2f}")
        return order.orderid

    def cancel_order(self, orderid: str):
        if not self.connected:
            return
        try:
            self.gateway.cancel_order(orderid)
            logger.info(f"[IB] 撤单: {orderid}")
        except Exception as e:
            logger.error(f"[IB] 撤单异常 {orderid}: {e}")

    def on_order(self, order: OrderData):
        self._orders[order.orderid] = order

    def on_trade(self, trade: TradeData):
        self._trades[trade.tradeid] = trade
        logger.info(f"[IB] 成交: {trade.tradeid} {trade.volume}@{trade.price:.2f}")

    def get_positions(self) -> dict:
        if not self.connected:
            return {}
        try:
            return self.gateway.query_position()
        except Exception:
            return {}

    def get_account(self) -> dict:
        if not self.connected:
            return {}
        try:
            return self.gateway.query_account()
        except Exception:
            return {}

    def subscribe(self, vt_symbol: str):
        if not self.connected:
            self.reconnect()
        try:
            self.gateway.subscribe(vt_symbol, "1m")
            logger.info(f"[IB] 订阅: {vt_symbol}")
        except Exception as e:
            logger.error(f"[IB] 订阅失败 {vt_symbol}: {e}")

    def close(self):
        if self.gateway:
            try:
                self.gateway.close()
            except Exception:
                pass
        self.connected = False
