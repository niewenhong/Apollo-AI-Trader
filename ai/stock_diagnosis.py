"""
ai/stock_diagnosis.py - Apollo-AI-Trader v2.6.0
诊股：技术面+资金面+趋势综合诊断，结果存数据库
"""
import numpy as np
from datetime import datetime, timedelta
from typing import Dict

try:
    from futu import RET_OK, KLType
except ImportError:
    RET_OK = 0; KLType = None

from core.db_manager import DBManager


class StockDiagnosis:
    """单票诊股器"""

    def __init__(self, quote_ctx, db: DBManager):
        self.ctx = quote_ctx
        self.db = db

    def diagnose(self, code: str) -> Dict:
        result = {
            "code": code, "timestamp": datetime.now().isoformat(),
            "technical": self._tech(code), "money": self._money(code),
            "trend": self._trend(code)}
        result["summary"] = self._summary(result)
        vt = self._to_vt(code)
        self.db.save_diagnosis(vt, result, result["summary"])
        return result

    def _tech(self, code):
        ret, k = self.ctx.request_history_kline(
            code, ktype=KLType.K_DAY if KLType else "K_DAY",
            start=(datetime.now()-timedelta(days=200)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=200)
        if ret != RET_OK or k.empty: return {"error": "kline fail"}
        c = k["close"].astype(float).values
        ma5, ma20, ma60 = np.mean(c[-5:]), np.mean(c[-20:]), np.mean(c[-60:])
        rsi = self._rsi(c,14)
        return {"close": float(c[-1]), "ma5":float(ma5),"ma20":float(ma20),
                "ma60":float(ma60),"rsi":float(rsi),
                "bullish": bool(ma5>ma20), "above_ma20": bool(c[-1]>ma20)}

    def _money(self, code):
        ret, mf = self.ctx.get_capital_flow(code)
        if ret != RET_OK or mf.empty: return {"error": "flow fail"}
        r = mf.iloc[-1]
        return {"net": float(r.get("net_inflow",0)),
                "large": float(r.get("inflow_large",0))}

    def _trend(self, code):
        ret, k = self.ctx.request_history_kline(
            code, ktype=KLType.K_WEEK if KLType else "K_WEEK",
            start=(datetime.now()-timedelta(days=500)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=100)
        if ret != RET_OK or k.empty: return {"error": "weekly fail"}
        c = k["close"].astype(float).values
        h52, l52 = float(np.max(c[-52:])), float(np.min(c[-52:]))
        cur = float(c[-1])
        return {"high52":h52,"low52":l52,"pos":float((cur-l52)/(h52-l52+1e-6)),
                "trend":"up" if cur>np.mean(c[-20:]) else "down"}

    def _summary(self, d):
        parts = []
        t = d.get("technical",{}); mf = d.get("money",{}); tr = d.get("trend",{})
        if t.get("bullish"): parts.append("多头排列")
        if t.get("above_ma20"): parts.append("站上MA20")
        if mf.get("net",0)>0: parts.append("资金流入")
        p = tr.get("pos",0.5)
        if p>0.7: parts.append(f"52周高位{p*100:.0f}%")
        elif p<0.3: parts.append(f"52周低位{p*100:.0f}%")
        return ";".join(parts) if parts else "无明显信号"

    @staticmethod
    def _rsi(c,n=14):
        if len(c)<n+1: return 50.0
        d=np.diff(c[-(n+1):]); g=np.maximum(d,0).sum()/n; l=np.maximum(-d,0).sum()/n
        return 100.0 if l==0 else float(100-100/(1+g/(l+1e-6)))

    @staticmethod
    def _to_vt(c): return c.replace("US.","")+".SMART" if c.startswith("US.") else c.replace("HK.","")+".SEHK"
