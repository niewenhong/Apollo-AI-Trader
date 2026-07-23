"""
ai/llm_client.py - Apollo Trader v2.6.0
LLM客户端：用于参数建议审核、选股辅助、报告摘要
支持对接任意大模型API（如DeepSeek、混元等）
"""
import json
import requests
from typing import Optional, Dict, List
from datetime import datetime


class LLMClient:
    """通用大模型客户端"""

    def __init__(self, api_key: str = "", model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def set_api_key(self, key: str):
        self.api_key = key
        self.headers["Authorization"] = f"Bearer {key}"

    def chat(self, messages: List[Dict], temperature: float = 0.3,
             max_tokens: int = 1024) -> Optional[str]:
        """调用大模型对话"""
        if not self.api_key:
            print("[LLM] 未设置API Key，跳过")
            return None

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=30
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            else:
                print(f"[LLM] API错误: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            print(f"[LLM] 请求异常: {e}")
            return None

    def refine_params(self, vt_symbol: str, strategy_class: str,
                      current_params: dict) -> Optional[dict]:
        """用LLM审核并优化参数"""
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
        response = self.chat([
            {"role": "system", "content": "你是一个专业的量化交易参数优化专家。"},
            {"role": "user", "content": prompt}
        ], temperature=0.2)

        if not response:
            return None

        try:
            # 尝试提取JSON
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(response[start:end])
                if "params" in parsed:
                    return parsed["params"]
        except:
            pass
        return None

    def summarize_diagnosis(self, diagnosis_data: dict) -> str:
        """用LLM生成诊股摘要"""
        prompt = f"""请根据以下诊股数据，生成一段简洁的中文投资建议（100字以内）：
{json.dumps(diagnosis_data, indent=2, ensure_ascii=False)}
"""
        response = self.chat([
            {"role": "user", "content": prompt}
        ], temperature=0.3, max_tokens=256)
        return response or diagnosis_data.get("summary", "")