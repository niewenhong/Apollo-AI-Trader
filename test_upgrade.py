"""
test_upgrade.py - v3.8.0
升级验证脚本

验证项目：
1. 所有模块可正常导入
2. 数据库迁移成功
3. 多用户功能正常
4. 策略生命周期管理正常
5. 外部持仓接管正常
"""
import sys
import os
import sqlite3
import logging
from datetime import datetime

# 设置工作目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(levelname)-7s | %(name)s | %(message)s')
logger = logging.getLogger("TestUpgrade")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results = []

def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    msg = f"  {status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    results.append((name, condition))
    return condition


def test_imports():
    """测试1: 所有模块可导入"""
    print("\n📦 测试1: 模块导入")
    print("-" * 60)

    try:
        from core import __version__ as core_ver
        check("core 包版本", core_ver == "3.8.0", f"v{core_ver}")
    except Exception as e:
        check("core 包版本", False, str(e))

    try:
        from strategies import __version__ as strat_ver
        check("strategies 包版本", strat_ver == "3.8.0", f"v{strat_ver}")
    except Exception as e:
        check("strategies 包版本", False, str(e))

    try:
        from core.db_manager import DBManager
        check("DBManager 导入", True)
    except Exception as e:
        check("DBManager 导入", False, str(e))

    try:
        from core.user_manager import UserManager, UserRole
        check("UserManager 导入", True)
    except Exception as e:
        check("UserManager 导入", False, str(e))

    try:
        from core.account_manager import AccountManager
        check("AccountManager 导入", True)
    except Exception as e:
        check("AccountManager 导入", False, str(e))

    try:
        from core.strategy_lifecycle_manager import (
            StrategyLifecycleManager, StrategyTier, LifecycleAction
        )
        check("StrategyLifecycleManager 导入", True)
    except Exception as e:
        check("StrategyLifecycleManager 导入", False, str(e))

    try:
        from core.strategy_engine import StrategyEngine
        check("StrategyEngine 导入", True)
    except Exception as e:
        check("StrategyEngine 导入", False, str(e))

    try:
        from core.scheduler_jobs import SchedulerJobs
        check("SchedulerJobs 导入", True)
    except Exception as e:
        check("SchedulerJobs 导入", False, str(e))

    try:
        from strategies.base_strategy import BaseStrategy
        check("BaseStrategy 导入", True)
    except Exception as e:
        check("BaseStrategy 导入", False, str(e))

    try:
        from strategies.strategy_factory import StrategyFactory
        check("StrategyFactory 导入", True)
    except Exception as e:
        check("StrategyFactory 导入", False, str(e))

    try:
        from strategies.equity.managed_position_strategy import ManagedPositionStrategy
        check("ManagedPositionStrategy 导入", True)
    except Exception as e:
        check("ManagedPositionStrategy 导入", False, str(e))

    try:
        from strategies.equity.trend_strategy import TrendStrategy
        check("TrendStrategy 导入", True)
    except Exception as e:
        check("TrendStrategy 导入", False, str(e))


def test_database(db_path: str = "data/apollo.db"):
    """测试2: 数据库结构"""
    print("\n🗄️ 测试2: 数据库结构")
    print("-" * 60)

    if not os.path.exists(db_path):
        # 使用内存数据库测试
        conn = sqlite3.connect(":memory:")
        check(f"数据库存在", False, f"{db_path} 不存在，使用内存DB")
    else:
        conn = sqlite3.connect(db_path)
        check(f"数据库存在", True, db_path)

    conn.row_factory = sqlite3.Row

    # 检查表
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    expected_tables = ['users', 'strategy_config', 'strategy_shares',
                      'strategy_events', 'positions', 'strategy_performance']
    for t in expected_tables:
        check(f"表 {t}", t in tables, f"({'✓' if t in tables else '✗'})")

    # 检查 strategy_config 字段
    if 'strategy_config' in tables:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(strategy_config)").fetchall()]
        for col in ['owner', 'user_id', 'tier', 'trial_score',
                     'optimize_count', 'scale_factor', 'is_adopt']:
            check(f"字段 strategy_config.{col}", col in columns)

    # 检查 users 字段
    if 'users' in tables:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        for col in ['user_id', 'username', 'password_hash', 'role', 'status']:
            check(f"字段 users.{col}", col in columns)

    conn.close()


def test_user_manager():
    """测试3: 多用户管理"""
    print("\n👥 测试3: 多用户管理")
    print("-" * 60)

    try:
        from core.db_manager import DBManager
        from core.user_manager import UserManager, UserRole

        db = DBManager(":memory:")
        um = UserManager(db)

        # 创建用户
        ok, msg = um.create_user("testuser", "password123", UserRole.STANDARD)
        check("创建标准用户", ok, msg)

        ok, msg = um.create_user("poweruser", "pass456", UserRole.POWER)
        check("创建高级用户", ok, msg)

        ok, msg = um.create_user("testuser", "dup", UserRole.STANDARD)
        check("重复用户名拒绝", not ok, msg)

        # 认证
        ok, uid = um.authenticate("testuser", "password123")
        check("用户认证成功", ok, f"uid={uid}")

        ok, uid = um.authenticate("testuser", "wrongpass")
        check("错误密码拒绝", not ok, uid)

        # 权限
        can, reason = um.check_permission(uid, "CREATE_STRATEGY")
        check("标准用户可创建策略", can, reason)

        can, reason = um.check_permission(uid, "PROMOTE_STRATEGY")
        check("标准用户不可晋升策略", not can, reason)

        # 管理员
        ok, admin_uid = um.authenticate("admin", "admin123")
        if ok:
            can, reason = um.check_permission(admin_uid, "MANAGE_USERS")
            check("管理员可管理用户", can, reason)
        else:
            check("管理员可管理用户", False, "admin 不存在")

    except Exception as e:
        check("多用户管理测试", False, f"异常: {e}")
        import traceback
        traceback.print_exc()


def test_lifecycle_manager():
    """测试4: 策略生命周期"""
    print("\n🔄 测试4: 策略生命周期")
    print("-" * 60)

    try:
        from core.db_manager import DBManager
        from core.strategy_lifecycle_manager import (
            StrategyLifecycleManager, StrategyTier
        )

        db = DBManager(":memory:")
        slm = StrategyLifecycleManager(db)

        # 测试评分
        score = slm._calculate_score("nonexistent")
        check("空策略返回中性分", score == 50.0, f"score={score}")

        # 测试资金比例
        for tier_name, expected in [
            ('TRIAL', 0.03), ('FORMAL', 0.10), ('CORE', 0.20),
            ('ADOPT', 0.05), ('DEPRECATED', 0.0)
        ]:
            tier = StrategyTier(tier_name)
            ratio = slm.TIER_CAPITAL_RATIO[tier]
            check(f"层级 {tier_name} 资金比例", ratio == expected,
                  f"{ratio*100:.0f}%")

        # 测试天数计算
        days = slm._days_since("2026-01-01T00:00:00")
        check("日期差计算", days > 0, f"{days}天前")

        # 测试缓存清理
        slm.clear_cache()
        check("缓存清理", len(slm._strategy_cache) == 0)

    except Exception as e:
        check("生命周期测试", False, f"异常: {e}")
        import traceback
        traceback.print_exc()


def test_config():
    """测试5: 配置加载"""
    print("\n⚙️ 测试5: 配置加载")
    print("-" * 60)

    try:
        from core.config_loader import load_config, get_nested, set_nested

        config = load_config("config/system_config.json")
        check("加载 system_config.json", isinstance(config, dict))

        version = config.get("version", "")
        check("配置版本 3.8.0", version == "3.8.0", f"v{version}")

        # 检查 lifecycle 配置
        lc = config.get("lifecycle", {})
        check("lifecycle 配置存在", len(lc) > 0)
        check("promotion_score 存在", "promotion_score" in lc)
        check("min_trial_trades 存在", "min_trial_trades" in lc)

        # 检查 users 配置
        uc = config.get("users", {})
        check("users 配置存在", len(uc) > 0)

        # 检查 scheduler 配置
        sc = config.get("scheduler", {})
        check("scheduler 配置存在", len(sc) > 0)
        check("evaluate_interval_minutes 存在", "evaluate_interval_minutes" in sc)

        # 嵌套获取
        val = get_nested(config, "risk.max_daily_loss_pct")
        check("嵌套配置获取", val is not None)

    except Exception as e:
        check("配置测试", False, f"异常: {e}")
        import traceback
        traceback.print_exc()


def test_managed_position():
    """测试6: 外部持仓接管"""
    print("\n🔄 测试6: 外部持仓接管")
    print("-" * 60)

    try:
        from strategies.equity.managed_position_strategy import ManagedPositionStrategy
        from strategies.base_strategy import BaseStrategy

        check("ManagedPositionStrategy 继承 BaseStrategy",
              issubclass(ManagedPositionStrategy, BaseStrategy))

        # 检查关键方法
        for method in ['on_init', 'on_start', 'on_1m_bar', 'on_trade', 'buy', 'short']:
            check(f"ManagedPositionStrategy.{method}", hasattr(ManagedPositionStrategy, method))

        # 检查 is_adopt 标记
        params = ManagedPositionStrategy.parameters
        check("is_adopt 在 parameters 中", "is_adopt" in params)

    except Exception as e:
        check("接管策略测试", False, f"异常: {e}")
        import traceback
        traceback.print_exc()


def test_base_strategy():
    """测试7: BaseStrategy 增强"""
    print("\n📋 测试7: BaseStrategy 增强")
    print("-" * 60)

    try:
        from strategies.base_strategy import BaseStrategy

        # 检查新增参数
        for param in ['user_id', 'is_adopt']:
            check(f"参数 {param}", param in BaseStrategy.parameters)

        # 检查生命周期方法
        for method in ['buy', 'sell', 'short', 'cover']:
            check(f"方法 {method} 存在", hasattr(BaseStrategy, method))

        # 检查 calc_position_size
        check("calc_position_size 存在", hasattr(BaseStrategy, 'calc_position_size'))

    except Exception as e:
        check("BaseStrategy 测试", False, f"异常: {e}")
        import traceback
        traceback.print_exc()


def main():
    print("=" * 60)
    print(f"🧪 Apollo AI Trader v3.8.0 升级验证")
    print(f"   时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    test_imports()
    test_config()
    test_database()
    test_user_manager()
    test_lifecycle_manager()
    test_base_strategy()
    test_managed_position()

    # 汇总
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"📊 汇总: {passed}/{total} 通过, {failed} 失败")
    if failed > 0:
        print(f"\n❌ 失败项:")
        for name, ok in results:
            if not ok:
                print(f"   • {name}")
        print(f"\n⚠️ 请修复上述问题后再启动系统")
        sys.exit(1)
    else:
        print(f"\n🎉 所有测试通过！系统已就绪，可以启动。")
        print(f"   启动命令: python main.py")


if __name__ == "__main__":
    main()
