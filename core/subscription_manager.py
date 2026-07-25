"""
subscription_manager.py — 全套订阅管理器 v2.7.0
- 每只标的订阅 K_1M+K_5M+K_15M+K_60M（4额度/只）
- 港股走HK链路，美股走US链路
- 延迟60秒反订阅（富途硬约束）
- 日线走历史接口（不占订阅额度）
- 配额审计
"""

import time
import logging
from futu import SubType, KLType, RET_OK, Session

logger = logging.getLogger(__name__)


class SubscriptionManager:
    FULL_SUBSCRIPTION = [SubType.K_1M, SubType.K_5M, SubType.K_15M, SubType.K_60M]
    QUOTA_PER_SYMBOL = 4

    def __init__(self, main_us, main_hk, max_quota=300):
        self.ctx_us = main_us.get_gateway("FUTU").quote_ctx
        self.ctx_hk = main_hk.get_gateway("FUTU").quote_ctx
        self.subscribed = {}
        self.max_quota = max_quota
        self.unsub_queue = []
        self.db = None  # 外部注入

    def _get_ctx(self, symbol):
        if symbol.startswith("HK."):
            return self.ctx_hk, Session.NONE
        elif symbol.startswith("US."):
            return self.ctx_us, Session.ALL
        raise ValueError(f"未知市场: {symbol}")

    def subscribe_all(self, symbol):
        if symbol in self.subscribed:
            return True
        used = len(self.subscribed) * self.QUOTA_PER_SYMBOL
        if used + self.QUOTA_PER_SYMBOL > self.max_quota:
            logger.warning(f"⚠️ 配额不足，无法订阅 {symbol}（已用{used}/{self.max_quota}）")
            return False
        ctx, session = self._get_ctx(symbol)
        ret, err = ctx.subscribe([symbol], self.FULL_SUBSCRIPTION, session=session)
        if ret == RET_OK:
            self.subscribed[symbol] = {"ctx": ctx, "sub_time": time.time()}
            logger.info(f"✅ 全套订阅: {symbol} (K_1M+5M+15M+60M)")
            return True
        logger.error(f"❌ 订阅失败 {symbol}: {err}")
        return False

    def unsubscribe_all(self, symbol):
        info = self.subscribed.get(symbol)
        if not info:
            return
        if time.time() - info["sub_time"] >= 60:
            info["ctx"].unsubscribe([symbol], self.FULL_SUBSCRIPTION)
            self.subscribed.pop(symbol, None)
            logger.info(f"✅ 反订阅: {symbol}")
        else:
            self.unsub_queue.append({"symbol": symbol, "ctx": info["ctx"],
                                    "execute_at": info["sub_time"] + 60})

    def process_unsub_queue(self):
        now = time.time()
        remain = []
        for item in self.unsub_queue:
            if now >= item["execute_at"]:
                item["ctx"].unsubscribe([item["symbol"]], self.FULL_SUBSCRIPTION)
                self.subscribed.pop(item["symbol"], None)
                logger.info(f"✅ 延迟反订阅: {item['symbol']}")
            else:
                remain.append(item)
        self.unsub_queue = remain

    def get_daily_bars(self, symbol, start_date, end_date):
        ctx, _ = self._get_ctx(symbol)
        ret, data, pk = ctx.request_history_kline(
            symbol, start=start_date, end=end_date,
            ktype=KLType.K_DAY, max_count=1000,
            session=Session.ALL if symbol.startswith("US.") else Session.NONE)
        if ret == RET_OK:
            if self.db:
                self.db.save_bars(symbol, "1d", data)
            logger.info(f"📈 日线 {symbol}: {len(data)}条")
            return data
        logger.error(f"日线失败 {symbol}: {data}")
        return None

    def audit_quota(self):
        used = len(self.subscribed) * self.QUOTA_PER_SYMBOL
        logger.info(f"📊 额度: 已用{used}/{self.max_quota} 剩余{self.max_quota-used} "
                    f"({len(self.subscribed)}只)")
        return used, self.max_quota - used
