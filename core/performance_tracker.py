# -*- coding: utf-8 -*-
"""
core/performance_tracker.py - Apollo Trader v3.2.0 绩效跟踪器
============================================================
变更：
  v3.2.0 - 支持多策略独立跟踪（dict of PerformanceTracker）
            新增 Sharpe / Sortino / Calmar 比率
            对接 vnpy CtaTemplate.trades 自动采集
            后台线程定时采集 + 写入 DB
            参考 vnpy CtaStrategy 绩效面板实现思路
"""
import logging
import threading
import time
import uuid
from datetime import datetime, date
from typing import Dict, List, Optional
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger("core.performance_tracker")


class SingleStrategyTracker:
    """单个策略的绩效跟踪（对标 vnpy CtaStrategy 绩效面板）"""

    def __init__(self, strategy_name: str, window: int = 252):
        self.strategy_name = strategy_name
        self._equity_curve: deque = deque(maxlen=window * 10)
        self._trade_pnls: List[float] = []
        self._trade_pnl_history: deque = deque(maxlen=10000)
        self._daily_returns: List[float] = []
        self._high_water_mark: float = 0.0
        self._current_equity: float = 0.0
        self._current_drawdown: float = 0.0
        self._max_drawdown: float = 0.0
        self._last_equity: float = 0.0
        self._last_trade_count: int = 0
        self._start_equity: float = 0.0
        self._start_time: Optional[str] = None
        self._lock = threading.Lock()

    def set_start_equity(self, equity: float):
        with self._lock:
            self._start_equity = equity
            self._current_equity = equity
            self._last_equity = equity
            self._high_water_mark = equity
            if self._start_time is None:
                self._start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def update_equity(self, equity: float, timestamp: Optional[str] = None):
        """更新权益（由外部定时调用）"""
        with self._lock:
            self._current_equity = equity
            if equity > self._high_water_mark:
                self._high_water_mark = equity

            dd = 0.0
            if self._high_water_mark > 0:
                dd = (self._high_water_mark - equity) / self._high_water_mark
            self._current_drawdown = dd
            if dd > self._max_drawdown:
                self._max_drawdown = dd

            # 收益率
            if self._last_equity > 0:
                ret = (equity - self._last_equity) / self._last_equity
                self._daily_returns.append(ret)

            self._equity_curve.append((timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), equity))
            self._last_equity = equity

    def add_trade(self, pnl: float, price: float = 0, volume: float = 0):
        """添加一笔交易盈亏"""
        with self._lock:
            self._trade_pnls.append(pnl)
            self._trade_pnl_history.append({
                'pnl': pnl, 'price': price, 'volume': volume,
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

    def add_trades_batch(self, trades_dict: dict):
        """
        从 vnpy CtaTemplate.trades 批量更新
        trades_dict: {trade_id: TradeData}
        """
        with self._lock:
            current_count = len(self._trade_pnls)
            new_count = len(trades_dict)
            if new_count > current_count:
                # 有新增交易，尝试计算新交易的 PnL
                # vnpy TradeData 有 price, volume 字段
                new_trades = list(trades_dict.values())[current_count:]
                for trade in new_trades:
                    price = getattr(trade, 'price', 0) or 0
                    volume = getattr(trade, 'volume', 0) or 0
                    # 简单估算：用价格变化（需要成本价才能精确计算）
                    pnl = getattr(trade, 'pnl', None)
                    if pnl is not None:
                        self._trade_pnls.append(float(pnl))
                        self._trade_pnl_history.append({
                            'pnl': float(pnl), 'price': price, 'volume': volume,
                            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                    elif price > 0 and volume > 0:
                        # 如果没有 pnl 字段，记录价格和数量供后续计算
                        self._trade_pnl_history.append({
                            'pnl': 0, 'price': price, 'volume': volume,
                            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'needs_calc': True
                        })

    def get_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """年化 Sharpe 比率（参考 vnpy 绩效面板）"""
        with self._lock:
            if len(self._daily_returns) < 20:
                return 0.0
            returns = np.array(self._daily_returns)
            excess = returns - (risk_free_rate / 252.0)
            std = excess.std()
            if std == 0:
                return 0.0
            sharpe = excess.mean() / std * np.sqrt(252)
            return round(float(sharpe), 2)

    def get_sortino_ratio(self, risk_free_rate: float = 0.02) -> float:
        """Sortino 比率（只衡量下行风险）"""
        with self._lock:
            if len(self._daily_returns) < 20:
                return 0.0
            returns = np.array(self._daily_returns)
            excess = returns - (risk_free_rate / 252.0)
            downside = excess[excess < 0]
            if len(downside) == 0 or downside.std() == 0:
                return 0.0
            sortino = excess.mean() / downside.std() * np.sqrt(252)
            return round(float(sortino), 2)

    def get_calmar_ratio(self) -> float:
        """Calmar 比率（年化收益 / 最大回撤）"""
        with self._lock:
            if self._max_drawdown == 0 or len(self._equity_curve) < 20:
                return 0.0
            # 粗略年化：用总收益 / 天数 * 252
            if self._start_equity > 0:
                total_return = (self._current_equity - self._start_equity) / self._start_equity
                days = max(len(self._equity_curve) / 390, 1)  # 假设每天390分钟
                annualized = total_return / days * 252
                calmar = annualized / self._max_drawdown
                return round(float(calmar), 2)
            return 0.0

    def get_max_drawdown_pct(self) -> float:
        with self._lock:
            return round(self._max_drawdown * 100.0, 2)

    def get_current_drawdown_pct(self) -> float:
        with self._lock:
            return round(self._current_drawdown * 100.0, 2)

    def get_win_rate(self) -> float:
        """胜率"""
        with self._lock:
            if not self._trade_pnls:
                return 0.0
            wins = sum(1 for p in self._trade_pnls if p > 0)
            return round((wins / len(self._trade_pnls)) * 100.0, 2)

    def get_profit_factor(self) -> float:
        """盈亏比 = 总盈利 / 总亏损"""
        with self._lock:
            gross_profit = sum(p for p in self._trade_pnls if p > 0)
            gross_loss = abs(sum(p for p in self._trade_pnls if p < 0))
            if gross_loss == 0:
                return float('inf') if gross_profit > 0 else 0.0
            return round(gross_profit / gross_loss, 2)

    def get_total_pnl(self) -> float:
        with self._lock:
            return round(sum(self._trade_pnls), 2)

    def get_trade_count(self) -> int:
        with self._lock:
            return len(self._trade_pnls)

    def get_winning_trades(self) -> int:
        with self._lock:
            return sum(1 for p in self._trade_pnls if p > 0)

    def get_losing_trades(self) -> int:
        with self._lock:
            return sum(1 for p in self._trade_pnls if p < 0)

    def get_avg_win(self) -> float:
        with self._lock:
            wins = [p for p in self._trade_pnls if p > 0]
            return round(sum(wins) / len(wins), 2) if wins else 0.0

    def get_avg_loss(self) -> float:
        with self._lock:
            losses = [p for p in self._trade_pnls if p < 0]
            return round(sum(losses) / len(losses), 2) if losses else 0.0

    def get_summary(self) -> dict:
        with self._lock:
            return {
                "strategy_name": self.strategy_name,
                "total_pnl": self.get_total_pnl(),
                "total_trades": self.get_trade_count(),
                "winning_trades": self.get_winning_trades(),
                "losing_trades": self.get_losing_trades(),
                "win_rate": self.get_win_rate(),
                "avg_win": self.get_avg_win(),
                "avg_loss": self.get_avg_loss(),
                "max_drawdown": self.get_max_drawdown_pct(),
                "current_drawdown": self.get_current_drawdown_pct(),
                "sharpe_ratio": self.get_sharpe_ratio(),
                "sortino_ratio": self.get_sortino_ratio(),
                "calmar_ratio": self.get_calmar_ratio(),
                "profit_factor": self.get_profit_factor(),
                "current_equity": self._current_equity,
                "start_equity": self._start_equity,
            }


class PerformanceTracker:
    """
    多策略绩效跟踪管理器
    - 每个策略独立跟踪
    - 后台线程定时采集
    - 自动写入数据库
    """
    COLLECT_INTERVAL = 60  # 采集间隔（秒）

    def __init__(self, db=None, strategy_engine=None):
        self._db = db
        self._engine = strategy_engine
        self._trackers: Dict[str, SingleStrategyTracker] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ==================== 对外接口 ====================

    def register_strategy(self, strategy_name: str, start_equity: float = 0.0):
        """注册一个策略的跟踪器"""
        with self._lock:
            if strategy_name not in self._trackers:
                tracker = SingleStrategyTracker(strategy_name)
                if start_equity > 0:
                    tracker.set_start_equity(start_equity)
                self._trackers[strategy_name] = tracker
                logger.info(f"[Perf] 📊 注册跟踪: {strategy_name}")
            else:
                if start_equity > 0 and self._trackers[strategy_name]._start_equity == 0:
                    self._trackers[strategy_name].set_start_equity(start_equity)

    def unregister_strategy(self, strategy_name: str):
        """注销一个策略的跟踪器"""
        with self._lock:
            self._trackers.pop(strategy_name, None)
            logger.info(f"[Perf] 🔚 注销跟踪: {strategy_name}")

    def update_equity(self, strategy_name: str, equity: float):
        """更新某策略权益"""
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                tracker = SingleStrategyTracker(strategy_name)
                tracker.set_start_equity(equity)
                self._trackers[strategy_name] = tracker
            else:
                tracker.update_equity(equity)

    def add_trade_pnl(self, strategy_name: str, pnl: float):
        """添加某策略的交易盈亏"""
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                tracker = SingleStrategyTracker(strategy_name)
                self._trackers[strategy_name] = tracker
            tracker.add_trade(pnl)

    def sync_from_engine(self):
        """
        ★ 核心方法：从 StrategyEngine 的 CTA 引擎同步所有策略的 trades
        参考 vnpy CtaStrategy 绩效面板的采集逻辑
        """
        if self._engine is None:
            return

        try:
            for label in ["US", "HK"]:
                cta = self._engine._get_cta_engine(label)
                if cta is None:
                    continue

                for name, strategy_obj in cta.strategies.items():
                    if name not in self._trackers:
                        self.register_strategy(name)

                    tracker = self._trackers[name]

                    # 从 vnpy 策略对象同步 trades
                    trades = getattr(strategy_obj, 'trades', {}) or {}
                    if trades:
                        tracker.add_trades_batch(trades)

                    # 从 pos 和 last_price 估算权益
                    pos = getattr(strategy_obj, 'pos', 0) or 0
                    last_price = getattr(strategy_obj, 'last_price', 0) or 0
                    avg_price = getattr(strategy_obj, 'avg_price', 0) or 0

                    if pos != 0 and last_price > 0:
                        # 浮动盈亏
                        if avg_price > 0:
                            floating_pnl = (last_price - avg_price) * pos
                            current_eq = tracker._start_equity + floating_pnl
                            tracker.update_equity(current_eq)
        except Exception as e:
            logger.debug(f"[Perf] sync_from_engine 异常: {e}")

    def get_summary_for(self, strategy_name: str) -> Optional[dict]:
        """获取某策略的绩效摘要"""
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                return None
            return tracker.get_summary()

    def get_all_summaries(self) -> List[dict]:
        """获取所有策略的绩效摘要"""
        with self._lock:
            return [t.get_summary() for t in self._trackers.values()]

    def get_summary(self) -> dict:
        """兼容旧接口"""
        all_s = self.get_all_summaries()
        if not all_s:
            return {}
        # 汇总
        total_pnl = sum(s["total_pnl"] for s in all_s)
        total_trades = sum(s["total_trades"] for s in all_s)
        return {
            "strategies_count": len(all_s),
            "total_pnl": round(total_pnl, 2),
            "total_trades": total_trades,
            "by_strategy": all_s,
        }

    # ==================== 后台采集 ====================

    def start(self):
        """启动后台采集线程"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True, name="PerfTracker")
        self._thread.start()
        logger.info(f"[Perf] 🔄 绩效采集已启动 (间隔 {self.COLLECT_INTERVAL}s)")

    def stop(self):
        """停止后台采集"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[Perf] 绩效采集已停止")

    def _collect_loop(self):
        """后台采集循环"""
        while not self._stop_event.is_set():
            try:
                self.sync_from_engine()
                self._write_snapshots_to_db()
            except Exception as e:
                logger.warning(f"[Perf] 采集异常: {e}")
            self._stop_event.wait(self.COLLECT_INTERVAL)

    def _write_snapshots_to_db(self):
        """将绩效快照写入数据库"""
        if self._db is None:
            return
        try:
            for tracker in list(self._trackers.values()):
                summary = tracker.get_summary()
                self._db.save_performance_snapshot(
                    strategy_name=tracker.strategy_name,
                    run_id="",  # 由 StrategyEngine 注入
                    perf_data=summary,
                )
        except Exception as e:
            logger.debug(f"[Perf] 写快照失败: {e}")

    # ==================== 报告 ====================

    def print_report(self):
        """打印所有策略的绩效报告"""
        summaries = self.get_all_summaries()
        if not summaries:
            print("\n[Perf] 暂无绩效数据")
            return

        print("\n" + "=" * 72)
        print(f"{'📊 Apollo Trader 绩效报告':^62}")
        print(f"{'生成时间: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^62}")
        print("=" * 72)

        for s in summaries:
            name = s["strategy_name"]
            print(f"\n┌─ {name}")
            print(f"│  💰 总盈亏:    {s['total_pnl']:>12.2f}")
            print(f"│  📈 总交易:    {s['total_trades']:>12d}")
            print(f"│  ✅ 盈利笔数:  {s['winning_trades']:>12d}")
            print(f"│  ❌ 亏损笔数:  {s['losing_trades']:>12d}")
            print(f"│  🎯 胜率:      {s['win_rate']:>11.2f}%")
            print(f"│  📊 平均盈利:  {s['avg_win']:>12.2f}")
            print(f"│  📉 平均亏损:  {s['avg_loss']:>12.2f}")
            print(f"│  🔻 最大回撤:  {s['max_drawdown']:>11.2f}%")
            print(f"│  📐 当前回撤:  {s['current_drawdown']:>11.2f}%")
            print(f"│  📏 Sharpe:    {s['sharpe_ratio']:>12.2f}")
            print(f"│  📐 Sortino:  {s['sortino_ratio']:>12.2f}")
            print(f"│  🏆 Calmar:    {s['calmar_ratio']:>12.2f}")
            print(f"└─ 盈亏比:     {s['profit_factor']:>12.2f}")

        print("\n" + "=" * 72)
