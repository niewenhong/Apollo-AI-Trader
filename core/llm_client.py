"""
LLM 决策客户端（可选）：把策略快照发给大模型，取回结构化决策。
当前为 stub，接 OpenAI / 本地 Ollama / 腾讯混元均可。
"""

import json
import logging
from typing import Optional

logger = logging.getLogger("LLMClient")


class LLMClient:
    """
    调用远程/本地 LLM 做交易决策。
    环境变量/配置：llm_endpoint, llm_api_key, llm_model
    """

    def __init__(self, config: dict):
        self.endpoint = config.get("llm_endpoint", "")
        self.api_key = config.get("llm_api_key", "")
        self.model = config.get("llm_model", "gpt-4o-mini")
        self.enabled = bool(self.endpoint and self.api_key)
        if self.enabled:
            logger.info(f"[LLMClient] 已启用 | endpoint={self.endpoint[:40]}...")
        else:
            logger.info("[LLMClient] 未启用（缺 endpoint 或 api_key）")

    def query_decision(self, strategy_snapshot: dict) -> Optional[dict]:
        """
        输入策略快照（指标、持仓、近期盈亏等），返回决策 dict：
        {"action": "BUY"|"SELL"|"HOLD", "size": int, "reason": str}
        当前 stub 返回 None，调用方应 fallback 到规则引擎。
        """
        if not self.enabled:
            return None

        # ── 示例：OpenAI 兼容接口（自行替换为实际请求）──
        # import requests
        # prompt = self._build_prompt(strategy_snapshot)
        # headers = {"Authorization": f"Bearer {self.api_key}",
        #            "Content-Type": "application/json"}
        # payload = {
        #     "model": self.model,
        #     "messages": [
        #         {"role": "system", "content": "你是量化交易决策助手..."},
        #         {"role": "user",   "content": prompt}
        #     ],
        #     "response_format": {"type": "json_object"},
        #     "temperature": 0.2,
        # }
        # r = requests.post(f"{self.endpoint}/chat/completions",
        #                    json=payload, headers=headers, timeout=10)
        # data = r.json()["choices"][0]["message"]["content"]
        # return json.loads(data)

        return None  # stub

    @staticmethod
    def _build_prompt(snap: dict) -> str:
        return json.dumps({
            "symbol": snap.get("symbol"),
            "last_price": snap.get("last_price"),
            "ma_fast": snap.get("fast_ma"),
            "ma_slow": snap.get("slow_ma"),
            "rsi": snap.get("rsi"),
            "atr": snap.get("atr"),
            "position": snap.get("pos"),
            "recent_pnl": snap.get("recent_pnl"),
        }, ensure_ascii=False)
