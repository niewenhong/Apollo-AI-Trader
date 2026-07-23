# -*- coding: utf-8 -*-
"""
策略基类（组合模式，内部持有 vnpy adapter）
支持：回测/实盘切换、调试开关、热配置、交易时段判断、风控前置、撤单
"""
import json
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import TickData, BarData, OrderData, TradeData
from vnpy.trader.utility import extract_vt_symbol

from core.config_loader import ConfigLoader
from core.risk_manager import RiskManager
from utils.trading_hours import is_trading_hour
from utils.logger import get_logger
from utils.shelly import round_to_lot

logger = get_logger("strategies.base")


class BaseStrategy(ABC):
    """策略基类，所有策略必须继承此类"""

    author = "Apollo"
    version = "2.2.0"
    strategy_name = ""  # 子类必须设置，与 config/strategies/ 下 json 对应

    # 默认参数（子类覆盖）
    fixed_size = 1
    price_offset = 0.003
    debug_mode = False
    dry_run = False
    backtest_mode = False

    def __init__(self, vnpy_adapter, settings: dict = None):
        self._adapter = vnpy_adapter
        self.vt_symbol = getattr(vnpy_adapter, "vt_symbol", "")
        if self.vt_symbol:
            self.symbol, self.exchange = extract_vt_symbol(self.vt_symbol)
        else:
            self.symbol, self.exchange = "", ""

        # 风控
        self.risk_manager = RiskManager()

        # 持仓
        self.pos = 0
        self.target_pos = 0

        # 热配置
        self._config_loader = ConfigLoader()
        self._load_config()

        # 交易时段
        self._in_trading_hour = False

        # 订单管理
        self.active_orders: Dict[str, OrderData] = {}

        # 应用外部参数
        if settings:
            for k, v in settings.items():
                setattr(self, k, v)

        # 子类初始化
        self.on_init()

    def _load_config(self):
        """从 config/strategies/{strategy_name}_config.json 加载"""
        if not self.strategy_name:
            return
        data = self._config_loader.load(self.strategy_name)
        for k, v in data.items():
            setattr(self, k, v)
        if data:
            logger.info(f"[{self.strategy_name}] 配置已加载")

    # ========== 子类必须实现 ==========
    @abstractmethod
    def on_init(self):
        """策略初始化"""
        pass

    @abstractmethod
    def calculate_signals(self, data) -> str:
        """
        根据 Tick 或 Bar 计算信号
        :return: "long" / "short" / "flat" / "hold"
        """
        pass

    @abstractmethod
    def get_target_position(self) -> int:
        """返回目标持仓（正数多/负数空/0平）"""
        pass

    # ========== 生命周期 ==========
    def on_start(self):
        self._load_config()
        logger.info(f"[{self.strategy_name}] 策略启动")

    def on_stop(self):
        self.cancel_all_orders()
        logger.info(f"[{self.strategy_name}] 策略停止")

    # ========== 行情回调 ==========
    def on_tick(self, tick: TickData):
        if not self._check_trading_hour(tick.datetime):
            return
        signal = self.calculate_signals(tick)
        self.target_pos = self.get_target_position()
        if self.debug_mode:
            logger.debug(f"[{self.strategy_name}] tick={tick.last_price} sig={signal} target={self.target_pos}")
        if not self.risk_manager.check(self.symbol, tick.last_price, self.pos, self.target_pos, 100000.0):
            return
        self._execute(signal)

    def on_bar(self, bar: BarData):
        if not self._check_trading_hour(bar.datetime):
            return
        signal = self.calculate_signals(bar)
        self.target_pos = self.get_target_position()
        if self.debug_mode:
            logger.info(f"[{self.strategy_name}] bar={bar.close_price} sig={signal} target={self.target_pos}")
        if not self.risk_manager.check(self.symbol, bar.close_price, self.pos, self.target_pos, 100000.0):
            return
        self._execute(signal)

    def on_order(self, order: OrderData):
        self.active_orders[order.orderid] = order
        if order.status in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
            self.active_orders.pop(order.orderid, None)
        logger.info(f"[{self.strategy_name}] 订单: {order.orderid} status={order.status.value}")

    def on_trade(self, trade: TradeData):
        self.pos += trade.volume if trade.direction == Direction.LONG else -trade.volume
        logger.info(f"[{self.strategy_name}] 成交: {trade.direction.value} {trade.volume}手 @ {trade.price:.2f}")

    # ========== 交易执行 ==========
    def _execute(self, signal: str):
        if self.dry_run:
            logger.info(f"[DRY] {self.strategy_name}: signal={signal} pos={self.pos} target={self.target_pos}")
            return
        diff = self.target_pos - self.pos
        if diff == 0:
            return
        price = self._get_order_price(diff)
        size = round_to_lot(abs(diff), self.fixed_size)
        if size <= 0:
            return
        if diff > 0:
            if self.pos <= 0:
                self._adapter.buy(price, size)
            else:
                self._adapter.sell(price, size)
        else:
            if self.pos >= 0:
                self._adapter.short(price, size)
            else:
                self._adapter.cover(price, size)

    def _get_order_price(self, diff: int) -> float:
        """子类覆盖：根据行情获取下单价格"""
        return 0.0

    # ========== 辅助方法 ==========
    def _check_trading_hour(self, dt: datetime) -> bool:
        if self.backtest_mode:
            return True
        if not dt:
            dt = datetime.now()
        return is_trading_hour(self.symbol, self.exchange, dt)

    def cancel_all_orders(self):
        for oid in list(self.active_orders.keys()):
            try:
                self._adapter.cancel_order(oid)
            except Exception as e:
                logger.error(f"撤单失败 {oid}: {e}")
        logger.info(f"[{self.strategy_name}] 已撤销所有未成交订单")

    def reload_config(self):
        self._load_config()
        logger.info(f"[{self.strategy_name}] 配置已热加载")

    def write_log(self, msg: str):
        """统一日志输出"""
        logger.info(f"[{self.strategy_name}] {msg}")
