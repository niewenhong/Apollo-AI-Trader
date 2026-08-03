"""
subscription_manager.py — 按需订阅管理 + 引用计数 + 配额审计
"""
import logging
from typing import Dict, List, Optional, Tuple
from futu import OpenQuoteContext, SubType, RET_OK, Session

log = logging.getLogger("SubManager")


class SubscriptionManager:
    def __init__(self, max_quota: int = 300):
        self.max_quota = max_quota
        self._ref_count: Dict[Tuple[str, str], int] = {}
        self._user_subs: Dict[str, Dict[str, List[str]]] = {}
        self._quote_ctx_us: Optional[OpenQuoteContext] = None
        self._quote_ctx_hk: Optional[OpenQuoteContext] = None
        self._gateways: Dict[str, object] = {}

    def set_contexts(self, us_ctx: OpenQuoteContext, hk_ctx: OpenQuoteContext):
        self._quote_ctx_us = us_ctx
        self._quote_ctx_hk = hk_ctx

    def register_gateway(self, gateway_name: str, gateway: object):
        self._gateways[gateway_name] = gateway
        if hasattr(gateway, 'quote_ctx'):
            if gateway_name == "FUTU_US":
                self._quote_ctx_us = gateway.quote_ctx
            elif gateway_name == "FUTU_HK":
                self._quote_ctx_hk = gateway.quote_ctx
        log.info(f"✅ 网关已注册: {gateway_name}")

    def subscribe(self, futu_symbol: str, subtypes: List[str], user: str = "strategy_engine") -> bool:
        return self.subscribe_demand(futu_symbol, user, subtypes)

    def subscribe_demand(self, symbol: str, user: str, subtypes: List[str]) -> bool:
        if user not in self._user_subs:
            self._user_subs[user] = {}
        newly_subscribed = []
        for st in subtypes:
            key = (symbol, st)
            is_new = key not in self._ref_count
            self._ref_count[key] = self._ref_count.get(key, 0) + 1
            if is_new:
                newly_subscribed.append(st)
            if symbol not in self._user_subs[user]:
                self._user_subs[user][symbol] = []
            if st not in self._user_subs[user][symbol]:
                self._user_subs[user][symbol].append(st)

        if newly_subscribed:
            ok = self._do_subscribe(symbol, newly_subscribed)
            if not ok:
                for st in newly_subscribed:
                    key = (symbol, st)
                    self._ref_count[key] -= 1
                    if self._ref_count[key] <= 0:
                        del self._ref_count[key]
                return False
        return True

    def unsubscribe(self, futu_symbol: str, subtypes: List[str], user: str = "strategy_engine") -> bool:
        return self.release_demand(futu_symbol, user, subtypes)

    def release_demand(self, symbol: str, user: str, subtypes: List[str]) -> bool:
        if user not in self._user_subs:
            return False
        to_unsub = []
        for st in subtypes:
            key = (symbol, st)
            if key in self._ref_count:
                self._ref_count[key] -= 1
                if self._ref_count[key] <= 0:
                    del self._ref_count[key]
                    to_unsub.append(st)
            if symbol in self._user_subs[user]:
                if st in self._user_subs[user][symbol]:
                    self._user_subs[user][symbol].remove(st)
        if to_unsub:
            self._do_unsubscribe(symbol, to_unsub)
        if symbol in self._user_subs[user] and not self._user_subs[user][symbol]:
            del self._user_subs[user][symbol]
        if not self._user_subs[user]:
            del self._user_subs[user]
        return True

    def _do_subscribe(self, symbol: str, subtypes: List[str]) -> bool:
        ctx = self._get_ctx_for(symbol)
        if ctx is None:
            log.error(f"❌ 无行情上下文: {symbol}")
            return False
        session = Session.ALL if symbol.startswith("US.") else Session.NONE
        sub_objs = [self._str_to_subtype(s) for s in subtypes]
        sub_objs = [s for s in sub_objs if s is not None]
        code, data = ctx.subscribe(symbol, sub_objs,
                                   is_first_push=True, subscribe_push=True,
                                   session=session)
        if code == RET_OK:
            log.info(f"✅ 订阅成功: {symbol} 类型={subtypes} (已用 {self.used()}/{self.max_quota})")
            return True
        else:
            log.error(f"❌ 订阅失败: {symbol} {subtypes} | {data}")
            return False

    def _do_unsubscribe(self, symbol: str, subtypes: List[str]) -> bool:
        ctx = self._get_ctx_for(symbol)
        if ctx is None: return False
        sub_objs = [self._str_to_subtype(s) for s in subtypes]
        sub_objs = [s for s in sub_objs if s is not None]
        code, data = ctx.unsubscribe(symbol, sub_objs)
        if code == RET_OK:
            log.info(f"✅ 退订成功: {symbol} 类型={subtypes} (已用 {self.used()}/{self.max_quota})")
        return code == RET_OK

    def used(self) -> int:
        return len(self._ref_count)

    def remaining(self) -> int:
        return self.max_quota - self.used()

    def audit_quota(self):
        print(f"📊 配额审计: 已用 {self.used()}/{self.max_quota}, 剩余 {self.remaining()}")
        stock_map: Dict[str, List[str]] = {}
        for sym, st in self._ref_count.keys():
            stock_map.setdefault(sym, []).append(st)
        print(f"📊 当前订阅股票数: {len(stock_map)}")
        for sym, sts in sorted(stock_map.items()):
            users = [u for u, d in self._user_subs.items() if sym in d]
            print(f"   {sym}: {sts} (用户: {set(users)})")

    def _get_ctx_for(self, symbol: str) -> Optional[OpenQuoteContext]:
        if symbol.startswith("US."): return self._quote_ctx_us
        elif symbol.startswith("HK."): return self._quote_ctx_hk
        return None

    @staticmethod
    def _str_to_subtype(s: str):
        mapping = {
            "QUOTE": SubType.QUOTE, "K_1M": SubType.K_1M,
            "K_5M": SubType.K_5M, "K_15M": SubType.K_15M,
            "K_30M": SubType.K_30M, "K_60M": SubType.K_60M,
            "K_DAY": SubType.K_DAY, "ORDER_BOOK": SubType.ORDER_BOOK,
            "TICKER": SubType.TICKER,
        }
        return mapping.get(s)

    def build_plan(self, config: dict, deployed_strategies: set = None) -> Dict[str, Dict[str, List[str]]]:
        from core.subscription_plan import build_subscription_plan
        return build_subscription_plan(config, deployed_strategies)

    def apply_plan(self, config: dict, deployed_strategies: set = None) -> bool:
        from core.subscription_plan import apply_subscription_plan
        return apply_subscription_plan(self, config, deployed_strategies)
