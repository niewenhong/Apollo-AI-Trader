"""
core/trade_mode_manager.py — 交易模式管理
模拟/实盘切换 + 风险提示 + 热加载
"""
import sqlite3
import logging
from datetime import datetime
from enum import Enum

log = logging.getLogger("TradeMode")


class TradeMode(Enum):
    SIMULATION = "simulation"
    LIVE = "live"


class AppTrdEnv(Enum):
    SIMULATE = "SIMULATE"
    REAL = "REAL"


# ─────────────────────────────────────────────
#  数据库操作
# ─────────────────────────────────────────────

def get_current_mode(db_path: str, user_id: str) -> str:
    """获取用户当前交易模式"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT trade_mode FROM user_config WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "simulation"


def get_trd_env(db_path: str, user_id: str) -> str:
    """获取交易环境字符串"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT trd_env FROM user_config WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "SIMULATE"


def switch_mode(db_path: str, user_id: str, new_mode: str,
                acknowledge_risk: bool = False) -> bool:
    """
    切换交易模式
    new_mode: 'simulation' 或 'live'
    切到 live 必须先 acknowledge_risk=True
    """
    if new_mode == "live" and not acknowledge_risk:
        log.error(f"用户 {user_id} 切换实盘未确认风险")
        return False

    trd_env = "REAL" if new_mode == "live" else "SIMULATE"

    conn = sqlite3.connect(db_path)
    conn.execute("""UPDATE user_config SET 
        trade_mode=?, trd_env=?,
        risk_acknowledged=?, risk_acknowledged_at=?
        WHERE user_id=?""",
        (new_mode, trd_env,
         1 if acknowledge_risk else 0,
         datetime.now().isoformat() if acknowledge_risk else None,
         user_id))
    conn.commit()
    conn.close()

    log.info(f"用户 {user_id} 切换到 {new_mode}")
    return True


def check_mode_change(db_path: str, user_id: str, current_mode: str) -> str:
    """检查数据库中的模式是否与当前一致，不一致则返回新模式"""
    db_mode = get_current_mode(db_path, user_id)
    if db_mode != current_mode:
        return db_mode
    return ""


# ─────────────────────────────────────────────
#  类封装
# ─────────────────────────────────────────────

class TradeModeManager:
    """交易模式管理器"""

    RISK_WARNING = (
        "⚠️ 风险警告 ⚠️\n"
        "您即将切换到【实盘交易】模式。\n"
        "1. 实盘交易将使用您的真实富途账户资金进行买卖。\n"
        "2. 金融市场存在固有风险，您可能损失部分或全部本金。\n"
        "3. 本平台仅提供策略执行服务，不对任何交易亏损承担责任。\n"
        "4. 过往业绩不代表未来表现。\n"
        "5. 请审慎决策。\n"
        "回复「我已知晓风险」确认切换，回复「取消」放弃。"
    )

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.db_path = self.config.get("database", {}).get("path", "trading.db")
        self.current_modes = {}

    def get_mode(self, user_id: str) -> tuple:
        mode = get_current_mode(self.db_path, user_id)
        acknowledged = self._is_acknowledged(user_id)
        return mode, acknowledged

    def _is_acknowledged(self, user_id: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT risk_acknowledged FROM user_config WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
        return bool(row[0]) if row else False

    def request_live(self, user_id: str) -> str:
        """请求切换到实盘，返回风险提示文案"""
        return self.RISK_WARNING

    def confirm_live(self, user_id: str) -> bool:
        """确认风险后切换实盘"""
        return switch_mode(self.db_path, user_id, "live", acknowledge_risk=True)

    def switch_to_sim(self, user_id: str) -> bool:
        """切换到模拟盘"""
        return switch_mode(self.db_path, user_id, "simulation", acknowledge_risk=False)

    def hot_reload(self, user_id: str) -> str:
        """热加载检查"""
        current = self.current_modes.get(user_id, "simulation")
        new = check_mode_change(self.db_path, user_id, current)
        if new:
            self.current_modes[user_id] = new
        return new