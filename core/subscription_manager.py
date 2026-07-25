"""
core/subscription_manager.py — 全套订阅管理器 v2.7.0
功能：按市场路由(US/HK)订阅K_1M+K_5M+K_15M+K_60M，延迟反订阅，配额审计
版本：v2.7.0
变更：2026-07-26 修复 SessionType 导入错误，改用 Session.ALL / Session.NONE
"""

import time
from futu import RET_OK, SubType, Session


class SubscriptionManager:
    FULL_SUBSCRIPTION = [SubType.K_1M, SubType.K_5M, SubType.K_15M, SubType.K_60M]
    QUOTA_PER_SYMBOL = 4

    def __init__(self, max_quota=300):
        self.max_quota = max_quota
        self.subscribed = {}
        self.unsub_queue = []
        self.ctx_us = self.ctx_hk = None

    def set_contexts(self, ctx_us, ctx_hk):
        self.ctx_us = ctx_us
        self.ctx_hk = ctx_hk

    def _ctx(self, symbol):
        if symbol.startswith("HK."):
            return self.ctx_hk, Session.NONE
        elif symbol.startswith("US."):
            return self.ctx_us, Session.ALL
        raise ValueError(f"未知市场: {symbol}")

    def subscribe_all(self, symbol):
        if symbol in self.subscribed:
            return True
        if not self.ctx_us or not self.ctx_hk:
            print(f"❌ 行情上下文未就绪，无法订阅 {symbol}")
            return False

        current_usage = len(self.subscribed) * self.QUOTA_PER_SYMBOL
        if current_usage + self.QUOTA_PER_SYMBOL > self.max_quota:
            print(f"⛔ 配额不足！当前已用 {current_usage}/{self.max_quota}，放弃订阅 {symbol}")
            return False

        ctx, session = self._ctx(symbol)
        ret, err = ctx.subscribe([symbol], self.FULL_SUBSCRIPTION,
                                 session=session, subscribe_push=False)
        if ret == RET_OK:
            self.subscribed[symbol] = {"ctx": ctx, "sub_time": time.time()}
            new_usage = current_usage + self.QUOTA_PER_SYMBOL
            print(f"✅ 订阅成功: {symbol} (消耗额度: {self.QUOTA_PER_SYMBOL}, 总用量: {new_usage}/{self.max_quota})")
            return True
        else:
            print(f"❌ 订阅失败 {symbol}: {err}")
            return False

    def unsubscribe_all(self, symbol):
        info = self.subscribed.get(symbol)
        if not info:
            return
        if time.time() - info["sub_time"] >= 60:
            info["ctx"].unsubscribe([symbol], self.FULL_SUBSCRIPTION)
            self.subscribed.pop(symbol, None)
            print(f"✅ 反订阅: {symbol}")
        else:
            self.unsub_queue.append({
                "symbol": symbol,
                "ctx": info["ctx"],
                "execute_at": info["sub_time"] + 60
            })

    def process_unsub_queue(self):
        now = time.time()
        remain = []
        for item in self.unsub_queue:
            if now >= item["execute_at"]:
                item["ctx"].unsubscribe([item["symbol"]], self.FULL_SUBSCRIPTION)
                self.subscribed.pop(item["symbol"], None)
                print(f"✅ 延迟反订阅: {item['symbol']}")
            else:
                remain.append(item)
        self.unsub_queue = remain

    def audit_quota(self):
        used = len(self.subscribed) * self.QUOTA_PER_SYMBOL
        remaining = self.max_quota - used
        print(f"📊 额度: 已用 {used}/{self.max_quota}，剩余 {remaining}（共 {len(self.subscribed)} 只股票）")
        return used, remaining