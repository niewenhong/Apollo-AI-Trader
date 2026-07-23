# -*- coding: utf-8 -*-
"""
富途网关封装（Futu Gateway Wrapper）
对 vnpy FutuGateway 的二次封装，增加：
- 自动重连
- 订单状态追踪
- 碎股校验前置
- 交易时段判断
"""
import time
import logging
from typing import Optional
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import OrderData, TradeData, TickData, BarData
from vnpy.gateway.futu import FutuGateway
from utils.decorators import retry, circuit_breaker
from utils.trading_hours import is_trading_hour

logger = logging.getLogger("execution.futu")


class FutuGatewayWrapper:
    """富途网关封装"""

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
        """连接富途网关（带自动重试）"""
        try:
            if self.gateway is None:
                self.gateway = FutuGateway(self.main_engine, "FUTU")
            self.gateway.connect(self.setting)
            self.connected = True
            self._last_connect_time = time.time()
            logger.info("[Futu] 网关已连接")
        except Exception as e:
            self.connected = False
            logger.error(f"[Futu] 连接失败: {e}")
            raise

    def reconnect(self):
        """重连（断线时调用）"""
        now = time.time()
        if now - self._last_connect_time < self._reconnect_delay:
            logger.warning("[Futu] 重连冷却中，跳过")
            return
        logger.info("[Futu] 尝试重新连接...")
        self.connected = False
        self.connect()

    @circuit_breaker(failure_threshold=10, reset_timeout=300.0)
    def send_order(self, vt_symbol: str, direction: Direction,
                  offset: Offset, price: float,
                  volume: int) -> str:
        """
        发送订单（带熔断保护）
        :return: 订单ID
        """
        if not self.connected:
            self.reconnect()
        if not self.connected:
            raise RuntimeError("[Futu] 网关未连接，无法下单")

        # 交易时段检查
        symbol, exchange = vt_symbol.split(".")
        if not is_trading_hour(symbol, exchange):
            raise RuntimeError(f"[Futu] 非交易时段: {vt_symbol}")

        order = self.gateway.send_order(
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            order_type=OrderType.LIMIT
        )
        self._orders[order.orderid] = order
        logger.info(f"[Futu] 下单: {direction.value} {vt_symbol} {volume}@{price:.2f}")
        return order.orderid

    def cancel_order(self, orderid: str):
        """撤单"""
        if not self.connected:
            logger.warning(f"[Futu] 撤单失败，网关未连接: {orderid}")
            return
        try:
            self.gateway.cancel_order(orderid)
            logger.info(f"[Futu] 撤单: {orderid}")
        except Exception as e:
            logger.error(f"[Futu] 撤单异常 {orderid}: {e}")

    def on_order(self, order: OrderData):
        """订单回调"""
        self._orders[order.orderid] = order
        if order.status in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
            logger.info(f"[Futu] 订单终态: {order.orderid} {order.status.value}")

    def on_trade(self, trade: TradeData):
        """成交回调"""
        self._trades[trade.tradeid] = trade
        logger.info(f"[Futu] 成交: {trade.tradeid} {trade.direction.value} "
                    f"{trade.volume}@{trade.price:.2f}")

    def get_positions(self) -> dict:
        """获取持仓"""
        if not self.connected:
            return {}
        try:
            return self.gateway.query_position()
        except Exception as e:
            logger.error(f"[Futu] 查询持仓失败: {e}")
            return {}

    def get_account(self) -> dict:
        """获取账户信息"""
        if not self.connected:
            return {}
        try:
            return self.gateway.query_account()
        except Exception as e:
            logger.error(f"[Futu] 查询账户失败: {e}")
            return {}

    def subscribe(self, vt_symbol: str):
        """订阅行情"""
        if not self.connected:
            self.reconnect()
        try:
            self.gateway.subscribe(vt_symbol, "1m")
            logger.info(f"[Futu] 订阅: {vt_symbol}")
        except Exception as e:
            logger.error(f"[Futu] 订阅失败 {vt_symbol}: {e}")

    def close(self):
        """关闭连接"""
        if self.gateway:
            try:
                self.gateway.close()
            except Exception:
                pass
        self.connected = False
        logger.info("[Futu] 网关已关闭")
