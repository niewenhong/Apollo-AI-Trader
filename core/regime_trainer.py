"""
core/regime_trainer.py — Regime 训练器
特征来自本地数据库，由 data_fetcher.load_bars 提供
"""
import numpy as np
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("RegimeTrainer")


def compute_features(daily_bars: list, min15_bars: list) -> dict:
    """提取多周期特征"""
    if len(daily_bars) < 20 or len(min15_bars) < 48:
        return None

    day_close = np.array([b["close"] for b in daily_bars])
    day_slope = (day_close[-1] - day_close[-20]) / day_close[-20]
    day_vol = np.std(day_close[-20:]) / day_close[-1]

    min15_close = np.array([b["close"] for b in min15_bars])
    short_slope = (min15_close[-1] - min15_close[-20]) / min15_close[-20]
    short_vol = np.std(min15_close[-20:]) / min15_close[-1]

    return {
        "day_slope": float(day_slope),
        "day_vol": float(day_vol),
        "short_slope": float(short_slope),
        "short_vol": float(short_vol),
    }


def predict_proba(features: dict) -> dict:
    """基于规则输出概率分布"""
    if features is None:
        return {"trend": 0.333, "range": 0.334, "volatile": 0.333}

    day_slope = abs(features["day_slope"])
    day_vol = features["day_vol"]
    short_slope = abs(features["short_slope"])
    short_vol = features["short_vol"]

    trend_score = 0.6 * day_slope + 0.4 * short_slope
    range_score = 0.5 * (1 - min(day_vol / 0.02, 1)) + 0.5 * (1 - min(short_vol / 0.03, 1))
    volatile_score = 0.5 * min(day_vol / 0.02, 1) + 0.5 * min(short_vol / 0.03, 1)

    total = trend_score + range_score + volatile_score
    if total == 0:
        return {"trend": 0.333, "range": 0.334, "volatile": 0.333}

    return {
        "trend": trend_score / total,
        "range": range_score / total,
        "volatile": volatile_score / total,
    }


def detect_and_store(db_path: str, symbol: str, market: str = "US") -> dict:
    """检测Regime并存储"""
    from core.data_fetcher import load_bars

    daily = load_bars(db_path, symbol, "1d", 60)
    min15 = load_bars(db_path, symbol, "15m", 96)

    feats = compute_features(daily, min15)
    proba = predict_proba(feats)

    now = datetime.now()
    primary = max(proba, key=proba.get)

    conn = sqlite3.connect(db_path)
    conn.execute("""INSERT OR REPLACE INTO regime_records
        (symbol, exchange, regime_date, regime_time,
         prob_trend, prob_range, prob_volatile, primary_regime, confidence, version)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (symbol, market, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
         proba["trend"], proba["range"], proba["volatile"], primary, proba[primary], 1))
    conn.commit()
    conn.close()

    return proba


def walk_forward_validate(db_path: str, symbol: str, market: str = "US",
                          n_windows: int = 4) -> dict:
    """Walk-Forward 验证"""
    from core.data_fetcher import load_bars

    daily = load_bars(db_path, symbol, "1d", 200)
    min15 = load_bars(db_path, symbol, "15m", 800)

    if len(daily) < 60 or len(min15) < 200:
        return {"error": "数据不足", "wfe": 0, "in_sample_sharpe": 0,
                "out_sample_sharpe": 0, "consistency": 0, "deployable": False}

    window = min(len(daily), len(min15)) // (n_windows + 1)
    is_list, oos_list = [], []

    for i in range(n_windows):
        split = (i + 1) * window
        end = split + window // 2
        train_d, train_m = daily[:split], min15[:split]
        test_d, test_m = daily[split:end], min15[split:end]

        if len(test_d) < 10:
            continue

        f_t = compute_features(train_d, train_m)
        f_o = compute_features(test_d, test_m)
        if f_t and f_o:
            is_list.append(f_t["day_slope"])
            oos_list.append(f_o["day_slope"])

    if not is_list:
        return {"error": "无法计算", "wfe": 0, "in_sample_sharpe": 0,
                "out_sample_sharpe": 0, "consistency": 0, "deployable": False}

    is_arr = np.array(is_list)
    oos_arr = np.array(oos_list)
    is_sharpe = float(np.mean(is_arr) / (np.std(is_arr) + 1e-6) * np.sqrt(252))
    oos_sharpe = float(np.mean(oos_arr) / (np.std(oos_arr) + 1e-6) * np.sqrt(252))
    wfe = float(oos_sharpe / is_sharpe) if is_sharpe != 0 else 0.0
    consistency = float(np.sum(np.sign(is_arr) == np.sign(oos_arr)) / len(is_arr))

    return {
        "wfe": round(wfe, 3),
        "in_sample_sharpe": round(is_sharpe, 2),
        "out_sample_sharpe": round(oos_sharpe, 2),
        "consistency": round(consistency, 2),
        "deployable": (wfe > 0.5) and (consistency > 0.5),
    }


class RegimeTrainer:
    """兼容旧接口的类封装"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.db_path = self.config.get("database", {}).get("path", "trading.db")

    def detect(self, symbol: str, market: str = "US") -> dict:
        return detect_and_store(self.db_path, symbol, market)

    def walk_forward(self, symbol: str, market: str = "US") -> dict:
        return walk_forward_validate(self.db_path, symbol, market)