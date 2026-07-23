# -*- coding: utf-8 -*-
"""风控中心（日内亏损熔断、仓位上限、下单频率、保证金检查）"""
import time
import json
import os
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("core.risk_manager")

class RiskManager:
    """风控中心（线程安全）"""

    def __init__(self, config_path: str = "config/risk_config.json"):
        self.config = self._load(config_path)
        self._daily_start_equity: float = 0.0
        self._daily_loss: float = 0.0
        self._order_count: int = 0
        self._order_window_start: float = time.time()
        self._breached: bool = False
        self._lock = __import__("threading").Lock()

    def _load(self, path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return {
            "max_daily_loss_pct": 2.0,
            "max_position_pct": 50.0,
            "max_single_order_pct": 10.0,
            "max_orders_per_minute": 10,
            "max_drawdown_pct": 15.0,
            "circuit_breaker": {"enabled": True, "daily_loss_limit_pct": 3.0, "action": "stop_all"}
        }

    def reset_daily(self, start_equity: float):
        """每个交易日开盘时调用"""
        with self._lock:
            self._daily_start_equity = start_equity
            self._daily_loss = 0.0
            self._order_count = 0
            self._order_window_start = time.time()
            self._breached = False
            logger.info(f"[Risk] 日重置，起始净值: {start_equity:.2f}")

    def update_equity(self, current_equity: float):
        """每次持仓/价格变动后更新"""
        if self._daily_start_equity <= 0:
            self._daily_start_equity = current_equity
            return
        self._daily_loss = self._daily_start_equity - current_equity

    def check(self, symbol: str, current_price: float,
              current_pos: int, target_pos: int,
              account_equity: float) -> bool:
        """
        下单前风控检查
        :return: True=允许下单, False=拒绝
        """
        with self._lock:
            if self._breached:
                logger.warning("[Risk] 熔断已触发，拒绝所有下单")
                return False

            # 1. 日内亏损检查
            if self._daily_start_equity > 0:
                daily_loss_pct = (self._daily_loss / self._daily_start_equity) * 100.0
                limit = self.config.get("max_daily_loss_pct", 2.0)
                if daily_loss_pct >= limit:
                    logger.error(f"[Risk] 日内亏损 {daily_loss_pct:.2f}% >= {limit}%，触发熔断")
                    self._trigger_circuit_breaker("daily_loss_exceeded")
                    return False

            # 2. 仓位上限检查
            max_pos_pct = self.config.get("max_position_pct", 50.0)
            diff = abs(target_pos - current_pos)
            order_value = diff * current_price
            pos_pct = (order_value / account_equity) * 100.0 if account_equity > 0 else 0
            if pos_pct > max_pos_pct:
                logger.warning(f"[Risk] 仓位 {pos_pct:.1f}% > {max_pos_pct}%，拒绝")
                return False

            # 3. 单笔上限
            max_single = self.config.get("max_single_order_pct", 10.0)
            if pos_pct > max_single:
                logger.warning(f"[Risk] 单笔 {pos_pct:.1f}% > {max_single}%，拒绝")
                return False

            # 4. 下单频率
            now = time.time()
            if now - self._order_window_start > 60:
                self._order_window_start = now
                self._order_count = 0
            self._order_count += 1
            max_freq = self.config.get("max_orders_per_minute", 10)
            if self._order_count > max_freq:
                logger.warning(f"[Risk] 下单频率 {self._order_count} > {max_freq}/min，拒绝")
                return False

            return True

    def check_knockout(self, symbol: str, current_price: float,
                        knockout_price: float, is_call: bool = False) -> bool:
        """检查牛熊证/涡轮是否触及收回价"""
        buffer_pct = self.config.get("cbbc_knockout_buffer_pct", 2.0)
        if is_call:
            threshold = knockout_price * (1 + buffer_pct / 100.0)
            if current_price >= threshold:
                logger.warning(f"[Risk] {symbol} 接近收回价 {knockout_price}，建议平仓")
                return True
        else:
            threshold = knockout_price * (1 - buffer_pct / 100.0)
            if current_price <= threshold:
                logger.warning(f"[Risk] {symbol} 接近收回价 {knockout_price}，建议平仓")
                return True
        return False

    def _trigger_circuit_breaker(self, reason: str):
        """触发熔断"""
        self._breached = True
        logger.critical(f"[Risk] 熔断触发: {reason}")
        # 发布事件（由 monitoring 模块监听处理）
        try:
            from core.event_bus import EventBus, Events
            EventBus().publish(Events.RISK_BREACH, {"reason": reason})
        except Exception:
            pass

    @property
    def is_breached(self) -> bool:
        return self._breached

    def get_status(self) -> dict:
        return {
            "breached": self._breached,
            "daily_loss": round(self._daily_loss, 2),
            "daily_loss_pct": round(
                (self._daily_loss / self._daily_start_equity) * 100.0
                if self._daily_start_equity > 0 else 0, 2
            ),
            "orders_this_minute": self._order_count,
            "start_equity": self._daily_start_equity
        }
