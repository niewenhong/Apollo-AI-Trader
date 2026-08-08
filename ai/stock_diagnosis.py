# -*- coding: utf-8 -*-
"""
ai/stock_diagnosis.py — 诊股模块 v3.8.1
==========================================
- 兼容 KlineProvider 返回 dict 格式（isinstance 判断）
- 兼容返回 DataFrame 格式
- 数据不足时返回真实降级
"""
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Optional

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

from core.kline_provider import KlineProvider


class StockDiagnosis:
    """单票诊股器 v3.8.1（兼容 dict / DataFrame 两种 K线返回格式）"""

    def __init__(self, quote_ctx=None, db=None,
                 kline_provider: Optional[KlineProvider] = None):
        self.ctx = quote_ctx
        self.db = db
        self.kp = kline_provider

    # ==================== 公开 API ====================

    def diagnose(self, vt_symbol: str) -> Dict:
        code_for_flow = KlineProvider.vt_to_futu(vt_symbol)

        result = {
            "code": vt_symbol,
            "timestamp": datetime.now().isoformat(),
            "technical": self._tech(vt_symbol),
            "money": self._money(code_for_flow),
            "trend": self._trend(vt_symbol),
        }
        result["summary"] = self._summary(result)

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
            return self._empty_tech("no_kline_provider")

        # ★ v3.8.1: 兼容 dict 和 DataFrame 两种返回
        data = self.kp.get_for_diagnosis(vt_symbol)
        if data is None:
            return self._empty_tech("kline_insufficient")

        # 兼容 dict 格式（KlineProvider 返回 {"daily_df": DataFrame, ...}）
        if isinstance(data, dict):
            df = data.get("daily_df")
        elif isinstance(data, pd.DataFrame):
            df = data
        else:
            df = None

        if df is None or df.empty or len(df) < 6:
            return self._empty_tech("kline_insufficient")

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
        """资金面：大单净流入"""
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
            return self._empty_trend()

        df = self.kp.get_weekly(vt_symbol, count=60)
        if df is None or df.empty or len(df) < 5:
            return self._empty_trend()

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

        rsi = t.get("rsi", 50.0)
        if rsi > 70:
            parts.append(f"RSI超买{rsi:.0f}")
        elif rsi < 30:
            parts.append(f"RSI超卖{rsi:.0f}")
        elif 40 < rsi < 60:
            parts.append(f"RSI中性{rsi:.0f}")

        if t.get("bullish"):
            parts.append("多头排列")
        if t.get("above_ma20"):
            parts.append("站上MA20")

        net = mf.get("net", 0.0)
        if net > 0:
            parts.append(f"资金流入{net:,.0f}")
        elif net < 0:
            parts.append(f"资金流出{abs(net):,.0f}")

        p = tr.get("pos", 0.5)
        if p > 0.7:
            parts.append(f"52周高位{p*100:.0f}%")
        elif p < 0.3:
            parts.append(f"52周低位{p*100:.0f}%")

        trend = tr.get("trend", "")
        if trend == "up":
            parts.append("周线偏多")
        elif trend == "down":
            parts.append("周线偏空")

        return ";".join(parts) if parts else "无明显信号"

    # ==================== 工具 ====================

    def _empty_tech(self, reason: str) -> Dict:
        return {"error": reason, "rsi": 50.0, "bullish": False,
                "above_ma20": False, "ma5": 0, "ma20": 0, "ma60": 0}

    def _empty_trend(self) -> Dict:
        return {"error": "no_kline", "high52": 0, "low52": 0,
                "pos": 0.5, "trend": "flat"}

    @staticmethod
    def _rsi(c, n=14):
        if len(c) < n + 1:
            return 50.0
        d = np.diff(c[-(n + 1):])
        g = np.maximum(d, 0).sum() / n
        l = np.maximum(-d, 0).sum() / n
        return 100.0 if l == 0 else float(100 - 100 / (1 + g / (l + 1e-6)))
