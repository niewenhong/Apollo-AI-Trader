"""
utils/helpers.py - v2.6.0
通用工具函数：时间转换、格式化、数据清洗等
"""
import json
from datetime import datetime, timedelta
from typing import Union, List, Dict, Optional
import numpy as np


def to_vt_symbol(code: str) -> str:
    """富途代码转vt_symbol格式"""
    if code.startswith("US."):
        return code.replace("US.", "") + ".SMART"
    if code.startswith("HK."):
        return code.replace("HK.", "") + ".SEHK"
    return code


def from_vt_symbol(vt_symbol: str) -> str:
    """vt_symbol转富途代码格式"""
    if ".SMART" in vt_symbol:
        return "US." + vt_symbol.replace(".SMART", "")
    if ".SEHK" in vt_symbol:
        return "HK." + vt_symbol.replace(".SEHK", "")
    return vt_symbol


def detect_market(vt_symbol: str) -> str:
    """检测市场"""
    if ".SMART" in vt_symbol:
        return "US"
    if ".SEHK" in vt_symbol:
        return "HK"
    return "UNKNOWN"


def format_pnl(pnl: float) -> str:
    """格式化盈亏显示"""
    if pnl >= 0:
        return f"+{pnl:.2f}"
    return f"{pnl:.2f}"


def calculate_sharpe(returns: List[float], risk_free_rate: float = 0.02) -> float:
    """计算夏普比率"""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    excess = arr - risk_free_rate / 252  # 日化
    if np.std(excess) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(excess) * np.sqrt(252))


def calculate_max_drawdown(equity_curve: List[float]) -> float:
    """计算最大回撤"""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """安全除法"""
    if b == 0:
        return default
    return a / b


def truncate_string(s: str, max_len: int = 100) -> str:
    """截断字符串"""
    if len(s) > max_len:
        return s[:max_len-3] + "..."
    return s


def parse_datetime(dt_str: str) -> Optional[datetime]:
    """解析多种格式的时间字符串"""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except:
            continue
    return None


def merge_dicts(base: dict, override: dict) -> dict:
    """合并字典（override覆盖base）"""
    result = base.copy()
    result.update(override)
    return result


def chunk_list(lst: list, chunk_size: int) -> List[list]:
    """将列表分割成多个小列表"""
    return [lst[i:i+chunk_size] for i in range(0, len(lst), chunk_size)]


def timestamp_now() -> str:
    """获取当前ISO时间戳"""
    return datetime.now().isoformat()