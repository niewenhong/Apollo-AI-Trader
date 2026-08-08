"""
core/user_manager.py - v3.8.0
多用户管理模块

功能：
- 用户注册/登录/权限管理
- 用户级策略与系统级策略的隔离与共享
- 用户资金账户管理
- 用户配置隔离

设计原则：
- 系统级策略（tier='SYSTEM'）对所有用户可见可用
- 用户级策略（tier='USER'）仅创建者可见
- 优秀用户策略可晋升为系统级策略
"""
import json
import logging
import hashlib
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("UserManager")


class UserRole(Enum):
    """用户角色"""
    ADMIN = "ADMIN"           # 系统管理员
    POWER = "POWER"           # 高级用户（可使用全部系统策略）
    STANDARD = "STANDARD"     # 标准用户
    TRIAL = "TRIAL"           # 试用用户（功能受限）


class UserStatus(Enum):
    """用户状态"""
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    PENDING = "PENDING"
    DELETED = "DELETED"


class UserManager:
    """
    多用户管理器

    职责：
    - 用户认证与权限
    - 策略可见性控制（用户级 vs 系统级）
    - 用户资金隔离
    - 优秀策略晋升为系统级
    """

    def __init__(self, db_manager):
        self.db = db_manager
        self._current_user: Optional[str] = None
        self._session_cache: Dict[str, dict] = {}

    # ==================== 用户管理 ====================

    def create_user(self, username: str, password: str,
                    role: UserRole = UserRole.STANDARD,
                    email: str = "",
                    initial_capital: float = 100000.0) -> Tuple[bool, str]:
        """创建新用户"""
        if self.db.user_exists(username):
            return False, f"用户名 {username} 已存在"

        user_id = str(uuid.uuid4())[:8]
        password_hash = self._hash_password(password)

        user_data = {
            'user_id': user_id,
            'username': username,
            'password_hash': password_hash,
            'role': role.value,
            'status': UserStatus.ACTIVE.value,
            'email': email,
            'initial_capital': initial_capital,
            'current_capital': initial_capital,
            'created_at': datetime.now().isoformat(),
            'last_login': None,
            'login_count': 0,
            'max_strategies': self._get_role_limits(role)['max_strategies'],
            'allowed_markets': json.dumps(self._get_role_limits(role)['markets']),
        }

        self.db.insert_user(user_data)
        logger.info(f"[UserManager] ✅ 用户创建: {username} (role={role.value}, id={user_id})")
        return True, user_id

    def authenticate(self, username: str, password: str) -> Tuple[bool, str]:
        """用户认证，返回 (成功, user_id)"""
        user = self.db.get_user_by_name(username)
        if not user:
            return False, "用户不存在"

        if user.get('status') != UserStatus.ACTIVE.value:
            return False, f"用户状态: {user.get('status')}"

        if user.get('password_hash') != self._hash_password(password):
            return False, "密码错误"

        # 更新登录信息
        self.db.update_user_login(user['user_id'])
        self._current_user = user['user_id']
        self._session_cache[user['user_id']] = user

        logger.info(f"[UserManager] 🔑 用户登录: {username} (id={user['user_id']})")
        return True, user['user_id']

    def logout(self):
        """注销当前用户"""
        if self._current_user:
            logger.info(f"[UserManager] 👋 用户注销: {self._current_user}")
            self._current_user = None

    def get_current_user(self) -> Optional[str]:
        """获取当前登录用户ID"""
        return self._current_user

    def require_auth(self) -> Optional[str]:
        """要求已认证，返回 user_id 或 None"""
        if not self._current_user:
            logger.warning("[UserManager] 需要用户认证")
            return None
        return self._current_user

    # ==================== 策略可见性 ====================

    def get_visible_strategies(self, user_id: str) -> List[dict]:
        """
        获取用户可见的策略列表
        - 系统级策略（owner='SYSTEM'）：所有用户可见
        - 用户级策略（owner=user_id）：仅创建者可见
        """
        all_strategies = self.db.get_all_strategies()
        visible = []
        for s in all_strategies:
            owner = s.get('owner', 'SYSTEM')
            if owner == 'SYSTEM':
                visible.append(s)  # 系统策略对所有人可见
            elif owner == user_id:
                visible.append(s)  # 自己的策略可见
        return visible

    def can_use_strategy(self, user_id: str, strategy_name: str) -> Tuple[bool, str]:
        """检查用户是否可以使用某个策略"""
        strategy = self.db.get_strategy(strategy_name)
        if not strategy:
            return False, "策略不存在"

        owner = strategy.get('owner', 'SYSTEM')
        if owner == 'SYSTEM':
            return True, "系统级策略"

        if owner == user_id:
            return True, "用户自有策略"

        # 检查是否通过分享获得权限
        if self.db.check_strategy_share(strategy_name, user_id):
            return True, "已授权"

        return False, "无权限使用该策略"

    def share_strategy(self, strategy_name: str, from_user: str,
                       to_user: str) -> Tuple[bool, str]:
        """用户分享策略给其他用户"""
        # 验证分享者拥有该策略
        can_use, reason = self.can_use_strategy(from_user, strategy_name)
        if not can_use:
            return False, reason

        self.db.add_strategy_share(strategy_name, from_user, to_user)
        logger.info(f"[UserManager] 🔗 {from_user} 分享 {strategy_name} → {to_user}")
        return True, "分享成功"

    # ==================== 策略晋升 ====================

    def promote_to_system(self, strategy_name: str,
                          promoted_by: str) -> Tuple[bool, str]:
        """
        将优秀用户策略晋升为系统级策略
        晋升条件：score≥80 且运行≥30天 且交易≥100笔
        """
        strategy = self.db.get_strategy(strategy_name)
        if not strategy:
            return False, "策略不存在"

        score = strategy.get('trial_score', 0) or 0
        days = self._days_since(strategy.get('created_at', ''))
        trades = strategy.get('total_trades', 0) or 0

        if score < 80:
            return False, f"评分不足 ({score:.0f}<80)"
        if days < 30:
            return False, f"运行时间不足 ({days}<30天)"
        if trades < 100:
            return False, f"交易样本不足 ({trades}<100笔)"

        # 晋升
        self.db.promote_strategy_to_system(strategy_name, promoted_by)
        logger.info(f"[UserManager] 🏆 策略晋升为系统级: {strategy_name} (score={score:.0f})")
        return True, "晋升成功"

    def demote_to_user(self, strategy_name: str,
                       new_owner: str) -> Tuple[bool, str]:
        """将系统级策略降级为用户级"""
        strategy = self.db.get_strategy(strategy_name)
        if not strategy:
            return False, "策略不存在"

        self.db.demote_strategy_to_user(strategy_name, new_owner)
        logger.info(f"[UserManager] 📉 策略降级为用户级: {strategy_name} → {new_owner}")
        return True, "降级成功"

    # ==================== 用户资金 ====================

    def get_user_capital(self, user_id: str) -> dict:
        """获取用户资金信息"""
        return self.db.get_user_capital(user_id)

    def update_user_capital(self, user_id: str, current_capital: float):
        """更新用户当前资金"""
        self.db.update_user_capital(user_id, current_capital)

    def get_user_strategy_count(self, user_id: str) -> int:
        """获取用户当前策略数量"""
        return self.db.count_user_strategies(user_id)

    def can_create_strategy(self, user_id: str) -> Tuple[bool, str]:
        """检查用户是否还能创建新策略"""
        user = self.db.get_user(user_id)
        if not user:
            return False, "用户不存在"

        max_s = user.get('max_strategies', 10)
        current = self.get_user_strategy_count(user_id)
        if current >= max_s:
            return False, f"已达策略上限 ({current}/{max_s})"
        return True, f"可创建 ({current}/{max_s})"

    # ==================== 权限检查 ====================

    def check_permission(self, user_id: str, action: str) -> Tuple[bool, str]:
        """
        检查用户是否有权限执行某操作
        action: 'CREATE_STRATEGY', 'USE_SYSTEM_STRATEGY',
                'PROMOTE_STRATEGY', 'MANAGE_USERS', 'VIEW_ALL'
        """
        user = self.db.get_user(user_id)
        if not user:
            return False, "用户不存在"

        role = UserRole(user.get('role', 'STANDARD'))

        permissions = {
            UserRole.ADMIN: ['CREATE_STRATEGY', 'USE_SYSTEM_STRATEGY',
                              'PROMOTE_STRATEGY', 'MANAGE_USERS', 'VIEW_ALL',
                              'DELETE_STRATEGY', 'MODIFY_SYSTEM'],
            UserRole.POWER: ['CREATE_STRATEGY', 'USE_SYSTEM_STRATEGY',
                              'PROMOTE_STRATEGY', 'VIEW_ALL'],
            UserRole.STANDARD: ['CREATE_STRATEGY', 'USE_SYSTEM_STRATEGY'],
            UserRole.TRIAL: ['USE_SYSTEM_STRATEGY'],  # 试用用户只能用系统策略
        }

        allowed = permissions.get(role, [])
        if action in allowed:
            return True, f"允许 ({role.value})"
        return False, f"无权限 ({role.value} 不能 {action})"

    # ==================== 工具方法 ====================

    @staticmethod
    def _hash_password(password: str) -> str:
        """SHA-256 密码哈希"""
        return hashlib.sha256(password.encode('utf-8')).hexdigest()

    @staticmethod
    def _get_role_limits(role: UserRole) -> dict:
        """各角色的资源限制"""
        limits = {
            UserRole.ADMIN: {'max_strategies': 999, 'markets': ['US', 'HK', 'FUTURES', 'OPTIONS']},
            UserRole.POWER: {'max_strategies': 50, 'markets': ['US', 'HK', 'FUTURES', 'OPTIONS']},
            UserRole.STANDARD: {'max_strategies': 20, 'markets': ['US', 'HK']},
            UserRole.TRIAL: {'max_strategies': 5, 'markets': ['US']},
        }
        return limits.get(role, limits[UserRole.STANDARD])

    @staticmethod
    def _days_since(date_str: str) -> int:
        if not date_str:
            return 0
        try:
            dt = datetime.fromisoformat(date_str)
            return (datetime.now() - dt).days
        except:
            return 0

    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        return self.db.get_user(user_id)

    def list_all_users(self) -> List[dict]:
        """列出所有用户（仅ADMIN可用）"""
        return self.db.get_all_users()
