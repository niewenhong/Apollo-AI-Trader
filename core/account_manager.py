# -*- coding: utf-8 -*-
"""
core/account_manager.py - 账户资金管理 v3.8.2 (Fixed: 自定义 upsert, 安全 SQL)
"""
import logging
from datetime import datetime
from typing import Dict, Optional, List
from sqlalchemy import text

logger = logging.getLogger("AccountManager")


class AccountManager:
    """账户资金管理"""

    def __init__(self, db_manager, main_engines: dict = None):
        self.db = db_manager
        self._bridges = main_engines or {}
        self._cache: Dict[str, dict] = {}
        self._last_sync: Dict[str, datetime] = {}

    def set_main_engines(self, bridges: dict):
        self._bridges = bridges

    # ==================== 数据库安全操作 ====================
    def _get_db_session(self):
        """获取 SQLAlchemy session"""
        if self.db is None:
            return None
        session = getattr(self.db, 'session', None)
        if session is not None and hasattr(session, 'execute'):
            return session
        engine = getattr(self.db, 'engine', None)
        if engine is not None:
            from sqlalchemy.orm import Session
            return Session(engine)
        conn = getattr(self.db, 'conn', None)
        if conn is not None and hasattr(conn, 'execute'):
            return conn
        raise RuntimeError("无法获取数据库 session")

    def _upsert_position(self, pos_data: dict):
        """自定义 upsert 持仓记录"""
        session = self._get_db_session()
        if session is None:
            logger.error("[Account] 无法获取数据库 session，跳过持仓写入")
            return
        try:
            stmt = text("""
                INSERT INTO positions 
                    (user_id, symbol, market, quantity, avg_cost, last_price, pnl, is_managed, updated_at)
                VALUES 
                    (:user_id, :symbol, :market, :quantity, :avg_cost, :last_price, :pnl, :is_managed, :updated_at)
                ON CONFLICT(user_id, symbol) 
                DO UPDATE SET 
                    market = :market,
                    quantity = :quantity,
                    avg_cost = :avg_cost,
                    last_price = :last_price,
                    pnl = :pnl,
                    is_managed = :is_managed,
                    updated_at = :updated_at
            """)
            session.execute(stmt, pos_data)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[Account] upsert_position 失败: {e}")
        finally:
            if hasattr(session, 'close'):
                session.close()

    # ==================== 资金查询 ====================
    def get_total_assets(self, market: str = "ALL") -> float:
        total = 0.0
        for mkt, bridge in self._bridges.items():
            if market != "ALL" and mkt != market:
                continue
            try:
                acc_info = bridge.query_account()
                if acc_info:
                    total += acc_info.get('total_assets', 0)
            except Exception as e:
                logger.error(f"[Account] 查询 {mkt} 账户失败: {e}")
        return total

    def get_cash(self, market: str = "ALL") -> float:
        cash = 0.0
        for mkt, bridge in self._bridges.items():
            if market != "ALL" and mkt != market:
                continue
            try:
                acc_info = bridge.query_account()
                if acc_info:
                    cash += acc_info.get('cash', 0)
            except Exception as e:
                logger.error(f"[Account] 查询 {mkt} 现金失败: {e}")
        return cash

    def get_buying_power(self, market: str = "ALL") -> float:
        power = 0.0
        for mkt, bridge in self._bridges.items():
            if market != "ALL" and mkt != market:
                continue
            try:
                acc_info = bridge.query_account()
                if acc_info:
                    bp = acc_info.get('power', 0)
                    power += bp if bp > 0 else acc_info.get('total_assets', 0)
            except Exception as e:
                logger.error(f"[Account] 查询 {mkt} 购买力失败: {e}")
        return power

    def get_market_value(self, market: str = "ALL") -> float:
        mv = 0.0
        for mkt, bridge in self._bridges.items():
            if market != "ALL" and mkt != market:
                continue
            try:
                acc_info = bridge.query_account()
                if acc_info:
                    mv += acc_info.get('market_val', 0)
            except Exception as e:
                logger.error(f"[Account] 查询 {mkt} 市值失败: {e}")
        return mv

    # ==================== 核心方法 ====================
    def get_available_capital(self, user_id: str = "SYSTEM", market: str = "ALL") -> float:
        buying_power = self.get_buying_power(market)
        external_positions = self._get_external_positions(user_id, market)
        external_value = sum(
            p.get('quantity', 0) * p.get('last_price', 0)
            for p in external_positions
        )
        user_strategy_value = self._calc_user_strategy_value(user_id, market)
        available = buying_power - external_value - user_strategy_value
        available = max(available, 0)
        self._cache[user_id] = {
            'buying_power': buying_power,
            'external_value': external_value,
            'strategy_value': user_strategy_value,
            'available': available,
            'updated_at': datetime.now().isoformat(),
        }
        self._last_sync[user_id] = datetime.now()
        logger.debug(f"[Account] {user_id}: power={buying_power:.0f} "
                     f"external={external_value:.0f} strategy={user_strategy_value:.0f} "
                     f"available={available:.0f}")
        return available

    def get_capital_summary(self, user_id: str = "SYSTEM") -> dict:
        if user_id in self._cache:
            return self._cache[user_id]
        return {
            'buying_power': self.get_buying_power(),
            'cash': self.get_cash(),
            'market_value': self.get_market_value(),
            'available': self.get_available_capital(user_id),
            'updated_at': datetime.now().isoformat(),
        }

    # ==================== 持仓同步 ====================
    def sync_all_positions(self, user_id: str = "SYSTEM"):
        """从所有网关同步持仓到本地数据库"""
        all_positions = []
        for mkt, bridge in self._bridges.items():
            try:
                positions = self._get_bridge_positions(bridge)
                for pos in positions:
                    pos['market'] = mkt
                    all_positions.append(pos)
            except Exception as e:
                logger.error(f"[Account] 同步 {mkt} 持仓失败: {e}")

        managed_symbols = self._get_managed_symbols(user_id)

        for pos in all_positions:
            symbol = pos.get('code', '')
            is_managed = symbol in managed_symbols
            # ★ 修复：用自定义 _upsert_position 替代 db.upsert_position
            self._upsert_position({
                'user_id': user_id,
                'symbol': symbol,
                'market': pos.get('market', ''),
                'quantity': pos.get('qty', 0),
                'avg_cost': pos.get('cost_price', 0),
                'last_price': pos.get('last_price', 0),
                'pnl': pos.get('pnl', 0),
                'is_managed': is_managed,
                'updated_at': datetime.now().isoformat(),
            })

        logger.info(f"[Account] 持仓同步完成: {len(all_positions)} 个持仓 (user={user_id})")

    def _get_bridge_positions(self, bridge) -> list:
        """从 bridge 获取持仓列表"""
        trade_ctx = getattr(bridge, 'trade_ctx', None) or getattr(bridge, 'trading_context', None)
        if trade_ctx is not None and hasattr(trade_ctx, 'position_list_query'):
            try:
                import futu
                ret, data = trade_ctx.position_list_query(
                    trd_env=futu.TrdEnv.SIMULATE, acc_id=0
                )
                if ret == 0 and data is not None and len(data) > 0:
                    positions = []
                    for _, row in data.iterrows():
                        positions.append({
                            'code': row.get('code', ''),
                            'qty': float(row.get('qty', 0)),
                            'can_sell_qty': float(row.get('can_sell_qty', 0)),
                            'cost_price': float(row.get('cost_price', 0)),
                            'last_price': float(row.get('nominal_price', 0)),
                            'pnl': float(row.get('pl_val', 0)),
                        })
                    return positions
            except Exception as e:
                logger.debug(f"[Account] trade_ctx.position_list_query 失败: {e}")

        for method_name in ('get_all_positions', 'get_positions', 'query_position'):
            method = getattr(bridge, method_name, None)
            if method is not None:
                try:
                    result = method()
                    if result:
                        return result if isinstance(result, list) else []
                except Exception as e:
                    logger.debug(f"[Account] {method_name}() 调用失败: {e}")
                    continue
        logger.warning("[Account] bridge 没有任何可用的持仓查询接口")
        return []

    def _get_managed_symbols(self, user_id: str) -> set:
        """获取系统管理的股票代码集合（兼容 db.get_managed_symbols）"""
        if hasattr(self.db, 'get_managed_symbols'):
            try:
                result = self.db.get_managed_symbols(user_id)
                if result:
                    return set(result)
            except Exception as e:
                logger.debug(f"[Account] db.get_managed_symbols 失败: {e}")

        # 回退：直接查询数据库
        try:
            session = self._get_db_session()
            result = session.execute(
                text("SELECT DISTINCT vt_symbol FROM strategy_config WHERE active=:active AND user_id=:uid"),
                {"active": 1, "uid": user_id}
            )
            return {row[0] for row in result.fetchall() if row[0]}
        except Exception as e:
            logger.debug(f"[Account] 回退查询 managed_symbols 失败: {e}")
        return set()

    def _get_external_positions(self, user_id: str, market: str = "ALL") -> list:
        if hasattr(self.db, 'get_external_positions'):
            try:
                return self.db.get_external_positions(user_id, market)
            except Exception as e:
                logger.debug(f"[Account] db.get_external_positions 失败: {e}")
        return []

    # ==================== 资金变更通知 ====================
    def on_equity_change(self, old_value: float, new_value: float):
        change = new_value - old_value
        pct = (change / old_value * 100) if old_value > 0 else 0
        logger.info(f"[Account] 💰 权益变更: {old_value:,.2f} → {new_value:,.2f} "
                    f"({change:+,.2f}, {pct:+.2f}%)")
        if abs(pct) > 5.0:
            self.db.log_event(
                timestamp=datetime.now().isoformat(),
                level="WARNING",
                message=f"账户权益大幅变动: {pct:+.2f}%",
                category="ACCOUNT"
            )

    # ==================== 内部方法 ====================
    def _calc_user_strategy_value(self, user_id: str, market: str) -> float:
        strategies = self._get_user_active_strategies(user_id, market)
        total = 0.0
        for s in strategies:
            pos_value = s.get('pos', 0) * s.get('last_price', 0)
            total += abs(pos_value)
        return total

    def _get_user_active_strategies(self, user_id: str, market: str) -> list:
        if hasattr(self.db, 'get_user_active_strategies'):
            try:
                return self.db.get_user_active_strategies(user_id, market)
            except Exception as e:
                logger.debug(f"[Account] db.get_user_active_strategies 失败: {e}")
        try:
            session = self._get_db_session()
            result = session.execute(
                text("SELECT vt_symbol, market, class_name FROM strategy_config WHERE active=:active AND user_id=:uid"),
                {"active": 1, "uid": user_id}
            )
            return [{'pos': 0, 'last_price': 0, 'vt_symbol': row[0]} for row in result.fetchall()]
        except Exception as e:
            logger.debug(f"[Account] 回退查询 user_active_strategies 失败: {e}")
        return []

    def force_refresh(self, user_id: str = "SYSTEM"):
        self._cache.pop(user_id, None)
        self._last_sync.pop(user_id, None)
        return self.get_available_capital(user_id)