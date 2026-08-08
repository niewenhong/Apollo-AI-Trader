# -*- coding: utf-8 -*-
"""
core/risk_manager.py - RiskManager v3.8.2
====================================
- on_strategy_deployed 真实实现（登记活跃策略 + 同市场冲突检测）
- v3.8.2: 防御性检查，base/class_name 为空时跳过冲突检测
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("RiskManager")


class RiskManager:
    """风险管理器 v3.8.2"""

    def __init__(self, config: Optional[dict] = None, db=None):
        self.config = config or {}
        self.db = db

        # 活跃策略登记 + 锁
        self._active_strategies: Dict[str, dict] = {}
        self._lock = threading.Lock()

        # 风控参数
        risk_cfg = self.config.get("risk", {})
        self.max_position_pct = risk_cfg.get("max_position_pct", 0.2)
        self.max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 0.05)
        self.max_leverage = risk_cfg.get("max_leverage", 3.0)
        self.min_margin_ratio = risk_cfg.get("min_margin_ratio", 0.25)
        self.max_orders_per_minute = risk_cfg.get("max_orders_per_minute", 10)
        self.cooldown_seconds = risk_cfg.get("cooldown_seconds", 60)

        # 运行状态
        self._daily_pnl = 0.0
        self._daily_start_equity = 0.0
        self._order_count = 0
        self._last_order_time = datetime.now()
        self._cooldown_until = datetime.now()

        logger.info(
            f"[RiskManager] ✅ 初始化完成 | "
            f"max_pos={self.max_position_pct*100:.0f}% | "
            f"max_loss={self.max_daily_loss_pct*100:.1f}% | "
            f"max_lev={self.max_leverage}x"
        )

    # ==================== 策略部署回调 ====================

    def on_strategy_deployed(self, strategy_name: str, **kwargs):
        """
        v3.8.2: 真实实现
        - 登记活跃策略
        - 检测同市场内同底层+同策略类冲突
        - 防御：base 或 class_name 为空时跳过冲突检测
        """
        vt_symbol = kwargs.get("vt_symbol", "")
        class_name = kwargs.get("class_name", "")
        market = kwargs.get("market", "US")

        with self._lock:
            # 提取底层 symbol
            base = vt_symbol.split('.')[0] if '.' in vt_symbol else vt_symbol

            # ★ 防御：参数缺失时跳过冲突检测，仅登记
            if not base or not class_name:
                logger.warning(
                    f"[Risk] ⚠️ {strategy_name} 缺少关键参数，跳过冲突检测 "
                    f"(vt_symbol='{vt_symbol}', class_name='{class_name}', market='{market}')"
                )
                self._active_strategies[strategy_name] = {
                    "vt_symbol": vt_symbol,
                    "class_name": class_name,
                    "market": market,
                    "base": base,
                    "deployed_at": datetime.now(),
                }
                logger.info(
                    f"[Risk] ✅ 策略已登记（跳过冲突检测）: {strategy_name} "
                    f"({vt_symbol}) [{market}]"
                )
                return

            # 冲突检测：同市场 + 同底层 + 同策略类
            for existing_name, info in list(self._active_strategies.items()):
                if (info.get("market") == market and
                    info.get("base") == base and
                    info.get("class_name") == class_name and
                    existing_name != strategy_name):
                    logger.warning(
                        f"[Risk] ⚠️ 冲突: {strategy_name} 与 {existing_name} "
                        f"均为 {market}/{base}/{class_name}"
                    )

            # 登记
            self._active_strategies[strategy_name] = {
                "vt_symbol": vt_symbol,
                "class_name": class_name,
                "market": market,
                "base": base,
                "deployed_at": datetime.now(),
            }

        logger.info(
            f"[Risk] ✅ 策略已登记: {strategy_name} ({vt_symbol}) [{market}]"
        )

    def on_strategy_removed(self, strategy_name: str, **kwargs):
        """策略移除时清理登记"""
        with self._lock:
            self._active_strategies.pop(strategy_name, None)
        logger.info(f"[Risk] 策略已移除: {strategy_name}")

    # ==================== 风控检查接口 ====================

    def check_pre_trade(self, strategy_name: str, symbol: str,
                        side: str, quantity: float, price: float,
                        equity: float) -> Tuple[bool, str]:
        """交易前风控检查，返回 (是否允许, 原因)"""
        # 1. 冷却期检查
        if datetime.now() < self._cooldown_until:
            remaining = int((self._cooldown_until - datetime.now()).total_seconds())
            return False, f"冷却期中，剩余 {remaining}s"

        # 2. 单笔金额不超过总资金的 max_position_pct
        order_value = quantity * price
        max_value = equity * self.max_position_pct
        if order_value > max_value:
            return False, f"单笔金额 {order_value:.0f} 超过限制 {max_value:.0f}"

        # 3. 当日亏损检查
        if self._daily_start_equity > 0:
            loss_limit = self._daily_start_equity * self.max_daily_loss_pct
            if self._daily_pnl < -loss_limit:
                return False, f"触及日亏损上限 {self.max_daily_loss_pct*100:.1f}%"

        # 4. 频率限制
        now = datetime.now()
        if (now - self._last_order_time).total_seconds() < 60:
            self._order_count += 1
            if self._order_count > self.max_orders_per_minute:
                return False, f"订单频率超限 {self.max_orders_per_minute}/min"
        else:
            self._order_count = 1
        self._last_order_time = now

        return True, "OK"

    def report_trade(self, strategy_name: str, pnl: float):
        """报告交易结果，更新当日盈亏"""
        self._daily_pnl += pnl
        logger.debug(
            f"[Risk] {strategy_name} 交易PnL={pnl:.2f}, "
            f"当日累计={self._daily_pnl:.2f}"
        )

    def check_post_trade(self, strategy_name: str, equity: float) -> Tuple[bool, str]:
        """交易后检查"""
        if self._daily_start_equity > 0:
            loss_pct = abs(self._daily_pnl) / self._daily_start_equity
            if loss_pct >= self.max_daily_loss_pct:
                self._cooldown_until = datetime.now() + timedelta(seconds=self.cooldown_seconds)
                return False, f"触发冷却 {self.cooldown_seconds}s (日亏损 {loss_pct*100:.1f}%)"
        return True, "OK"

    def reset_daily(self, equity: float):
        """每日重置"""
        self._daily_pnl = 0.0
        self._daily_start_equity = equity
        self._order_count = 0
        logger.info(f"[Risk] 每日重置 | 起始资金={equity:.0f}")

    # ==================== 查询接口 ====================

    def get_active_strategies(self) -> dict:
        with self._lock:
            return dict(self._active_strategies)

    def get_active_count(self) -> int:
        with self._lock:
            return len(self._active_strategies)

    def get_status(self) -> dict:
        return {
            "active_strategies": self.get_active_count(),
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": self.max_daily_loss_pct * 100,
            "cooldown_active": datetime.now() < self._cooldown_until,
            "orders_this_minute": self._order_count,
        }

    def set_daily_loss_limit(self, pct: float):
        self.max_daily_loss_pct = pct
        logger.info(f"[Risk] 日亏损上限更新: {pct*100:.1f}%")

    def emergency_stop(self, reason: str = "手动触发"):
        """紧急停止所有交易"""
        self._cooldown_until = datetime.now() + timedelta(days=1)
        logger.error(f"[Risk] 🚨 紧急停止: {reason}")
        if self.db:
            try:
                self.db.log_risk_event("EMERGENCY_STOP", reason)
            except Exception:
                pass
        return True