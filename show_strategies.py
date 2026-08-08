"""
show_strategies.py - v3.8.0
显示所有策略及其状态（支持多用户）

新增功能：
- 显示策略层级（TRIAL/FORMAL/CORE/SYSTEM/ADOPT）
- 显示所属用户
- 显示评分和绩效
- 显示资金比例
"""
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("ShowStrategies")


def show_all(db_path: str = "data/apollo.db"):
    """显示所有策略"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("=" * 100)
    print(f"📊 Apollo AI Trader v3.8.0 - 策略总览")
    print(f"   时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 100)

    # ─── 用户列表 ───
    users = conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    print(f"\n👥 用户 ({len(users)} 个):")
    print(f"  {'用户名':<15} {'角色':<10} {'状态':<10} {'资金':>15} {'策略数':>8}")
    print(f"  {'-'*60}")
    for u in users:
        strategy_count = conn.execute(
            "SELECT COUNT(*) FROM strategy_config WHERE user_id=?",
            (u['user_id'],)
        ).fetchone()[0]
        print(f"  {u['username']:<15} {u['role']:<10} {u['status']:<10} "
              f"{u['current_capital']:>15,.2f} {strategy_count:>8}")

    # ─── 策略统计 ───
    print(f"\n📋 策略统计:")
    tier_stats = conn.execute(
        """SELECT tier, COUNT(*) as cnt,
           SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) as active
           FROM strategy_config GROUP BY tier"""
    ).fetchall()

    for row in tier_stats:
        tier = row['tier'] or 'UNKNOWN'
        pct = (row['active'] / row['cnt'] * 100) if row['cnt'] > 0 else 0
        print(f"  {tier:<12} 总计:{row['cnt']:>4}  活跃:{row['active']:>4}  "
              f"占比:{pct:>5.1f}%")

    # ─── 详细策略列表 ───
    strategies = conn.execute(
        """SELECT sc.*, sp.sharpe_ratio, sp.profit_factor, sp.win_rate,
           sp.max_drawdown, sp.total_trades
           FROM strategy_config sc
           LEFT JOIN strategy_performance sp ON sc.strategy_name = sp.strategy_name
           ORDER BY sc.tier, sc.owner, sc.strategy_name"""
    ).fetchall()

    print(f"\n📦 策略详情 ({len(strategies)} 个):")
    print(f"  {'策略名称':<35} {'层级':<8} {'所有者':<10} {'评分':>6} "
          f"{'交易':>6} {'夏普':>7} {'PF':>6} {'胜率':>6} {'回撤':>7} {'状态':>6}")
    print(f"  {'-'*100}")

    for s in strategies:
        name = s['strategy_name'][:34]
        tier = (s['tier'] or '?')[:7]
        owner = (s['owner'] or '?')[:9]
        score = s['trial_score'] or 0
        trades = s['total_trades'] or 0
        sharpe = s['sharpe_ratio'] or 0
        pf = s['profit_factor'] or 0
        wr = (s['win_rate'] or 0) * 100
        dd = (s['max_drawdown'] or 0) * 100
        status = "✅" if s['enabled'] else "⏸"

        print(f"  {name:<35} {tier:<8} {owner:<10} {score:>6.0f} "
              f"{trades:>6} {sharpe:>7.2f} {pf:>6.2f} {wr:>5.1f}% {dd:>6.1f}% {status:>6}")

    # ─── 最近事件 ───
    events = conn.execute(
        "SELECT * FROM strategy_events ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()

    if events:
        print(f"\n📝 最近事件 (10条):")
        for e in events:
            print(f"  [{e['timestamp'][:16]}] [{e['level']:<7}] "
                  f"{e['strategy_name'] or '':<30} {e['message'][:50]}")

    # ─── 持仓概览 ───
    positions = conn.execute(
        "SELECT user_id, COUNT(*) as cnt, SUM(quantity) as qty "
        "FROM positions GROUP BY user_id"
    ).fetchall()

    if positions:
        print(f"\n📈 持仓概览:")
        for p in positions:
            print(f"  {p['user_id']:<15} 标的:{p['cnt']:>4}  总数量:{p['qty']:>10,.0f}")

    print("\n" + "=" * 100)

    conn.close()


def show_user(db_path: str, user_id: str):
    """显示某用户的策略"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not user:
        print(f"❌ 用户不存在: {user_id}")
        conn.close()
        return

    print(f"\n👤 用户: {user['username']} ({user['role']})")
    print(f"   资金: {user['current_capital']:,.2f}  策略上限: {user['max_strategies']}")

    strategies = conn.execute(
        "SELECT * FROM strategy_config WHERE user_id=? ORDER BY tier, strategy_name",
        (user_id,)
    ).fetchall()

    print(f"\n   策略 ({len(strategies)} 个):")
    for s in strategies:
        print(f"   • {s['strategy_name']:<35} {s['tier']:<10} "
              f"score={s['trial_score'] or 0:.0f}  enabled={s['enabled']}")

    conn.close()


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "data/apollo.db"
    user = sys.argv[2] if len(sys.argv) > 2 else None

    if user:
        show_user(db, user)
    else:
        show_all(db)
