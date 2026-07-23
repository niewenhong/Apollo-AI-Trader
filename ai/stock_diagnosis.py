"""
ai/stock_diagnosis.py - Apollo Trader v2.6.0
诊股：技术面+资金面+趋势 综合诊断，结果存数据库
"""
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional

try:
    from futu import RET_OK, KLType
except ImportError:
    RET_OK = 0
    KLType = None

from core.db_manager import CustomDBManager


class StockDiagnosis:
    """单票深度诊断"""

    def __init__(self, quote_ctx, db: CustomDBManager):
        self.ctx = quote_ctx
        self.db = db

    def diagnose(self, code: str) -> Dict:
        """完整诊断一只标的，结果写入数据库"""
        tech = self._technical(code)
        mf = self._money_flow(code)
        trend = self._trend(code)

        result = {
            "code": code,
            "vt_symbol": self._to_vt(code),
            "timestamp": datetime.now().isoformat(),
            "technical": tech,
            "money_flow": mf,
            "trend": trend,
        }
        result["summary"] = self._summary(result)

        self.db.save_diagnosis(result["vt_symbol"], result, result["summary"])
        return result

    def diagnose_batch(self, codes: list) -> list:
        """批量诊断"""
        results = []
        for c in codes:
            try:
                r = self.diagnose(c)
                results.append(r)
            except Exception as e:
                print(f"[Diagnosis] {c} 失败: {e}")
        return results

    def _technical(self, code) -> Dict:
        ret, kd = self.ctx.request_history_kline(
            code, ktype=KLType.K_DAY,
            start=(datetime.now()-timedelta(days=250)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=250)
        if ret != RET_OK or kd.empty:
            return {"error": str(kd)}
        c = kd["close"].astype(float).values
        h = kd["high"].astype(float).values
        l = kd["low"].astype(float).values

        ma5, ma20, ma60 = np.mean(c[-5:]), np.mean(c[-20:]), np.mean(c[-60:])
        rsi = self._rsi(c, 14)
        bb_u, bb_m, bb_l = self._bollinger(c, 20, 2)

        # 均线排列
        if ma5 > ma20 > ma60: arrangement = "多头排列(强)"
        elif ma5 > ma20: arrangement = "短多长空(中)"
        elif ma5 < ma20 < ma60: arrangement = "空头排列(弱)"
        else: arrangement = "震荡"

        # 距离MA20
        dist = (c[-1] - ma20) / ma20 * 100

        return {
            "close": float(c[-1]),
            "ma5": float(ma5), "ma20": float(ma20), "ma60": float(ma60),
            "rsi14": float(rsi),
            "bollinger": {"upper":bb_u,"mid":bb_m,"lower":bb_l},
            "arrangement": arrangement,
            "dist_from_ma20_pct": float(dist),
            "above_ma20": bool(c[-1] > ma20),
        }

    def _money_flow(self, code) -> Dict:
        ret, mf = self.ctx.get_capital_flow(code)
        if ret != RET_OK or mf.empty:
            return {"net_inflow": 0, "note": "获取失败"}
        row = mf.iloc[-1]
        net = float(row.get("net_inflow", 0))
        big = float(row.get("inflow_large", 0))
        return {
            "net_inflow": net,
            "inflow_large": big,
            "direction": "流入" if net > 0 else "流出" if net < 0 else "平衡",
        }

    def _trend(self, code) -> Dict:
        ret, kd = self.ctx.request_history_kline(
            code, ktype=KLType.K_WEEK,
            start=(datetime.now()-timedelta(days=600)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=120)
        if ret != RET_OK or kd.empty:
            return {}
        c = kd["close"].astype(float).values
        h52, l52 = float(np.max(c[-52:])), float(np.min(c[-52:]))
        cur = float(c[-1])
        pos = (cur - l52) / (h52 - l52 + 1e-6)

        ma10w = np.mean(c[-10:])
        return {
            "high_52w": h52, "low_52w": l52,
            "position_52w_pct": float(pos),
            "week_trend": "uptrend" if cur > ma10w else "downtrend",
        }

    def _summary(self, d) -> str:
        parts = []
        t = d.get("technical", {})
        m = d.get("money_flow", {})
        tr = d.get("trend", {})
        if t.get("arrangement","").startswith("多头"): parts.append("均线多头")
        if t.get("above_ma20"): parts.append("站上MA20")
        if m.get("net_inflow",0) > 0: parts.append(f"资金{m.get('direction','')}")
        pos = tr.get("position_52w_pct", 0.5)
        if pos > 0.7: parts.append(f"52周高位{pos*100:.0f}%")
        elif pos < 0.3: parts.append(f"52周低位{pos*100:.0f}%")
        return ";".join(parts) if parts else "无明显信号"

    @staticmethod
    def _rsi(c, n=14):
        if len(c) < n+1: return 50.0
        d = np.diff(c[-(n+1):])
        g = np.maximum(d,0).sum()/n; l = np.maximum(-d,0).sum()/n
        return 100.0 if l==0 else float(100-100/(1+g/l))

    @staticmethod
    def _bollinger(c, n=20, k=2):
        m = np.mean(c[-n:]); s = np.std(c[-n:])
        return float(m+k*s), float(m), float(m-k*s)

    @staticmethod
    def _to_vt(code):
        if code.startswith("US."): return code.replace("US.","")+".SMART"
        if code.startswith("HK."): return code.replace("HK.","")+".SEHK"
        return code