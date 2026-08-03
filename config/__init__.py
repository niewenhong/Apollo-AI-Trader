"""
config/__init__.py - 配置加载器
"""
import json
import os
import logging

log = logging.getLogger("Config")

DEFAULT_CONFIG = {
    "version": "3.2.0",
    "default_market": "US",
    "futu": {"host": "127.0.0.1", "port": 11111, "password": ""},
    "database": {"path": "data/history.db", "wal_mode": True},
    "selector": {
        "top_n": 20,
        "basic_min_score": 55.0,
        "anomaly_threshold": 30,
        "us_universe": [],
        "hk_universe": [],
    },
    "regime": {"enabled": True, "min_confidence": 0.35, "fallback_default": True},
    "subscription": {
        "max_quota": 300,
        "auto_subscribe_anomaly": True,
        "auto_subscribe_basic": True,
        "subscribe_warrants": True,
        "subscribe_cbbc": True,
        "subscribe_options": True,
    },
    "prelive_gate": {
        "enabled": False,
        "hot_reload_interval": 600,
        "backtest_days": 60,
        "backtest_interval": "1m",
        "thresholds": {
            "min_sharpe": 0.5,
            "min_win_rate": 0.4,
            "max_drawdown": 0.15,
        },
    },
    "risk": {
        "max_position_pct": 0.15,
        "max_strategies": 30,
        "max_daily_loss_pct": 0.05,
        "kill_switch_enabled": True,
    },
    "logging": {"level": "INFO", "file_rotation_mb": 50, "keep_days": 7},
}


def load_config(config_path: str = "config/system_config.json") -> dict:
    """
    加载配置文件，与默认值合并。
    优先读 system_config.json，不存在则读 system_config.example.json。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    candidates = [config_path, "config/system_config.example.json"]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                _deep_merge(cfg, user_cfg)
                log.info(f"✅ 配置已加载: {path}")
                return cfg
            except Exception as e:
                log.warning(f"⚠️ 配置文件 {path} 解析失败: {e}")

    log.warning("⚠️ 未找到配置文件，使用默认配置")
    return cfg


def _deep_merge(base: dict, override: dict):
    """递归合并字典"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save_config(config: dict, path: str = "config/system_config.json"):
    """保存配置"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    log.info(f"✅ 配置已保存: {path}")
