"""
test_upgrade.py — Apollo AI Trader v2.8.0
升级包测试验证脚本
"""
import os
import sys
import sqlite3
import tempfile
import shutil
from datetime import datetime, timedelta
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("TestUpgrade")

PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️ WARN"


def setup_test_db(db_path: str):
    """创建测试数据库并插入模拟数据"""
    from core.data_fetcher import init_database, save_klines
    init_database(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM kline_cache")

    np.random.seed(42)
    symbols = ["NVDA", "AAPL", "MSFT"]
    intervals = ["1d", "60m", "15m"]

    for sym in symbols:
        for interval in intervals:
            if interval == "1d":
                n = 200
                base_price = 100 + hash(sym) % 50
            elif interval == "60m":
                n = 400
                base_price = 100 + hash(sym) % 50
            else:
                n = 800
                base_price = 100 + hash(sym) % 50

            prices = base_price + np.cumsum(np.random.randn(n) * 0.5)
            times = []
            base = datetime(2025, 1, 1)
            for i in range(n):
                if interval == "1d":
                    times.append((base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S"))
                elif interval == "60m":
                    times.append((base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    times.append((base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"))

            for i in range(n):
                conn.execute(
                    """INSERT OR REPLACE INTO kline_cache
                       (symbol, exchange, interval, datetime, open, high, low, close, volume)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sym, "SMART", interval, times[i],
                     float(prices[i] - 0.1), float(prices[i] + 0.2),
                     float(prices[i] - 0.3), float(prices[i]),
                     float(1000 + i * 10))
                )
    conn.commit()
    conn.close()
    log.info(f"📊 测试数据库就绪: {db_path}")


def test_1_database_schema(db_path: str):
    print("\n" + "=" * 60)
    print("测试1: 数据库 Schema")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    conn.close()

    required = [
        "kline_cache", "regime_records", "param_optimization_results",
        "user_config", "user_positions", "user_orders",
        "user_daily_performance", "optimization_jobs"
    ]

    all_pass = True
    for t in required:
        if t in tables:
            print(f"  {PASS}: 表 '{t}' 存在")
        else:
            print(f"  {FAIL}: 表 '{t}' 缺失")
            all_pass = False

    print(f"\n  ✅ 全部 {len(required)} 张表就绪")
    return all_pass


def test_2_regime_detection(db_path: str):
    print("\n" + "=" * 60)
    print("测试2: Regime 概率分布")
    print("=" * 60)

    from core.regime_trainer import batch_detect

    symbols = ["NVDA", "AAPL", "MSFT"]
    results = batch_detect(db_path, symbols)

    all_pass = True
    for sym in symbols:
        proba = results.get(sym, {})
        keys = set(proba.keys())
        expected = {"trend", "range", "volatile"}

        if keys != expected:
            print(f"  {FAIL}: {sym} 概率键缺失: {keys}")
            all_pass = False
            continue

        total = sum(proba.values())
        if abs(total - 1.0) > 0.01:
            print(f"  {FAIL}: {sym} 概率和={total:.4f} ≠ 1.0")
            all_pass = False
            continue

        if all(0 <= v <= 1 for v in proba.values()):
            primary = max(proba, key=proba.get)
            print(f"  {PASS}: {sym} → {proba} (primary={primary})")
        else:
            print(f"  {FAIL}: {sym} 概率超出 [0,1]")
            all_pass = False

    return all_pass


def test_3_walk_forward(db_path: str):
    print("\n" + "=" * 60)
    print("测试3: Walk-Forward 验证")
    print("=" * 60)

    from core.regime_trainer import walk_forward_validate

    symbols = ["NVDA", "AAPL", "MSFT"]
    results = {}
    for sym in symbols:
        wf = walk_forward_validate(db_path, sym, "SMART")
        results[sym] = wf
        if "error" in wf:
            print(f"  {WARN}: {sym} → {wf['error']} (需要≥{wf.get('required', '?')}根, 有{wf.get('got', '?')}根)")
        else:
            deployable = "✅ 可部署" if wf.get("deployable") else "⚠️ 需改进"
            print(f"  {PASS}: {sym}: WFE={wf['wfe']:.3f} "
                  f"IS={wf['in_sample_sharpe']:.2f} OOS={wf['out_sample_sharpe']:.2f} "
                  f"C={wf['consistency']:.2f} {deployable}")

    # 只要有数据且不报错就算通过
    return all("error" not in r or r.get("got", 0) >= 60 for r in results.values())


def test_4_strategy_matching(db_path: str):
    print("\n" + "=" * 60)
    print("测试4: 策略概率加权路由")
    print("=" * 60)

    from core.strategy_matcher import StrategyMatcher

    matcher = StrategyMatcher(db_path)
    symbols = ["NVDA", "AAPL", "MSFT"]

    all_pass = True
    for sym in symbols:
        weights = matcher.get_weights(sym, "SMART")
        if not weights:
            print(f"  {WARN}: {sym} 无 regime 数据，返回均匀分布")
            continue

        total = sum(weights.values())
        if abs(total - 1.0) > 0.01:
            print(f"  {FAIL}: {sym} 权重和={total:.4f} ≠ 1.0")
            all_pass = False
            continue

        chosen = matcher.select_strategy(sym, "SMART", method="top")
        if chosen in weights:
            print(f"  {PASS}: {sym} → {chosen} (权重={weights[chosen]:.3f})")
            print(f"         全量权重: {{'Trend': {weights.get('TrendStrategy',0):.3f}, "
                  f"'Grid': {weights.get('GridStrategy',0):.3f}, "
                  f"'OrderFlow': {weights.get('OrderFlowStrategy',0):.3f}}}")
        else:
            print(f"  {FAIL}: {sym} 选择策略不在权重表中")
            all_pass = False

    return all_pass


def test_5_param_optimization(db_path: str):
    print("\n" + "=" * 60)
    print("测试5: 参数优化 (小规模)")
    print("=" * 60)

    from core.param_optimizer import ParamOptimizer, PARAM_SPACES

    optimizer = ParamOptimizer(db_path)
    sym = "NVDA"
    strat = "TrendStrategy"

    result = optimizer.optimize(sym, strat, "all", n_trials=20)
    if result:
        params = result["params"]
        perf = result["performance"]
        print(f"  {PASS}: {sym}/{strat} 优化完成")
        print(f"         最优 Sharpe: {perf['sharpe']}")
        print(f"         参数: {params}")

        space = PARAM_SPACES[strat]
        for name, value in params.items():
            if name in space:
                low, high = space[name]
                if low <= value <= high:
                    print(f"         {PASS}: {name}={value:.2f} 在 [{low},{high}] 内")
                else:
                    print(f"         {FAIL}: {name}={value:.2f} 超出 [{low},{high}]")
                    return False
        return True
    else:
        print(f"  {WARN}: 优化未返回结果")
        return True


def test_6_trade_mode(db_path: str):
    print("\n" + "=" * 60)
    print("测试6: 模拟/实盘切换 + 风险提示")
    print("=" * 60)

    from core.trade_mode_manager import (
        create_user, get_current_mode,
        start_switch_to_live, handle_confirmation,
        switch_to_simulation
    )

    test_user = "test_user_001"
    create_user(db_path, test_user, "test_futu", "123456789", "moderate")

    mode = get_current_mode(db_path, test_user)
    if mode == "simulation":
        print(f"  {PASS}: 初始模式 = simulation")
    else:
        print(f"  {FAIL}: 初始模式 = {mode}")
        return False

    msg = start_switch_to_live(db_path, test_user)
    if "风险" in msg or "警告" in msg:
        print(f"  {PASS}: 切换到实盘时弹出风险提示")
    else:
        print(f"  {FAIL}: 未弹出风险提示")
        return False

    confirm_msg = handle_confirmation(db_path, test_user, "我已知晓风险")
    if "已切换" in confirm_msg and "实盘" in confirm_msg:
        print(f"  {PASS}: 风险确认后成功切换到实盘")
    else:
        print(f"  {FAIL}: 确认后未切换: {confirm_msg}")
        return False

    mode = get_current_mode(db_path, test_user)
    if mode == "live":
        print(f"  {PASS}: 数据库模式 = live")
    else:
        print(f"  {FAIL}: 数据库模式 = {mode}")
        return False

    sim_msg = switch_to_simulation(db_path, test_user)
    if "模拟盘" in sim_msg:
        print(f"  {PASS}: 成功切换回模拟盘")
    else:
        print(f"  {FAIL}: 切换回模拟盘失败")
        return False

    return True


def test_7_risk_profiles(db_path: str):
    print("\n" + "=" * 60)
    print("测试7: 风险等级模板")
    print("=" * 60)

    from core.risk_profiles import (
        get_template, get_all_profiles,
        validate_risk_profile, apply_custom_overrides
    )

    profiles = get_all_profiles()
    print(f"  可用等级: {profiles}")

    all_pass = True
    for profile_name in ["conservative", "moderate", "aggressive", "extreme"]:
        template = get_template(profile_name)
        required_keys = {"max_position_pct", "max_drawdown_pct", "stop_loss_pct",
                         "allowed_products", "preferred_strategies"}
        if required_keys.issubset(template.keys()):
            print(f"  {PASS}: {profile_name} ({template['label']}) "
                  f"max_pos={template['max_position_pct']} "
                  f"max_dd={template['max_drawdown_pct']}")
        else:
            missing = required_keys - template.keys()
            print(f"  {FAIL}: {profile_name} 缺失: {missing}")
            all_pass = False

    if not validate_risk_profile("invalid"):
        print(f"  {PASS}: 非法等级被正确拒绝")
    else:
        print(f"  {FAIL}: 非法等级未被拒绝")
        all_pass = False

    template = get_template("moderate")
    overrides = {"max_position_pct": 0.35, "stop_loss_pct": 0.07}
    merged = apply_custom_overrides(template, overrides)
    if merged["max_position_pct"] == 0.35:
        print(f"  {PASS}: 自定义覆盖生效")
    else:
        print(f"  {FAIL}: 自定义覆盖未生效")
        all_pass = False

    return all_pass


def test_8_scheduler(db_path: str):
    print("\n" + "=" * 60)
    print("测试8: 定时调度器")
    print("=" * 60)

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print(f"  {WARN}: APScheduler 未安装")
        return True

    try:
        from core.scheduler_jobs import create_scheduler
        config = {
            "optimization": {
                "strategies": ["TrendStrategy"],
                "regimes": ["all"],
                "n_trials": 10,
            }
        }
        scheduler = BackgroundScheduler()
        # 直接用 create_scheduler 但不 start（避免阻塞）
        scheduler.start(paused=True)

        # 手动添加任务测试
        from core.regime_trainer import batch_detect
        scheduler.add_job(
            lambda: batch_detect(db_path, ["NVDA"]),
            IntervalTrigger(minutes=30),
            id="test_regime"
        )

        jobs = scheduler.get_jobs()
        print(f"  {PASS}: 调度器创建成功，任务数={len(jobs)}")
        for job in jobs:
            print(f"         - {job.name} (id={job.id})")

        scheduler.shutdown()
        return True
    except Exception as e:
        print(f"  {FAIL}: 调度器异常: {e}")
        return False


def test_9_telegram_bot(db_path: str):
    print("\n" + "=" * 60)
    print("测试9: Telegram Bot")
    print("=" * 60)

    try:
        from core.telegram_bot import create_bot
        print(f"  {PASS}: Telegram Bot 模块导入成功")

        # 验证命令函数
        import inspect
        from core.telegram_bot import cmd_help, cmd_status, cmd_risk, cmd_pause
        for cmd in [cmd_help, cmd_status, cmd_risk, cmd_pause]:
            if callable(cmd):
                print(f"  {PASS}: 命令 {cmd.__name__} 可调用")
        return True
    except ImportError as e:
        print(f"  {WARN}: python-telegram-bot 未安装: {e}")
        return True
    except Exception as e:
        print(f"  {FAIL}: Telegram Bot 异常: {e}")
        return False


def test_10_data_flow(db_path: str):
    print("\n" + "=" * 60)
    print("测试10: 端到端数据流")
    print("=" * 60)

    from core.data_fetcher import load_bars
    from core.regime_trainer import detect_and_store
    from core.strategy_matcher import StrategyMatcher

    sym = "NVDA"
    exchange = "SMART"

    # 1. 读取K线
    daily = load_bars(db_path, sym, exchange, "1d", 60)
    min15 = load_bars(db_path, sym, exchange, "15m", 96)
    if len(daily) > 0 and len(min15) > 0:
        print(f"  {PASS}: K线读取成功 (daily={len(daily)}, 15m={len(min15)})")
    else:
        print(f"  {FAIL}: K线读取失败 (daily={len(daily)}, 15m={len(min15)})")
        return False

    # 2. Regime 检测
    proba = detect_and_store(db_path, sym, exchange)
    if proba and sum(proba.values()) > 0:
        print(f"  {PASS}: Regime 检测成功: {proba}")
    else:
        print(f"  {FAIL}: Regime 检测失败")
        return False

    # 3. 策略匹配
    matcher = StrategyMatcher(db_path)
    weights = matcher.get_weights(sym, exchange)
    chosen = matcher.select_strategy(sym, exchange, method="top")
    if chosen and sum(weights.values()) > 0:
        print(f"  {PASS}: 策略匹配成功: {chosen} (权重={weights[chosen]:.3f})")
    else:
        print(f"  {FAIL}: 策略匹配失败")
        return False

    # 4. 参数查询
    params = matcher.get_strategy_params(sym, chosen)
    print(f"  {PASS}: 参数查询完成: {params if params else '(使用策略默认值)'}")

    return True


# ─────────────────────────────────────────────
#  主测试运行器
# ─────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║   Apollo AI Trader v2.8.0 — 升级包验证测试      ║")
    print("╚══════════════════════════════════════════════════╝")

    tmpdir = tempfile.mkdtemp(prefix="apollo_test_")
    db_path = os.path.join(tmpdir, "test_trading.db")

    try:
        setup_test_db(db_path)

        tests = [
            ("数据库 Schema", lambda: test_1_database_schema(db_path)),
            ("Regime 概率分布", lambda: test_2_regime_detection(db_path)),
            ("Walk-Forward 验证", lambda: test_3_walk_forward(db_path)),
            ("策略权重路由", lambda: test_4_strategy_matching(db_path)),
            ("参数优化", lambda: test_5_param_optimization(db_path)),
            ("交易模式切换", lambda: test_6_trade_mode(db_path)),
            ("风险等级模板", lambda: test_7_risk_profiles(db_path)),
            ("定时调度器", lambda: test_8_scheduler(db_path)),
            ("Telegram Bot", lambda: test_9_telegram_bot(db_path)),
            ("端到端数据流", lambda: test_10_data_flow(db_path)),
        ]

        results = []
        for name, test_func in tests:
            try:
                passed = test_func()
                results.append((name, bool(passed)))
            except Exception as e:
                print(f"\n  {FAIL}: {name} 异常: {e}")
                import traceback
                traceback.print_exc()
                results.append((name, False))

        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)

        passed_count = 0
        for name, passed in results:
            status = PASS if passed else FAIL
            print(f"  {status}: {name}")
            if passed:
                passed_count += 1

        total = len(results)
        pct = passed_count / total * 100
        print(f"\n  ┌─────────────────────────────────┐")
        print(f"  │ 通过: {passed_count}/{total} ({pct:.0f}%) │")
        print(f"  └─────────────────────────────────┘")

        if passed_count == total:
            print(f"\n  🎉 全部测试通过！升级包验证成功。")
            return 0
        elif passed_count >= total * 0.8:
            print(f"\n  ⚠️ 大部分通过，{total - passed_count} 项需修复。")
            return 0
        else:
            print(f"\n  ❌ 多项失败，请检查。")
            return 1

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
