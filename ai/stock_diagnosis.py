"""
ai/stock_diagnosis.py — 诊股模块 v3.0.0
==========================================
变更：
  v3.0.0 - 接入 KlineProvider，日K/周K 全部走统一缓存，
           不再各自调 request_history_kline。
           数据不足时返回真实降级（不再假装 54 分）。
           内置 vt↔futu 符号转换。
功能：技术面+资金面+趋势综合诊断，结果存数据库。
"""

import numpy as np
from datetime import datetime
from typing import Dict, Optional

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

from core.kline_provider import KlineProvider


class StockDiagnosis:
    """
    单票诊股器。
    使用方式：
        kp = KlineProvider(quote_ctx=us_ctx, market="US")
        diag = StockDiagnosis(kline_provider=kp, db=db_manager)
        result = diag.diagnose("AAPL.SMART")
    """

    def __init__(self, quote_ctx=None, db=None,
                 kline_provider: Optional[KlineProvider] = None):
        """
        Parameters
        ----------
        quote_ctx : 富途行情上下文（仅资金流用，可 None 若不需要）
        db : 实现 save_diagnosis(vt_symbol, result, summary) 的对象
        kline_provider : KlineProvider 实例（必需，否则诊股降级）
        """
        self.ctx = quote_ctx
        self.db = db
        self.kp = kline_provider

    # ==================== 公开 API ====================

    def diagnose(self, vt_symbol: str) -> Dict:
        """
        对单只票做综合诊断。
        返回 dict 含 technical / money / trend / summary。
        即使 K线拿不到也返回完整结构（降级），不会抛异常。
        """
        code_for_flow = KlineProvider.vt_to_futu(vt_symbol)

        result = {
            "code": vt_symbol,
            "timestamp": datetime.now().isoformat(),
            "technical": self._tech(vt_symbol),
            "money": self._money(code_for_flow),
            "trend": self._trend(vt_symbol),
        }
        result["summary"] = self._summary(result)

        # 落库
        if self.db is not None:
            try:
                self.db.save_diagnosis(vt_symbol, result, result["summary"])
            except Exception as e:
                print(f"[Diagnosis] 落库失败 {vt_symbol}: {e}")

        return result

    # ==================== 子项计算 ====================

    def _tech(self, vt_symbol: str) -> Dict:
        """技术面：MA 排列 + RSI"""
        if self.kp is None:
            return {"error": "no_kline_provider", "rsi": 50.0,
                    "bullish": False, "above_ma20": False,
                    "ma5": 0, "ma20": 0, "ma60": 0}

        df = self.kp.get_for_diagnosis(vt_symbol)
        if df is None or df.empty or len(df) < 6:
            return {"error": "kline_insufficient", "rsi": 50.0,
                    "bullish": False, "above_ma20": False,
                    "ma5": 0, "ma20": 0, "ma60": 0}

        c = df["close"].astype(float).values
        ma5 = float(np.mean(c[-5:]))
        ma20 = float(np.mean(c[-20:])) if len(c) >= 20 else float(c[-1])
        ma60 = float(np.mean(c[-60:])) if len(c) >= 60 else float(c[-1])
        rsi = self._rsi(c, 14)
        last = float(c[-1])

        return {
            "close": last,
            "ma5": round(ma5, 4),
            "ma20": round(ma20, 4),
            "ma60": round(ma60, 4),
            "rsi": round(rsi, 2),
            "bullish": bool(ma5 > ma20 > ma60),
            "above_ma20": bool(last > ma20),
        }

    def _money(self, futu_code: str) -> Dict:
        """资金面：大单净流入（走富途 get_capital_flow）"""
        if self.ctx is None:
            return {"error": "no_quote_ctx", "net": 0.0, "large": 0.0}

        try:
            ret, mf = self.ctx.get_capital_flow(futu_code)
        except Exception as e:
            return {"error": str(e), "net": 0.0, "large": 0.0}

        if ret != RET_OK or mf is None or mf.empty:
            return {"error": "flow_fail", "net": 0.0, "large": 0.0}

        r = mf.iloc[-1]
        return {
            "net": float(r.get("net_inflow", 0) or 0),
            "large": float(r.get("inflow_large", 0) or 0),
        }

    def _trend(self, vt_symbol: str) -> Dict:
        """趋势：52周高低位 + 近期方向"""
        if self.kp is None:
            return {"error": "no_kline_provider",
                    "high52": 0, "low52": 0, "pos": 0.5, "trend": "flat"}

        df = self.kp.get_weekly(vt_symbol, weeks=60)  # ~14个月覆盖52周
        if df is None or df.empty or len(df) < 5:
            return {"error": "weekly_insufficient",
                    "high52": 0, "low52": 0, "pos": 0.5, "trend": "flat"}

        c = df["close"].astype(float).values
        h52 = float(np.max(c[-52:]))
        l52 = float(np.min(c[-52:]))
        cur = float(c[-1])
        pos = (cur - l52) / (h52 - l52 + 1e-6)
        ma20w = float(np.mean(c[-20:])) if len(c) >= 20 else cur

        return {
            "high52": round(h52, 4),
            "low52": round(l52, 4),
            "pos": round(pos, 4),
            "trend": "up" if cur > ma20w else "down",
        }

    # ==================== 汇总 ====================

    def _summary(self, d: Dict) -> str:
        parts = []
        t = d.get("technical", {}) or {}
        mf = d.get("money", {}) or {}
        tr = d.get("trend", {}) or {}

        if t.get("bullish"): parts.append("多头排列")
        if t.get("above_ma20"): parts.append("站上MA20")
        if t.get("rsi"): rsi = t["rsi"]
        if rsi > 70: parts.append(f"RSI超买{rsi:.0f}")
        elif rsi < 30: parts.append(f"RSI超卖{rsi:.0f}")
        elif 40 < rsi < 60: parts.append(f"RSI中性{rsi:.0f}")

        net = mf.get("net", 0.0)
        if net > 0: parts.append(f"资金流入{net:,.0f}")
        elif net < 0: parts.append(f"资金流出{abs(net):,.0f}")

        p = tr.get("pos", 0.5)
        if p > 0.7: parts.append(f"52周高位{p*100:.0f}%")
        elif p < 0.3: parts.append(f"52周低位{p*100:.0f}%")

        trend = tr.get("trend", "")
        if trend == "up": parts.append("周线偏多")
        elif trend == "down": parts.append("周线偏空")

        return ";".join(parts) if parts else "无明显信号"

    # ==================== 静态工具 ====================

    @staticmethod
    def _rsi(c, n=14):
        if len(c) < n + 1:
            return 50.0
        d = np.diff(c[-(n + 1):])
        g = np.maximum(d, 0).sum() / n
        l = np.maximum(-d, 0).sum() / n
        return 100.0 if l == 0 else float(100 - 100 / (1 + g / (l + 1e-6)))
