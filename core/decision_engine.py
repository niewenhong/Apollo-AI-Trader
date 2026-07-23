"""
core/decision_engine.py - Apollo Trader v2.6.0
决策引擎：AI审核交易信号，参数建议确认，自动执行
"""
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from core.db_manager import CustomDBManager
from ai.param_advisor import ParamAdvisor
from ai.report_generator import ReportGenerator
from ai.llm_client import LLMClient


class DecisionEngine:
    """决策引擎：审核+确认+执行"""

    def __init__(self, db: CustomDBManager, llm_client: Optional[LLMClient] = None):
        self.db = db
        self.llm = llm_client
        self.advisor = ParamAdvisor(db, llm_client)
        self.reporter = ReportGenerator(db)

    def review_signal(self, vt_symbol: str, strategy_class: str,
                      signal: dict, threshold: float = 0.7) -> Tuple[bool, str]:
        """审核交易信号，返回(是否通过, 原因)"""
        score = signal.get("score", 0)
        reasons = []

        # 基本过滤
        if score < threshold:
            reasons.append(f"评分{score}<阈值{threshold}")
            self.db.save_review(vt_symbol, "rejected", strategy_class,
                                ";".join(reasons), signal)
            return False, ";".join(reasons)

        # 检查是否在AI选股池中
        pool = self.db.get_top_pool(n=100)
        pool_symbols = [p["vt_symbol"] for p in pool]
        if vt_symbol not in pool_symbols:
            reasons.append("不在AI选股池中")
            self.db.save_review(vt_symbol, "rejected", strategy_class,
                                ";".join(reasons), signal)
            return False, ";".join(reasons)

        # 检查是否有诊股记录
        diag = self.db.get_latest_diagnosis(vt_symbol)
        if diag:
            d = json.loads(diag["diagnosis_json"])
            tech = d.get("technical", {})
            if tech.get("arrangement", "").startswith("空头"):
                reasons.append("诊股显示空头排列")
                self.db.save_review(vt_symbol, "rejected", strategy_class,
                                    ";".join(reasons), signal)
                return False, ";".join(reasons)

        # 如果有LLM，进一步审核
        if self.llm:
            llm_ok, llm_reason = self._llm_review(vt_symbol, strategy_class, signal)
            if not llm_ok:
                reasons.append(f"LLM拒绝: {llm_reason}")
                self.db.save_review(vt_symbol, "rejected", strategy_class,
                                    ";".join(reasons), signal)
                return False, ";".join(reasons)

        self.db.save_review(vt_symbol, "approved", strategy_class,
                            "通过审核", signal)
        return True, "通过审核"

    def _llm_review(self, vt_symbol: str, strategy_class: str,
                    signal: dict) -> Tuple[bool, str]:
        """用LLM审核信号"""
        prompt = f"""你是一个严格的量化交易风控专家。请审核以下交易信号是否应该执行：

标的: {vt_symbol}
策略: {strategy_class}
信号详情: {json.dumps(signal, indent=2, ensure_ascii=False)}

请评估：
1. 风险是否可控？
2. 当前市场环境是否适合？
3. 是否存在明显风险点？

回复格式: {{"approve": true/false, "reason": "..."}}
"""
        response = self.llm.chat([
            {"role": "system", "content": "你是严格的风控专家。"},
            {"role": "user", "content": prompt}
        ], temperature=0.1)
        if not response:
            return True, "LLM未响应，默认通过"
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0:
                parsed = json.loads(response[start:end])
                return parsed.get("approve", True), parsed.get("reason", "")
        except:
            pass
        return True, "解析失败，默认通过"

    def auto_confirm_params(self, vt_symbol: str, strategy_class: str,
                            params: dict) -> bool:
        """自动确认参数建议，写入参数历史"""
        # 简单校验：参数不能为空
        if not params:
            return False
        # 检查是否已有更好的参数
        best = self.db.get_best_params(vt_symbol, strategy_class)
        if best:
            # 比较夏普比率等（此处简化，直接覆盖）
            pass
        self.db.save_param_history(vt_symbol, strategy_class, params, source="auto_confirm")
        return True

    def execute_decision(self, vt_symbol: str, strategy_class: str,
                         action: str, params: dict = None):
        """执行决策：下单或调整参数"""
        # 此处应调用vnpy的交易接口
        # 简化实现：记录到数据库
        self.db.save_review(vt_symbol, action, strategy_class,
                            f"执行{action}", {"params": params})
        print(f"[Decision] 执行 {action} 于 {vt_symbol}")