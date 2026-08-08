"""
core/config_loader.py - v3.8.0
配置加载器（支持多用户配置 + 生命周期配置）
"""
import json
import os
import logging
from typing import Any, Dict, Optional
from pathlib import Path

logger = logging.getLogger("ConfigLoader")

DEFAULT_CONFIG = {
    "version": "3.8.0",
    "db_path": "data/apollo.db",
    "enable_us": True,
    "enable_hk": True,
    "futu_us": {
        "host": "127.0.0.1",
        "port": 11111,
        "unlock_pwd": "",
        "trade_password": "",
        "env": "SIMULATE"
    },
    "futu_hk": {
        "host": "127.0.0.1",
        "port": 11111,
        "unlock_pwd": "",
        "trade_password": "",
        "env": "SIMULATE"
    },
    "risk": {
        "max_daily_loss_pct": 0.02,
        "max_position_pct": 0.15,
        "max_orders_per_minute": 10,
        "max_strategy_orders_per_minute": 3,
        "circuit_breaker_pct": 0.05,
        "max_leverage": 2.0
    },
    "lifecycle": {
        "trial_capital_pct": 0.03,
        "formal_capital_pct": 0.10,
        "core_capital_pct": 0.20,
        "adopt_capital_pct": 0.05,
        "promotion_score": 75,
        "min_trial_trades": 30,
        "min_trial_days": 14,
        "max_optimize_count": 3,
        "decay_sharpe_ratio": 0.5,
        "decay_profit_factor": 0.6,
        "decay_dd_multiplier": 1.5
    },
    "users": {
        "default_role": "STANDARD",
        "max_login_attempts": 5,
        "session_timeout_minutes": 60,
        "allow_self_registration": False
    },
    "scheduler": {
        "evaluate_interval_minutes": 60,
        "trial_evaluate_interval_minutes": 30,
        "decay_check_interval_hours": 6,
        "adopt_check_interval_minutes": 30,
        "sync_account_interval_minutes": 5,
        "promote_check_hour": 2,
        "cleanup_hour": 3,
        "status_report_interval_minutes": 60
    },
    "logging": {
        "level": "INFO",
        "max_file_size_mb": 100,
        "backup_count": 30,
        "format": "%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s"
    }
}


def load_config(config_path: str = "config/system_config.json") -> dict:
    """
    加载配置文件，与默认值合并
    支持环境变量覆盖（APOLLO_DB_PATH 等）
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    # 从文件加载
    path = Path(config_path)
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            config = _deep_merge(config, file_config)
            logger.info(f"✅ 配置已加载: {config_path}")
        except Exception as e:
            logger.error(f"⚠️ 配置加载失败，使用默认: {e}")
    else:
        logger.warning(f"⚠️ 配置文件不存在: {config_path}，使用默认配置")
        # 创建默认配置
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
            logger.info(f"✅ 已创建默认配置: {config_path}")
        except Exception as e:
            logger.error(f"创建默认配置失败: {e}")

    # 环境变量覆盖
    _apply_env_overrides(config)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _apply_env_overrides(config: dict):
    """环境变量覆盖（优先级最高）"""
    env_mappings = {
        'APOLLO_DB_PATH': ('db_path', str),
        'APOLLO_LOG_LEVEL': ('logging', 'level', str),
        'APOLLO_ENABLE_US': ('enable_us', lambda x: x.lower() == 'true'),
        'APOLLO_ENABLE_HK': ('enable_hk', lambda x: x.lower() == 'true'),
        'APOLLO_FUTU_HOST': ('futu_us', 'host', str),
        'APOLLO_FUTU_PORT': ('futu_us', 'port', int),
    }

    for env_key, path in env_mappings.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        # 导航到目标位置
        target = config
        for step in path[:-2]:
            target = target.setdefault(step, {})
        converter = path[-1]
        try:
            if len(path) == 2:
                config[path[0]] = converter(val)
            else:
                target[path[-2]] = converter(val)
            logger.info(f"🌍 环境变量覆盖: {env_key}={val}")
        except Exception as e:
            logger.error(f"环境变量转换失败 {env_key}: {e}")


def get_nested(config: dict, dotted_key: str, default=None):
    """获取嵌套配置值，如 'risk.max_daily_loss_pct'"""
    keys = dotted_key.split('.')
    current = config
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            return default
    return current


def set_nested(config: dict, dotted_key: str, value):
    """设置嵌套配置值"""
    keys = dotted_key.split('.')
    current = config
    for k in keys[:-1]:
        current = current.setdefault(k, {})
    current[keys[-1]] = value


def save_config(config: dict, config_path: str = "config/system_config.json"):
    """保存配置到文件"""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ 配置已保存: {config_path}")


def load_user_config(user_id: str) -> dict:
    """加载用户级配置（覆盖系统配置）"""
    user_config_path = f"config/users/{user_id}/config.json"
    path = Path(user_config_path)
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"用户配置加载失败 {user_id}: {e}")
        return {}


def save_user_config(user_id: str, config: dict):
    """保存用户级配置"""
    user_config_path = f"config/users/{user_id}/config.json"
    path = Path(user_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ 用户配置已保存: {user_id}")
