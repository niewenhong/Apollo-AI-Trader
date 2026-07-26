"""
scripts/seed_strategies.py - Apollo Trader v2.7.0
将现有策略配置迁移到数据库 strategies 表。
运行方式：python scripts/seed_strategies.py
"""
import sys
import json
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.db_manager import DBManager

# ── 默认策略种子数据 ──
# 根据你的实际选股池和偏好修改
DEFAULT_STRATEGIES = [
    {
        "strategy_name": "MultiInd_US_NVDA",
        "class_name": "MultiIndicatorStrategy",
        "vt_symbol": "US.NVDA",
        "market": "US",
        "params": {
            "ma_fast": 5, "ma_slow": 20,
            "rsi_period": 14, "rsi_overbought": 75, "rsi_oversold": 30,
            "atr_period": 14, "atr_multiplier": 2.0,
            "fixed_size": 100,
        },
        "source": "manual", "modifier": "system:seed",
    },
    {
        "strategy_name": "MultiInd_US_AAPL",
        "class_name": "MultiIndicatorStrategy",
        "vt_symbol": "US.AAPL",
        "market": "US",
        "params": {
            "ma_fast": 5, "ma_slow": 20,
            "rsi_period": 14, "rsi_overbought": 75, "rsi_oversold": 30,
            "atr_period": 14, "atr_multiplier": 2.0,
            "fixed_size": 100,
        },
        "source": "manual", "modifier": "system:seed",
    },
    {
        "strategy_name": "MultiInd_HK_00700",
        "class_name": "MultiIndicatorStrategy",
        "vt_symbol": "HK.00700",
        "market": "HK",
        "params": {
            "ma_fast": 5, "ma_slow": 20,
            "rsi_period": 14, "rsi_overbought": 75, "rsi_oversold": 30,
            "atr_period": 14, "atr_multiplier": 2.0,
            "fixed_size": 100,
        },
        "source": "manual", "modifier": "system:seed",
    },
    {
        "strategy_name": "MultiInd_HK_09988",
        "class_name": "MultiIndicatorStrategy",
        "vt_symbol": "HK.09988",
        "market": "HK",
        "params": {
            "ma_fast": 5, "ma_slow": 20,
            "rsi_period": 14, "rsi_overbought": 75, "rsi_oversold": 30,
            "atr_period": 14, "atr_multiplier": 2.0,
            "fixed_size": 100,
        },
        "source": "manual", "modifier": "system:seed",
    },
]


def seed(db_path=None):
    db = DBManager(db_path)
    print(f"[Seed] 数据库: {db.db_path}")

    count = 0
    for s in DEFAULT_STRATEGIES:
        existing = db.get_strategy(s["strategy_name"])
        if existing:
            print(f"  ⏭️  {s['strategy_name']} 已存在，跳过")
            continue
        action, version = db.save_strategy(
            strategy_name=s["strategy_name"],
            class_name=s["class_name"],
            vt_symbol=s["vt_symbol"],
            market=s["market"],
            params=s["params"],
            source=s["source"],
            modifier=s["modifier"],
        )
        print(f"  ✅ {s['strategy_name']} → {action} v{version}")
        count += 1

    print(f"\n[Seed] 完成：新增 {count} 个策略")
    print("[Seed] 提示：运行 main.py 后，这些策略会在 boot() 中自动通过门禁验证并部署")

    # 显示当前所有策略
    all_s = db.get_all_strategies()
    print(f"\n📋 数据库中共有 {len(all_s)} 个策略：")
    for s in all_s:
        print(f"  • {s['strategy_name']} [{s['market']}] "
              f"v{s.get('current_version',1)} enabled={s.get('enabled',1)}")

    db.close()


if __name__ == "__main__":
    seed()
