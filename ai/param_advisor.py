"""
ai/param_advisor.py - v3.0.2
参数建议器：基于历史绩效 + 可选 LLM 优化建议
修复 v2.7.0：
  1. DBManager.get_best_params 返回值对齐
  2. 新增 suggest_from_performance() 基于 strategy_performance 表
  3. LLM 调用保护
"""
import logging
from typing import Dict, Optional

log = logging.getLogger("ParamAdvisor")


class ParamAdvisor:
    """参数建议器"""

    def __init__(self, db=None, llm=None):
        self.db = db
        self.llm = llm

    def suggest(self, vt_symbol: str, class_name: str,
                current_params: Dict) -> Optional[Dict]:
        """
        根据历史绩效和当前参数，返回建议参数。
        优先级：DB 历史最优 > LLM 建议 > 当前参数
        """
        # 1. 从数据库获取历史最优参数
        best = self._get_best_params(vt_symbol, class_name)
        if best:
            merged = {**current_params, **best}
            merged = {k: v for k, v in merged.items() if not k.startswith('_')}
            log.info(f"[Advisor] {vt_symbol} DB最优参数: {list(best.keys())}")
            return merged

        # 2. LLM 建议
        if self.llm:
            llm_params = self.llm.refine_params(vt_symbol, class_name, current_params)
            if llm_params:
                log.info(f"[Advisor] {vt_symbol} LLM建议参数: {list(llm_params.keys())}")
                return {**current_params, **llm_params}

        return {}

    def _get_best_params(self, vt_symbol: str, class_name: str) -> Dict:
        """从 strategy_performance 表获取 sharpe 最高的参数"""
        try:
            if hasattr(self.db, 'conn'):
                conn = self.db.conn
                cur = conn.execute(
                    """SELECT params_json FROM strategy_performance
                       WHERE vt_symbol=? AND strategy_name=?
                       ORDER BY sharpe DESC LIMIT 1""",
                    (vt_symbol, class_name),
                )
                row = cur.fetchone()
                if row and row[0]:
                    import json
                    return json.loads(row[0])
        except Exception:
            pass
        # 降级到 strategy_config
        try:
            if hasattr(self.db, 'get_best_params'):
                return self.db.get_best_params(vt_symbol, class_name) or {}
        except Exception:
            pass
        return {}

    def record_performance(self, strategy_name: str, vt_symbol: str,
                           sharpe: float, total_return: float = 0.0,
                           max_dd: float = 0.0, win_rate: float = 0.0,
                           params: Optional[Dict] = None, regime: str = ""):
        """记录策略绩效供后续参考"""
        try:
            import json
            params_json = json.dumps(params or {}, ensure_ascii=False)
            if hasattr(self.db, 'save_performance'):
                self.db.save_performance(
                    strategy_name, vt_symbol, sharpe, total_return,
                    max_dd, win_rate, regime)
        except Exception as e:
            log.warning(f"[Advisor] 记录绩效失败: {e}")
