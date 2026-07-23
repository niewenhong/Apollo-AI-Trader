"""
单市场切换器 — 只做一件事：按时间表启动/暂停本市场策略。
阶段：REVIEW → PREPARE → TRADE → REVIEW ...
回调签名统一：callback(label: str)

适配架构：单 MainEngine + 单 CtaStrategyApp + 双 Gateway (FUTU_US / FUTU_HK)
策略通过 strategy.current_market 区分所属市场。
"""

import time
import threading
import logging
from datetime import datetime, timezone

logger = logging.getLogger("MarketSwitcher")

PHASE_REVIEW  = "REVIEW"
PHASE_PREPARE = "PREPARE"
PHASE_TRADE   = "TRADE"


class MarketSwitcher:
    """
    cta_engine:  CtaStrategyApp 实例（vnpy_ctastrategy）
    market:      "US" 或 "HK"（本切换器负责的市场）
    schedule:    list of (start_h, start_m, end_h, end_m, phase, label)
    config:      config.json 的 dict
    """

    def __init__(self, cta_engine, market: str, schedule: list, config: dict):
        self.cta = cta_engine
        self.market = market
        self.schedule = schedule
        self.config = config
        self.interval = config.get("market_switch_interval", 30)

        self.current_phase = None
        self.current_label = None
        self.running = False
        self._thread = None

        # 回调（统一接收一个 str 参数）
        self.on_review:      callable = None
        self.on_prepare:     callable = None
        self.on_trade_start: callable = None
        self.on_trade_end:   callable = None

    # ── 公开接口 ──
    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"[MS:{self.market}] 已启动")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
        logger.info(f"[MS:{self.market}] 已停止")

    # ── 主循环 ──
    def _loop(self):
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                now_min = now.hour * 60 + now.minute

                entry = None
                for sh, sm, eh, em, phase, label in self.schedule:
                    if sh * 60 + sm <= now_min < eh * 60 + em:
                        # 延伸时段（暗盘）要看开关
                        if phase == PHASE_TRADE and label in ("HK_DARK",):
                            if not self._allowed_extended():
                                continue
                        entry = (phase, label)
                        break

                if entry is None:
                    time.sleep(self.interval)
                    continue

                phase, label = entry
                if phase != self.current_phase or label != self.current_label:
                    self._handle_change(phase, label)
                    self.current_phase = phase
                    self.current_label = label
            except Exception as e:
                logger.error(f"[MS:{self.market}] 异常: {e}")
            time.sleep(self.interval)

    def _handle_change(self, new_phase, new_label):
        # 离开旧交易阶段
        if self.current_phase == PHASE_TRADE and self.current_label:
            self._pause_market()
            if self.on_trade_end:
                try:
                    self.on_trade_end(self.current_label)
                except Exception as e:
                    logger.error(f"on_trade_end: {e}")

        if new_phase == PHASE_REVIEW:
            logger.info(f"[MS:{self.market}] 🔍 复盘: {new_label}")
            if self.on_review:
                try:
                    self.on_review(new_label or "")
                except Exception as e:
                    logger.error(f"on_review: {e}")

        elif new_phase == PHASE_PREPARE:
            logger.info(f"[MS:{self.market}] 🛠 准备: {new_label}")
            if self.on_prepare:
                try:
                    self.on_prepare(new_label or "")
                except Exception as e:
                    logger.error(f"on_prepare: {e}")

        elif new_phase == PHASE_TRADE:
            logger.info(f"[MS:{self.market}] ▶️ 交易: {new_label}")
            self._activate_market()
            if self.on_trade_start:
                try:
                    self.on_trade_start(new_label or "")
                except Exception as e:
                    logger.error(f"on_trade_start: {e}")

    # ── 延伸时段开关 ──
    def _allowed_extended(self) -> bool:
        if not self.config.get("allow_extended_hours", False):
            return False
        # 用决策引擎的盈利判断（如果已注入到 cta_engine）
        de = getattr(self.cta, "decision_engine", None)
        if de and hasattr(de, "is_profitable"):
            return de.is_profitable()
        return True

    # ── 策略过滤：只操作属于本市场的策略 ──
    def _market_strategies(self):
        """Yield (name, strategy) 仅属于 self.market 的策略"""
        for name, s in self.cta.strategies.items():
            m = getattr(s, "current_market", None) or getattr(s, "market", None)
            if m == self.market:
                yield name, s

    # ── 策略控制（只动本市场的） ──
    def _activate_market(self):
        for name, s in list(self._market_strategies()):
            try:
                if not getattr(s, "active", False):
                    self.cta.start_strategy(name)
                    logger.info(f"  ✅ 启动: {name}")
            except Exception as e:
                logger.error(f"  ❌ 启动 {name}: {e}")

    def _pause_market(self):
        for name, s in list(self._market_strategies()):
            try:
                if getattr(s, "active", False):
                    self.cta.stop_strategy(name)
                    logger.info(f"  ⏸ 暂停: {name}")
            except Exception as e:
                logger.error(f"  ❌ 暂停 {name}: {e}")
