# -*- coding: utf-8 -*-
"""配置加载工具"""
import json
import os

def load_json(filepath: str) -> dict:
    """加载 JSON 配置文件"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(filepath: str, data: dict):
    """保存 JSON 配置文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_strategy_config(strategy_name: str, config_dir: str = "config/strategies") -> dict:
    """获取策略配置"""
    path = os.path.join(config_dir, f"{strategy_name}_config.json")
    return load_json(path)

def get_global_config(config_dir: str = "config") -> dict:
    """获取全局配置"""
    return load_json(os.path.join(config_dir, "system_config.json"))
