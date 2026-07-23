"""
ai/param_advisor.py - Apollo Trader v2.6.0 (fixed)
参数顾问：用LLM辅助审核/优化策略参数，并记录建议历史
"""
import json
import time
from datetime import datetime
from typing import Optional, Dict, List


class ParamAdvisor:
    """参数顾问 - 配合 LLMClient 使用"""

    def __init__(self, db, llm_client=None):
        """
        db: CustomDBManager 实例
        llm_client: LLMClient 实例（可选，未设置时仅记录不调用LLM）
        """
        self.db = db
        self.llm = llm_client

    def set_llm(self, llm_client):
        """动态注入/切换 LLM 客户端"""
        self.llm = llm_client

    def advise(self, vt_symbol: str, strategy_class: str,
               current_params: dict) -> Optional[dict]:
        """
        用LLM审核并优化参数
        返回建议的新参数字典，失败返回 None
        """
        if not self.llm:
            print("[ParamAdvisor] 未设置LLM客户端，跳过")
            return None

        prompt = f"""你是一个量化交易参数优化专家。请审核以下参数配置，并根据经验给出优化建议。

标的: {vt_symbol}
策略: {strategy_class}
当前参数: {json.dumps(current_params, indent=2)}

请分析：
1. 参数是否合理？
2. 哪些参数需要调整？建议值是多少？
3. 调整理由是什么？

请以JSON格式回复，格式: {{"params": {{...}}, "reason": "..."}}
"""
        try:
            response = self.llm.chat([
                {"role": "system", "content": "你是一个专业的量化交易参数优化专家。"},
                {"role": "user", "content": prompt}
            ], temperature=0.2)

            if not response:
                return None

            # 提取JSON
            start = response.find("{")
            end = response.rfind("}") + 1
            if start < 0 or end <= start:
                return None

            parsed = json.loads(response[start:end])
            params = parsed.get("params")
            reason = parsed.get("reason", "")

            # 记录到数据库
            if params and self.db:
                try:
                    self.db.save_param_advice(
                        vt_symbol=vt_symbol,
                        strategy=strategy_class,
                        original=current_params,
                        suggested=params,
                        reason=reason
                    )
                except Exception as e:
                    print(f"[ParamAdvisor] 保存建议失败: {e}")

            return params

        except Exception as e:
            print(f"[ParamAdvisor] 参数建议失败: {e}")
            return None

    def get_history(self, vt_symbol: str = "", limit: int = 20) -> List[dict]:
        """获取历史参数建议记录"""
        if not self.db:
            return []
        try:
            return self.db.get_param_advice(vt_symbol, limit)
        except Exception as e:
            print(f"[ParamAdvisor] 查询历史失败: {e}")
            return []

    def batch_advise(self, strategies: List[dict]) -> Dict[str, dict]:
        """
        批量给多个策略提建议
        strategies: [{"vt_symbol": "...", "strategy_class": "...", "params": {...}}, ...]
        返回: {"vt_symbol": suggested_params}
        """
        results = {}
        for s in strategies:
            sym = s.get("vt_symbol", "")
            cls = s.get("strategy_class", "")
            params = s.get("params", {})
            suggested = self.advise(sym, cls, params)
            if suggested:
                results[sym] = suggested
            time.sleep(0.5)
        return results
