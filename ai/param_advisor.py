"""
ai/param_advisor.py - v2.7.0
参数建议器：基于历史数据和LLM提供策略参数优化建议
"""
from typing import Dict, Optional
from core.db_manager import CustomDBManager


class ParamAdvisor:
    """参数建议器"""

    def __init__(self, db: CustomDBManager, llm=None):
        self.db = db
        self.llm = llm  # 可选，用于高级建议

    def suggest(self, vt_symbol: str, class_name: str,
                current_params: Dict) -> Optional[Dict]:
        """
        根据历史回测表现和当前参数，返回建议的参数调整。
        如果无建议，返回空字典 {}。
        """
        # 1. 从数据库获取该策略最近的最优参数
        best_params = self.db.get_best_params(vt_symbol, class_name)
        if best_params:
            # 合并：以 best_params 为主，保留 current_params 中未出现的键
            merged = {**current_params, **best_params}
            # 过滤掉无意义的键（如 _version）
            merged = {k: v for k, v in merged.items() if not k.startswith('_')}
            return merged

        # 2. 如果有 LLM，可以调用 LLM 生成建议（此处简化）
        if self.llm:
            # prompt = f"针对{vt_symbol}的{class_name}策略，当前参数{current_params}，请给出优化建议..."
            # response = self.llm.chat(prompt)
            pass

        # 3. 无可用数据，返回空
        return {}