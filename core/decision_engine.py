"""
core/decision_engine.py - Apollo-AI-Trader v2.6.0
AI审核引擎：综合规则+LLM评估，决定采纳/拒绝/暂缓参数更新
"""
import json
from datetime import datetime
from typing import Dict, Optional

from core.db_manager import DBManager
from ai.llm_client import LLMClient


class DecisionEngine:
    """AI审核引擎 - 全自动参数治理"""

    def __init__(self, db: DBManager, llm: Optional[LLMClient] = None,
                 config: dict = None):
        self.db = db
        self.llm = llm
        self.config = config or {
            "min_sharpe": 0.5, "min_improvement": 0.1,
            "max_drawdown": 0.35, "cooldown_days": 7,
            "max_consecutive_updates": 3,
        }
        self._last_update = {}
        self._consecutive = {}

    def review(self, vt_symbol: str, new_params: dict,
               backtest_stats: dict, old_stats: dict) -> str:
        """审核参数更新请求，返回 accept/reject/defer"""
        # 1. 冷却期
        last = self._last_update.get(vt_symbol)
        if last and (datetime.now()-last).days < self.config["cooldown_days"]:
            return "defer"
        # 2. 连续更新限制
        if self._consecutive.get(vt_symbol,0) >= self.config["max_consecutive_updates"]:
            return "reject"
        # 3. 规则检查
        checks = []
        ns = backtest_stats.get("sharpe",0); os_ = old_stats.get("sharpe",0)
        ndd = backtest_stats.get("max_dd",1); odd = old_stats.get("max_dd",1)
        if ns < self.config["min_sharpe"]:
            checks.append(("fail",f"夏普{ns:.2f}<{self.config['min_sharpe']}"))
        imp = (ns-os_)/(os_+1e-6)
        if imp < self.config["min_improvement"]:
            checks.append(("fail",f"提升{imp:.1%}<{self.config['min_improvement']:.0%}"))
        if ndd > self.config["max_drawdown"]:
            checks.append(("fail",f"回撤{ndd:.2%}>{self.config['max_drawdown']:.0%}"))
        # 4. LLM审核
        llm_result = None
        if self.llm and self.llm.enabled:
            llm_result = self.llm.review_decision(vt_symbol, backtest_stats, old_stats)
        # 5. 综合
        fails = [c for c in checks if c[0]=="fail"]
        if fails:
            decision = "reject"; reason = ";".join(c[1] for c in fails)
        elif llm_result and llm_result.get("decision") == "accept":
            decision = "accept"; reason = llm_result.get("reason","LLM通过")
        elif llm_result and llm_result.get("decision") == "defer":
            decision = "defer"; reason = llm_result.get("reason","LLM建议暂缓")
        else:
            decision = "accept" if not fails else "defer"
            reason = "规则通过" if decision=="accept" else "规则未通过"
        # 6. 记录
        self.db.save_review_decision(vt_symbol, decision, reason,
                                      {"new":backtest_stats,"old":old_stats})
        # 7. 更新状态
        if decision == "accept":
            self._last_update[vt_symbol] = datetime.now()
            self._consecutive[vt_symbol] = self._consecutive.get(vt_symbol,0)+1
        elif decision == "reject":
            self._consecutive[vt_symbol] = 0
        return decision
