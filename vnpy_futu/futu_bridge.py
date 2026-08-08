"""
vnpy_futu/futu_bridge.py - v3.8.0 (fix: get_positions → query_positions)
FUTU 网关桥接器（多用户 + 生命周期集成）
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger("FutuBridge")


class FutuBridge:
    """
    FUTU 网关桥接器

    职责：
    - 连接 FUTU OpenD
    - 提供行情/交易接口
    - 多用户账户映射
    - 权益变更通知
    """

    def __init__(self, market: str, config: dict = None):
        self.market = market
        self.config = config or {}
        self.host = self.config.get("host", "127.0.0.1")
        self.port = self.config.get("port", 11111)
        self.unlock_pwd = self.config.get("unlock_pwd", "")
        self.trade_password = self.config.get("trade_password", "")
        self.env = self.config.get("env", "SIMULATE")

        self._gateway = None
        self._trade_ctx = None
        self._quote_ctx = None

        self._user_accounts: Dict[str, str] = {}
        self._equity_callback = None

        self._connect()
        logger.info(f"[FutuBridge] ✅ {market} 初始化完成")

    def _connect(self):
        try:
            from vnpy_futu.futu_gateway import FutuGateway
            self._gateway = FutuGateway(
                event_engine=None,
                host=self.host,
                port=self.port,
                env=self.env,
            )
            logger.info(f"[FutuBridge] 🔗 {self.market} 已连接 {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"[FutuBridge] ⚠️ {self.market} 连接失败: {e}")
            self._gateway = None

    # ==================== 账户管理 ====================

    def add_user_account(self, user_id: str, account_id: str = ""):
        self._user_accounts[user_id] = account_id or user_id
        logger.info(f"[FutuBridge] 👤 {user_id} → account={account_id or 'default'}")

    def query_account(self, user_id: str = "SYSTEM") -> Optional[dict]:
        if not self._gateway:
            return None
        try:
            # 兼容多种方法名：query_account / query_acc_info
            gateway = self._gateway
            if hasattr(gateway, 'query_account'):
                result = gateway.query_account()
            elif hasattr(gateway, 'query_acc_info'):
                result = gateway.query_acc_info()
            else:
                logger.error("[FutuBridge] 网关无 query_account 方法")
                return None

            if result:
                acc_info = {
                    'total_assets': result.get('total_assets', 0),
                    'cash': result.get('cash', 0),
                    'market_val': result.get('market_val', 0),
                    'power': result.get('power', 0),
                    'user_id': user_id,
                    'market': self.market,
                    'timestamp': datetime.now().isoformat(),
                }
                if self._equity_callback:
                    try:
                        self._equity_callback(user_id, acc_info)
                    except Exception as e:
                        logger.error(f"[FutuBridge] 权益回调异常: {e}")
                return acc_info
        except Exception as e:
            logger.error(f"[FutuBridge] 查询账户失败: {e}")
        return None

    def get_all_positions(self, user_id: str = "SYSTEM") -> List[dict]:
        if not self._gateway:
            return []
        try:
            # 兼容多种方法名：query_positions / query_position / get_positions
            gateway = self._gateway
            positions_raw = []
            if hasattr(gateway, 'query_positions'):
                positions_raw = gateway.query_positions()
            elif hasattr(gateway, 'query_position'):
                positions_raw = gateway.query_position()
            elif hasattr(gateway, 'get_positions'):
                positions_raw = gateway.get_positions()
            else:
                logger.error("[FutuBridge] 网关无持仓查询方法")
                return []

            result = []
            for pos in positions_raw:
                result.append({
                    'code': pos.get('code', ''),
                    'qty': pos.get('qty', 0),
                    'cost_price': pos.get('cost_price', 0),
                    'last_price': pos.get('last_price', 0),
                    'pnl': pos.get('pnl', 0),
                    'market': self.market,
                    'user_id': user_id,
                })
            return result
        except Exception as e:
            logger.error(f"[FutuBridge] 查询持仓失败: {e}")
            return []

    # ==================== 下单接口 ====================

    def send_order(self, symbol: str, price: float, volume: int,
                   direction: str, offset: str,
                   order_type: str = "LIMIT",
                   user_id: str = "SYSTEM") -> str:
        if not self._gateway:
            logger.error("[FutuBridge] 网关未连接")
            return ""
        try:
            order_id = self._gateway.send_order(
                symbol=symbol, price=price, volume=volume,
                direction=direction, offset=offset, order_type=order_type,
            )
            logger.info(
                f"[FutuBridge] 📤 {order_id} | {direction} {symbol} "
                f"{volume}@{price:.2f} offset={offset} user={user_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"[FutuBridge] 下单失败: {e}")
            return ""

    def cancel_order(self, order_id: str) -> bool:
        if not self._gateway:
            return False
        try:
            self._gateway.cancel_order(order_id)
            return True
        except Exception as e:
            logger.error(f"[FutuBridge] 撤单失败: {e}")
            return False

    # ==================== 行情订阅 ====================

    def subscribe(self, symbol: str, data_types: list = None) -> bool:
        if not self._gateway:
            return False
        try:
            self._gateway.subscribe(symbol, data_types or ["QUOTE", "K_1M"])
            logger.info(f"[FutuBridge] 📡 订阅: {symbol} {data_types}")
            return True
        except Exception as e:
            logger.error(f"[FutuBridge] 订阅失败: {e}")
            return False

    def unsubscribe(self, symbol: str) -> bool:
        if not self._gateway:
            return False
        try:
            self._gateway.unsubscribe(symbol)
            return True
        except Exception:
            return False

    # ==================== 回调注册 ====================

    def set_equity_callback(self, callback):
        self._equity_callback = callback

    def set_order_callback(self, callback):
        if self._gateway and hasattr(self._gateway, 'set_order_callback'):
            self._gateway.set_order_callback(callback)

    def set_trade_callback(self, callback):
        if self._gateway and hasattr(self._gateway, 'set_trade_callback'):
            self._gateway.set_trade_callback(callback)

    # ==================== 状态 ====================

    def is_connected(self) -> bool:
        return self._gateway is not None

    def get_gateway(self):
        return self._gateway

    def disconnect(self):
        if self._gateway:
            try:
                self._gateway.close()
            except Exception:
                pass
            self._gateway = None
        logger.info(f"[FutuBridge] 🔌 {self.market} 已断开")

    def get_status(self) -> dict:
        return {
            'market': self.market,
            'connected': self.is_connected(),
            'host': self.host,
            'port': self.port,
            'env': self.env,
            'users': list(self._user_accounts.keys()),
        }