"""
ai/regime_trainer.py — 市场状态训练器 v3.0.1
==============================================
变更：
  v3.0.1 - save() 方法：features 写入时 json.dumps 序列化（不再是 str(dict)）
           get_latest() 读取时同时兼容 features 和 features_json 列
           建表增加 exchange 列 + features_json 列
           修复：原代码 str(dict) 写入 TEXT 列，读出来不是合法 JSON
  v3.0.0 - 接入 KlineProvider，120根日K 走统一缓存
"""
import json
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import sqlite3
import logging

log = logging.getLogger("RegimeTrainer")

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

from core.kline_provider import KlineProvider


class RegimeTrainer:
    """
    市场状态（Regime）计算与存储。
    使用方式：
        kp = KlineProvider(quote_ctx=us_ctx, market="US")
        rt = RegimeTrainer(kline_provider=kp, db_path="data/history.db")
        rt.batch_compute(["AAPL.SMART", "NVDA.SMART", ...])
    """

    def __init__(self, quote_ctx=None,
                 db_path: str = "data/history.db",
                 kline_provider: Optional[KlineProvider] = None):
        self.ctx = quote_ctx
        self.db_path = db_path
        self.kp = kline_provider
        self._init_table()

    # ==================== 表结构 ====================

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_table(self):
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS regime_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'US',
                    regime TEXT,
                    prob_trend REAL DEFAULT 0,
                    prob_range REAL DEFAULT 0,
                    prob_volatile REAL DEFAULT 0,
                    confidence REAL DEFAULT 0,
                    features TEXT DEFAULT '{}',
                    features_json TEXT DEFAULT '{}',
                    timestamp TEXT DEFAULT (datetime('now')),
                    UNIQUE(symbol, exchange)
                )
            """)
            # 兼容旧表：添加缺失列
            cur = conn.execute("PRAGMA table_info(regime_records)")
            columns = [r[1] for r in cur.fetchall()]
            for col, col_type in {
                "exchange": "TEXT NOT NULL DEFAULT 'US'",
                "features_json": "TEXT DEFAULT '{}'",
            }.items():
                if col not in columns:
                    try:
                        conn.execute(f"ALTER TABLE regime_records ADD COLUMN {col} {col_type}")
                        log.info(f"[Regime] +列 regime_records.{col}")
                    except Exception:
                        pass
            conn.commit()
        finally:
            conn.close()

    # ==================== 单只计算 ====================

    def compute(self, vt_symbol: str) -> Optional[Dict]:
        """
        对单只票计算 regime 概率分布。
        返回 dict，或 None（数据不足时）。
        """
        df = None
        if self.kp is not None:
            df = self.kp.get_for_regime(vt_symbol)

        # 降级：直接调富途
        if (df is None or df.empty) and self.ctx is not None:
            try:
                from futu import KLType
                ret, k, *_ = self.ctx.request_history_kline(
                    KlineProvider.vt_to_futu(vt_symbol),
                    ktype=KLType.K_DAY,
                    start=(datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d"),
                    end=datetime.now().strftime("%Y-%m-%d"),
                    max_count=120)
                if ret == RET_OK and k is not None and not k.empty:
                    df = k
            except Exception as e:
                log.error(f"[{vt_symbol}] 降级请求失败: {e}")

        if df is None or df.empty or len(df) < 30:
            log.warning(f"[{vt_symbol}] 数据不足 ({0 if df is None else len(df)}根)，跳过")
            return None

        c = df["close"].astype(float).values
        v = df["volume"].astype(float).values

        # ---- 特征 ----
        returns = np.diff(c) / c[:-1]
        volatility = float(np.std(returns[-20:]) * np.sqrt(252)) if len(returns) >= 20 else 0.2
        trend_strength = float((c[-1] - c[-20]) / (c[-20] + 1e-6)) if len(c) >= 20 else 0.0
        ma_spread = float((np.mean(c[-5:]) - np.mean(c[-20:])) / (np.mean(c[-20:]) + 1e-6)) if len(c) >= 20 else 0.0
        vol_ratio = float(np.mean(v[-5:]) / (np.mean(v[-20:]) + 1e-6)) if len(v) >= 20 else 1.0

        # ---- 规则打分 → 概率 ----
        s_trend = 0.0; s_range = 0.0; s_vol = 0.0

        if trend_strength > 0.05: s_trend += 0.4
        if ma_spread > 0.02:      s_trend += 0.3
        if vol_ratio > 1.2:        s_trend += 0.1
        if volatility < 0.3:       s_trend += 0.1

        if abs(trend_strength) < 0.02: s_range += 0.4
        if volatility < 0.25:         s_range += 0.3
        if vol_ratio < 0.8:          s_range += 0.2
        if volatility < 0.2:         s_range += 0.1

        if volatility > 0.4:                s_vol += 0.4
        if len(returns) > 0 and abs(returns[-1]) > 0.03: s_vol += 0.3
        if vol_ratio > 1.5:                s_vol += 0.2
        if volatility > 0.5:                s_vol += 0.1

        total = s_trend + s_range + s_vol + 1e-6
        probs = {
            "prob_trend":   round(s_trend / total, 4),
            "prob_range":   round(s_range / total, 4),
            "prob_volatile": round(s_vol   / total, 4),
        }

        regime = max(probs, key=probs.get).replace("prob_", "")
        confidence = round(max(probs.values()), 4)

        features = {
            "volatility":    round(volatility, 4),
            "trend_strength": round(trend_strength, 4),
            "ma_spread":     round(ma_spread, 4),
            "vol_ratio":     round(vol_ratio, 4),
        }

        return {
            "regime": regime,
            **probs,
            "confidence": confidence,
            "features": features,
        }

    # ==================== 默认/降级 ====================

    @staticmethod
    def default_regime() -> Dict:
        return {
            "regime": "range",
            "prob_trend": 0.33,
            "prob_range": 0.34,
            "prob_volatile": 0.33,
            "confidence": 0.34,
            "features": {},
        }

    # ==================== 落库 ====================

    def save(self, vt_symbol: str, regime_data: Dict):
        """
        保存 regime 结果到 regime_records 表。
        features 同时写入 features 列和 features_json 列（均为 JSON 字符串）。
        """
        parts = vt_symbol.split(".")
        symbol = parts[0]
        exchange = parts[1] if len(parts) > 1 else "SMART"

        features = regime_data.get("features", {})
        features_json_str = json.dumps(features, ensure_ascii=False)

        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO regime_records
                (symbol, exchange, regime, prob_trend, prob_range,
                 prob_volatile, confidence, features, features_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, exchange) DO UPDATE SET
                    regime=excluded.regime,
                    prob_trend=excluded.prob_trend,
                    prob_range=excluded.prob_range,
                    prob_volatile=excluded.prob_volatile,
                    confidence=excluded.confidence,
                    features=excluded.features,
                    features_json=excluded.features_json,
                    timestamp=excluded.timestamp
            """, (
                symbol, exchange,
                regime_data.get("regime", "range"),
                regime_data.get("prob_trend", 0.33),
                regime_data.get("prob_range", 0.34),
                regime_data.get("prob_volatile", 0.33),
                regime_data.get("confidence", 0.0),
                features_json_str,
                features_json_str,
                datetime.now().isoformat()
            ))
            conn.commit()
            log.info(f"[Regime] ✅ {vt_symbol} → {regime_data['regime']} "
                     f"(conf={regime_data.get('confidence',0):.2f})")
        finally:
            conn.close()

    def get_latest(self, vt_symbol: str) -> Optional[Dict]:
        """
        读取最新 regime 记录。
        兼容 features / features_json 两种列名。
        """
        parts = vt_symbol.split(".")
        symbol = parts[0]
        exchange = parts[1] if len(parts) > 1 else "SMART"

        conn = self._connect()
        try:
            cur = conn.cursor()
            # 先检查有哪些列
            info = conn.execute("PRAGMA table_info(regime_records)").fetchall()
            col_names = [r[1] for r in info]
            has_features_json = "features_json" in col_names

            if has_features_json:
                cur.execute("""
                    SELECT regime, prob_trend, prob_range, prob_volatile, confidence, features_json
                    FROM regime_records WHERE symbol=? AND exchange=?
                    ORDER BY rowid DESC LIMIT 1
                """, (symbol, exchange))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    feats = json.loads(row[5]) if row[5] else {}
                except (json.JSONDecodeError, TypeError):
                    feats = {}
                return {
                    "regime": row[0],
                    "prob_trend": row[1],
                    "prob_range": row[2],
                    "prob_volatile": row[3],
                    "confidence": row[4],
                    "features": feats,
                }
            else:
                # 旧表只有 features 列
                cur.execute("""
                    SELECT regime, prob_trend, prob_range, prob_volatile, confidence, features
                    FROM regime_records WHERE symbol=? AND exchange=?
                    ORDER BY rowid DESC LIMIT 1
                """, (symbol, exchange))
                row = cur.fetchone()
                if not row:
                    return None
                try:
                    feats = json.loads(row[5]) if row[5] else {}
                except (json.JSONDecodeError, TypeError):
                    feats = {}
                return {
                    "regime": row[0],
                    "prob_trend": row[1],
                    "prob_range": row[2],
                    "prob_volatile": row[3],
                    "confidence": row[4],
                    "features": feats,
                }
        finally:
            conn.close()

    # ==================== 批量接口 ====================

    def batch_compute(self, vt_symbols: List[str],
                      fallback_to_default: bool = True) -> Dict[str, Dict]:
        """
        批量计算 + 落库。预热由 KlineProvider.preload 完成。
        返回 {vt_symbol: regime_dict}。
        """
        if self.kp is not None:
            self.kp.preload(vt_symbols, ktype="K_DAY", days=120)

        results: Dict[str, Dict] = {}
        for vt in vt_symbols:
            try:
                r = self.compute(vt)
                if r is None:
                    if fallback_to_default:
                        r = self.default_regime()
                        log.warning(f"[Regime] {vt} 用默认 regime")
                    else:
                        log.warning(f"[Regime] {vt} 跳过（无数据）")
                        continue
                self.save(vt, r)
                results[vt] = r
            except Exception as e:
                log.error(f"[Regime] {vt} 计算失败: {e}")
                if fallback_to_default:
                    r = self.default_regime()
                    self.save(vt, r)
                    results[vt] = r

        log.info(f"[Regime] ✅ 批量完成: {len(results)}/{len(vt_symbols)} 只")
        return results
