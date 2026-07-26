"""
core/subscription_manager.py — 按需订阅管理器 v2.8.0
功能：
  - 按需订阅：策略声明所需数据类型，管理器按最小集合订阅
  - 引用计数：同一类型被多个策略使用时只订阅一次
  - 延迟反订阅（≥60秒），配额审计
  - 支持从富途查询当前订阅状态
"""
import time
from typing import Dict, List, Set, Optional, Any
from futu import RET_OK, SubType, Session


class SubscriptionManager:
    """按需订阅管理器
    
    配额规则（富途官方）：
    - 每只股票订阅一个类型 = 1 个额度
    - 取消订阅释放额度
    - 反订阅需等待至少 60 秒
    - 所有连接都反订阅后才释放额度
    """
    
    AVAILABLE_TYPES = {
        "QUOTE": SubType.QUOTE,
        "TICKER": SubType.TICKER,
        "K_1M": SubType.K_1M,
        "K_5M": SubType.K_5M,
        "K_15M": SubType.K_15M,
        "K_60M": SubType.K_60M,
        "K_DAY": SubType.K_DAY,
        "RT_DATA": SubType.RT_DATA,
        "ORDER_BOOK": SubType.ORDER_BOOK,
        "BROKER": SubType.BROKER,
    }
    
    def __init__(self, max_quota: int = 300):
        self.max_quota = max_quota
        self.ctx_us = None
        self.ctx_hk = None
        
        # {symbol: {subtype_str: {"ctx": ctx, "sub_time": float, "users": set()}}}
        self._subscriptions: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._unsub_queue: List[Dict[str, Any]] = []
        self.total_used = 0
        
    def set_contexts(self, ctx_us, ctx_hk):
        self.ctx_us = ctx_us
        self.ctx_hk = ctx_hk
        
    def _get_ctx(self, symbol: str):
        if symbol.startswith("HK."):
            return self.ctx_hk, Session.NONE
        elif symbol.startswith("US."):
            return self.ctx_us, Session.ALL
        else:
            return self.ctx_us, Session.ALL
        
    def subscribe_demand(self, symbol: str, user: str, 
                         subtypes: List[str]) -> bool:
        """按需订阅：user 声明对 symbol 需要的 subtypes"""
        if not self.ctx_us or not self.ctx_hk:
            print(f"❌ 行情上下文未就绪，无法订阅 {symbol}")
            return False
            
        valid_subtypes = [st for st in subtypes if st in self.AVAILABLE_TYPES]
        if not valid_subtypes:
            print(f"⚠️ {symbol} 无有效订阅类型")
            return False
                
        symbol_subs = self._subscriptions.get(symbol, {})
        to_subscribe = []
        
        for st_str in valid_subtypes:
            if st_str not in symbol_subs:
                if self.total_used + 1 > self.max_quota:
                    print(f"⛔ 配额不足！已用 {self.total_used}/{self.max_quota}，"
                          f"无法订阅 {symbol}.{st_str}")
                    continue
                to_subscribe.append(st_str)
            else:
                symbol_subs[st_str]["users"].add(user)
        
        if not to_subscribe:
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = {}
            for st_str in valid_subtypes:
                if st_str not in self._subscriptions[symbol]:
                    self._subscriptions[symbol][st_str] = {
                        "users": set(),
                        "sub_time": time.time()
                    }
                self._subscriptions[symbol][st_str]["users"].add(user)
            return True
                
        ctx, session = self._get_ctx(symbol)
        subtype_enums = [self.AVAILABLE_TYPES[st] for st in to_subscribe]
        
        ret, err = ctx.subscribe([symbol], subtype_enums,
                                 session=session, subscribe_push=False)
        if ret == RET_OK:
            now = time.time()
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = {}
                
            for st_str in to_subscribe:
                self._subscriptions[symbol][st_str] = {
                    "ctx": ctx,
                    "sub_time": now,
                    "users": {user}
                }
                self.total_used += 1
                
            print(f"✅ 订阅成功: {symbol} 类型={to_subscribe} "
                  f"(已用 {self.total_used}/{self.max_quota})")
            
            for st_str in valid_subtypes:
                if st_str in self._subscriptions[symbol]:
                    self._subscriptions[symbol][st_str]["users"].add(user)
                    
            return True
        else:
            print(f"❌ 订阅失败 {symbol}: {err}")
            return False
            
    def unsubscribe_demand(self, symbol: str, user: str,
                           subtypes: List[str] = None) -> bool:
        """按需反订阅"""
        symbol_subs = self._subscriptions.get(symbol)
        if not symbol_subs:
            return False
            
        if subtypes is None:
            subtypes = list(symbol_subs.keys())
            
        unsub_types = []
        for st_str in subtypes:
            if st_str in symbol_subs:
                users = symbol_subs[st_str]["users"]
                users.discard(user)
                if not users:
                    unsub_types.append(st_str)
                    
        if not unsub_types:
            return False
            
        now = time.time()
        immediate_unsub = []
        delayed_unsub = []
        
        for st_str in unsub_types:
            sub_info = symbol_subs[st_str]
            if now - sub_info["sub_time"] >= 60:
                immediate_unsub.append(st_str)
            else:
                delayed_unsub.append({
                    "subtype": st_str,
                    "ctx": sub_info["ctx"],
                    "execute_at": sub_info["sub_time"] + 60
                })
                
        if immediate_unsub:
            ctx, _ = self._get_ctx(symbol)
            subtype_enums = [self.AVAILABLE_TYPES[st] for st in immediate_unsub]
            ret, err = ctx.unsubscribe([symbol], subtype_enums)
            if ret == RET_OK:
                for st_str in immediate_unsub:
                    del symbol_subs[st_str]
                    self.total_used -= 1
                print(f"✅ 反订阅: {symbol} 类型={immediate_unsub} "
                      f"(已用 {self.total_used}/{self.max_quota})")
            else:
                print(f"❌ 反订阅失败 {symbol}: {err}")
                
        for item in delayed_unsub:
            self._unsub_queue.append({
                "symbol": symbol,
                "subtype": item["subtype"],
                "ctx": item["ctx"],
                "execute_at": item["execute_at"]
            })
            
        if not symbol_subs:
            del self._subscriptions[symbol]
            
        return True
        
    def process_unsub_queue(self):
        """处理延迟反订阅队列"""
        now = time.time()
        remain = []
        for item in self._unsub_queue:
            if now >= item["execute_at"]:
                try:
                    ret, err = item["ctx"].unsubscribe(
                        [item["symbol"]], 
                        [self.AVAILABLE_TYPES[item["subtype"]]]
                    )
                    if ret == RET_OK:
                        symbol_subs = self._subscriptions.get(item["symbol"], {})
                        if item["subtype"] in symbol_subs:
                            del symbol_subs[item["subtype"]]
                            self.total_used -= 1
                        if not symbol_subs:
                            self._subscriptions.pop(item["symbol"], None)
                        print(f"✅ 延迟反订阅: {item['symbol']}.{item['subtype']} "
                              f"(已用 {self.total_used}/{self.max_quota})")
                    else:
                        remain.append(item)
                except Exception as e:
                    print(f"❌ 延迟反订阅异常 {item['symbol']}.{item['subtype']}: {e}")
            else:
                remain.append(item)
        self._unsub_queue = remain
        
    def audit_quota(self):
        """审计配额"""
        used = self.total_used
        remaining = self.max_quota - used
        print(f"📊 配额审计: 已用 {used}/{self.max_quota}，剩余 {remaining}")
        print(f"📊 当前订阅股票数: {len(self._subscriptions)}")
        for sym, subs in self._subscriptions.items():
            types = list(subs.keys())
            users = set()
            for st_info in subs.values():
                users.update(st_info["users"])
            print(f"   {sym}: {types} (用户: {users})")
        return used, remaining
        
    def get_subscribed_types(self, symbol: str) -> List[str]:
        return list(self._subscriptions.get(symbol, {}).keys())
        
    def is_subscribed(self, symbol: str, subtype: str) -> bool:
        return subtype in self._subscriptions.get(symbol, {})
