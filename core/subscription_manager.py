# -*- coding: utf-8 -*-
"""
core/subscription_manager.py - Apollo Trader v3.8.3

职责：只做引用计数 + 配额统计，不再发起真实行情订阅。
真实订阅由 FutuGateway -> MainEngine -> quote_ctx.subscribe() 完成。

变更记录：
  v3.8.3 - 彻底移除 set_contexts / register_gateway / _do_subscribe / _do_unsubscribe
           - 只保留 subscribe_demand / release_demand / audit_quota / get_subscription_report
           - 新增 _normalize_symbol() 静态方法，确保符号带市场前缀
  v3.8.2 - 修复参数顺序、回退订阅逻辑
  v3.2.0 - 初始版本
"""
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger("SubManager")


class SubscriptionManager:
    """
    订阅管理器（引用计数版）
    不再持有行情上下文或网关，只做注册/注销统计。
    """

    def __init__(self, max_quota: int = 200):
        self.max_quota = max_quota
        # {futu_symbol: {user: set(subtypes)}}
        self._demands: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
        # 快速查询：{futu_symbol: set(subtypes)}
        self._merged: Dict[str, Set[str]] = {}

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """
        标准化符号，确保带市场前缀。
        规则：
          - 如果已有 US./HK. 前缀，直接返回
          - 如果是纯数字且长度5，视为港股，加 HK.
          - 否则视为美股，加 US.
        """
        if symbol.startswith("US.") or symbol.startswith("HK."):
            return symbol
        # 去除可能的后缀 .SMART/.SEHK
        clean = symbol.replace(".SMART", "").replace(".SEHK", "")
        if clean.isdigit() and len(clean) == 5:
            return f"HK.{clean}"
        return f"US.{clean}"

    def subscribe_demand(self, symbol: str, user: str,
                         subtypes: Optional[List[str]] = None) -> bool:
        """
        注册一个订阅需求（引用计数）。
        :param symbol: 可以是 QQQ / US.QQQ / 00700 / HK.00700
        :param user: 使用者标识（如策略名）
        :param subtypes: 需要的行情类型列表，如 ["QUOTE","K_1M"]
        :return: 是否成功（仅当超过配额时失败）
        """
        if subtypes is None:
            subtypes = ["QUOTE"]

        futu_sym = self._normalize_symbol(symbol)
        current_total = self.used()

        # 检查配额上限
        if current_total >= self.max_quota:
            log.warning(f"⚠️ 配额已满 ({current_total}/{self.max_quota})，拒绝订阅: {futu_sym}")
            return False

        # 记录需求
        old_count = len(self._merged.get(futu_sym, set()))
        self._demands[futu_sym][user].update(subtypes)
        self._rebuild_merged()

        new_count = len(self._merged.get(futu_sym, set()))
        added = new_count - old_count
        log.info(f"[Sub] ✅ 注册新订阅: {futu_sym} 类型={sorted(self._merged[futu_sym])} "
                 f"(已用 {self.used()}/{self.max_quota})")
        return True

    def release_demand(self, symbol: str, user: str,
                       subtypes: Optional[List[str]] = None) -> bool:
        """
        释放一个订阅需求（减少引用计数）。
        :param symbol: 符号（会自动标准化）
        :param user: 使用者标识
        :param subtypes: 要释放的类型，None 表示释放该用户的所有需求
        :return: 是否成功
        """
        futu_sym = self._normalize_symbol(symbol)
        if futu_sym not in self._demands:
            log.warning(f"[Sub] 未找到订阅记录: {futu_sym}")
            return False

        user_demands = self._demands[futu_sym]
        if user not in user_demands:
            log.warning(f"[Sub] 用户 {user} 未订阅 {futu_sym}")
            return False

        if subtypes is None:
            # 释放该用户的所有订阅
            del user_demands[user]
            log.info(f"[Sub] 释放 {futu_sym} 用户 {user} 的全部订阅")
        else:
            # 移除指定的子类型
            for st in subtypes:
                user_demands[user].discard(st)
            if not user_demands[user]:
                del user_demands[user]

        # 如果没有任何用户订阅该符号，清除条目
        if not user_demands:
            del self._demands[futu_sym]

        self._rebuild_merged()
        log.info(f"[Sub] 释放完成: {futu_sym} (已用 {self.used()}/{self.max_quota})")
        return True

    def used(self) -> int:
        """当前已使用的配额数（合并后的子类型总数）"""
        total = 0
        for subs in self._merged.values():
            total += len(subs)
        return total

    def remaining(self) -> int:
        return self.max_quota - self.used()

    def audit_quota(self) -> dict:
        """
        审计配额使用情况
        :return: {"total": int, "remaining": int, "details": {symbol: [subtypes]}}
        """
        details = {sym: sorted(list(subs)) for sym, subs in self._merged.items()}
        return {
            "total": self.max_quota,
            "used": self.used(),
            "remaining": self.remaining(),
            "details": details
        }

    def get_subscription_report(self) -> str:
        """生成人类可读的订阅报告"""
        report_lines = []
        report_lines.append(f"📊 配额审计: 已用 {self.used()}/{self.max_quota}, 剩余 {self.remaining()}")
        report_lines.append(f"📊 当前订阅股票数: {len(self._merged)}")
        for sym, subs in sorted(self._merged.items()):
            report_lines.append(f"  {sym}: {sorted(subs)}")
        return "\n".join(report_lines)

    def _rebuild_merged(self):
        """重建合并后的订阅视图"""
        merged = {}
        for sym, users_dict in self._demands.items():
            all_subs = set()
            for subs_set in users_dict.values():
                all_subs.update(subs_set)
            merged[sym] = all_subs
        self._merged = merged

    # ---------- 兼容旧接口（空实现，避免导入报错） ----------
    def set_contexts(self, *args, **kwargs):
        log.debug("[Sub] set_contexts 已废弃，无操作")
        pass

    def register_gateway(self, *args, **kwargs):
        log.debug("[Sub] register_gateway 已废弃，无操作")
        pass