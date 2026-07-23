"""
ai/stock_selector.py - Apollo Trader v2.6.0
AI选股：技术面+资金面多维评分 → 写入数据库
"""
import time
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional

try:
    from futu import RET_OK, KLType
except ImportError:
    RET_OK = 0
    KLType = None

from core.db_manager import CustomDBManager


class AIStockSelector:
    """基于技术面+资金面的AI选股器"""

    def __init__(self, quote_ctx, db: CustomDBManager,
                 top_n: int = 25, market: str = "US"):
        self.ctx = quote_ctx
        self.db = db
        self.top_n = top_n
        self.market = market
        self.min_score = 55.0
        self.score_weights = {
            "ma_bullish": 15,
            "above_ma20": 10,
            "rsi_healthy": 10,
            "macd_golden": 10,
            "volume_surge": 8,
            "mild_uptrend": 7,
            "large_cap": 5,
            "low_volatility": 5,
        }

    def select(self, universe: Optional[List[str]] = None) -> List[Dict]:
        """执行选股，结果写入数据库"""
        if universe is None:
            universe = self._default_universe()

        scored = []
        for code in universe:
            try:
                s = self._score(code)
                if s and s["score"] >= self.min_score:
                    scored.append(s)
            except Exception as e:
                print(f"[Selector] {code} 失败: {e}")
            time.sleep(0.3)

        scored.sort(key=lambda x: x["score"], reverse=True)
        selected = scored[: self.top_n]

        pool_data = []
        for s in selected:
            pool_data.append({
                "vt_symbol": s["vt_symbol"],
                "market": self.market,
                "score": s["score"],
                "reason": s.get("reason", ""),
                "indicators": s.get("indicators", {}),
                "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
                "status": "selected",
            })
        saved = self.db.save_stock_pool(pool_data)
        print(f"[Selector] {saved} 只标的写入数据库（前5: "
              f"{', '.join(s['vt_symbol'] for s in selected[:5])}）")
        return selected

    def _score(self, code: str) -> Optional[Dict]:
        """单票综合评分 (0-100)"""
        # 获取快照
        ret, snap = self.ctx.get_market_snapshot([code])
        if ret != RET_OK or snap.empty:
            return None
        row = snap.iloc[0]
        last = float(row.get("last_price", 0))
        prev = float(row.get("prev_close_price", last))
        chg = (last - prev) / prev if prev else 0
        turnover = float(row.get("turnover", 0))

        # 获取日K
        ret, kd = self.ctx.request_history_kline(
            code, ktype=KLType.K_DAY,
            start=(datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=130)
        if ret != RET_OK or kd.empty:
            return None

        c = kd["close"].astype(float).values
        v = kd["volume"].astype(float).values
        if len(c) < 60:
            return None

        ma5 = float(np.mean(c[-5:]))
        ma20 = float(np.mean(c[-20:]))
        ma60 = float(np.mean(c[-60:]))
        rsi = self._rsi(c, 14)
        macd, signal, hist = self._macd(c)
        vr = float(v[-1] / (np.mean(v[-20:]) + 1e-6))

        ind = {
            "last": last, "chg": chg, "turnover": turnover,
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "rsi": rsi, "macd_hist": hist, "vol_ratio": vr,
        }

        score = 50.0
        reasons = []

        if ma5 > ma20 > ma60:
            score += self.score_weights["ma_bullish"]; reasons.append("多头排列")
        if last > ma20:
            score += self.score_weights["above_ma20"]; reasons.append("站上MA20")
        if 35 < rsi < 70:
            score += self.score_weights["rsi_healthy"]; reasons.append(f"RSI{rsi:.0f}")
        if hist > 0 and hist > signal * 0.5:
            score += self.score_weights["macd_golden"]; reasons.append("MACD金叉")
        if vr > 1.5:
            score += self.score_weights["volume_surge"]; reasons.append(f"放量{vr:.1f}x")
        if 0.005 < chg < 0.06:
            score += self.score_weights["mild_uptrend"]; reasons.append(f"温和涨{chg*100:.1f}%")
        if turnover > 5e7:
            score += self.score_weights["large_cap"]; reasons.append("大市值")

        vol_std = float(np.std(c[-20:] / np.mean(c[-20:]) - 1))
        if vol_std < 0.03:
            score += self.score_weights["low_volatility"]; reasons.append("低波动")

        score = min(score, 100.0)
        return {
            "vt_symbol": self._to_vt(code),
            "code": code,
            "score": round(score, 1),
            "reason": ";".join(reasons),
            "indicators": ind,
        }

    def _default_universe(self) -> List[str]:
        if self.market == "US":
            return ["US.NVDA","US.AAPL","US.MSFT","US.AMZN","US.TSLA",
                    "US.META","US.GOOGL","US.AMD","US.NFLX","US.BABA",
                    "US.COIN","US.MARA","US.RIOT","US.UPST","US.ARM"]
        return ["HK.00700","HK.09988","HK.03690","HK.00388","HK.00941",
                "HK.02318","HK.00005","HK.00011","HK.00027","HK.01024"]

    @staticmethod
    def _to_vt(code: str) -> str:
        if code.startswith("US."): return code.replace("US.","")+".SMART"
        if code.startswith("HK."): return code.replace("HK.","")+".SEHK"
        return code

    @staticmethod
    def _rsi(closes, n=14):
        if len(closes) < n+1: return 50.0
        d = np.diff(closes[-(n+1):])
        g = np.maximum(d,0).sum()/n
        l = np.maximum(-d,0).sum()/n
        return 100.0 if l==0 else float(100 - 100/(1+g/l))

    @staticmethod
    def _macd(closes, f=12, s=26, n=9):
        if len(closes) < s+n: return 0.0,0.0,0.0
        def ema(d, p):
            r = np.zeros_like(d); r[0]=d[0]
            k=2.0/(p+1)
            for i in range(1,len(d)): r[i]=d[i]*k+r[i-1]*(1-k)
            return r
        ef, es = ema(closes,f), ema(closes,s)
        ml = ef-es
        sl = ema(np.concatenate([[ml[:s].mean()], ml[s:]]), n)
        return float(ml[-1]), float(sl[-1]), float(ml[-1]-sl[-1])