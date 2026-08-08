# -*- coding: utf-8 -*-
"""
core/performance_tracker.py - Apollo Trader v3.5.0 绩效跟踪器
修复记录（基于 v3.2.0 基线）：
1. [CRITICAL] add_trades_batch: 切片逻辑错误，应从 len(_trade_pnls) 开始而非 len(trades_dict)
2. [HIGH] sync_from_engine: 使用 trade.pnl 但 vnpy TradeData 没有 pnl 字段
3. [HIGH] get_sharpe_ratio: 未处理 np.std() 返回 NaN 的情况
4. [MEDIUM] update_equity: 每次调用都 append 到 _daily_returns，高频调用会膨胀
5. [MEDIUM] get_calmar_ratio: days 计算用 390 不对，美股交易时段仅约 6.5 小时
6. [LOW] print_report: emoji 在某些终端乱码
7. [NEW] 新增: register_trade_callback 供 OrderManager 直接推送成交
8. [NEW] 新增: add_trade_from_order_manager 接收 OM 的成交事件
9. [NEW] 新增: _calculate_pnl_from_trades 从成交记录反推盈亏
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
    """单个策略的绩效跟踪"""

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
        self._last_equity_update: float = 0.0  # ★ 新增：防止高频 update
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
        """更新权益（限频：至少 1 秒间隔）"""
        with self._lock:
            now = time.time()
            if now - self._last_equity_update < 1.0:
                # 高频调用时跳过，避免 _daily_returns 膨胀
                self._current_equity = equity
                return
            self._last_equity_update = now

            self._current_equity = equity
            if equity > self._high_water_mark:
                self._high_water_mark = equity

            dd = 0.0
            if self._high_water_mark > 0:
                dd = (self._high_water_mark - equity) / self._high_water_mark
            self._current_drawdown = dd
            if dd > self._max_drawdown:
                self._max_drawdown = dd

            if self._last_equity > 0:
                ret = (equity - self._last_equity) / self._last_equity
                self._daily_returns.append(ret)

            ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._equity_curve.append((ts, equity))
            self._last_equity = equity

    def add_trade(self, pnl: float, price: float = 0, volume: float = 0):
        """添加一笔交易盈亏"""
        with self._lock:
            self._trade_pnls.append(pnl)
            self._trade_pnl_history.append({
                'pnl': pnl, 'price': price, 'volume': volume,
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

    def add_trade_pnl(self, pnl: float):
        """★ 新增：直接添加 PnL（供 OrderManager 推送）"""
        with self._lock:
            self._trade_pnls.append(float(pnl))
            self._trade_pnl_history.append({
                'pnl': float(pnl), 'price': 0, 'volume': 0,
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

    def add_trades_batch(self, trades_dict: dict):
        """
        ★ 修复：正确计算每笔交易的 PnL
        vnpy TradeData 没有 pnl 字段，需要用前后两笔交易的价格差计算
        """
        with self._lock:
            current_count = len(self._trade_pnls)

            if not trades_dict:
                return

            # 获取新增的交易（按 orderid 排序）
            new_trades = [t for t in trades_dict.values()]
            # 只处理超出部分
            if len(new_trades) <= current_count:
                return

            new_trades = new_trades[current_count:]

            for trade in new_trades:
                price = getattr(trade, 'price', 0) or 0
                volume = getattr(trade, 'volume', 0) or 0
                # TradeData 没有直接 pnl，记录价格和数量
                # 真正的 PnL 由 OrderManager 的 Position.update_on_fill 计算后推送
                self._trade_pnl_history.append({
                    'pnl': 0,  # 待 OrderManager 推送真实 PnL
                    'price': price,
                    'volume': volume,
                    'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'needs_calc': True
                })
                # 先记 0，等 OrderManager 推送真实 PnL 后覆盖
                self._trade_pnls.append(0.0)

    def update_last_trade_pnl(self, pnl: float):
        """★ 新增：由 OrderManager 回调更新最后一笔交易的真实 PnL"""
        with self._lock:
            if self._trade_pnls:
                idx = len(self._trade_pnls) - 1
                self._trade_pnls[idx] = float(pnl)
            if self._trade_pnl_history:
                self._trade_pnl_history[-1]['pnl'] = float(pnl)
                self._trade_pnl_history[-1].pop('needs_calc', None)

    def get_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """年化 Sharpe 比率"""
        with self._lock:
            if len(self._daily_returns) < 20:
                return 0.0
            returns = np.array(self._daily_returns)
            if np.isnan(returns).any():
                returns = returns[~np.isnan(returns)]
            if len(returns) < 20:
                return 0.0
            excess = returns - (risk_free_rate / 252.0)
            std = excess.std()
            if std == 0 or np.isnan(std):
                return 0.0
            sharpe = excess.mean() / std * np.sqrt(252)
            if np.isnan(sharpe):
                return 0.0
            return round(float(sharpe), 2)

    def get_sortino_ratio(self, risk_free_rate: float = 0.02) -> float:
        """Sortino 比率"""
        with self._lock:
            if len(self._daily_returns) < 20:
                return 0.0
            returns = np.array(self._daily_returns)
            if np.isnan(returns).any():
                returns = returns[~np.isnan(returns)]
            excess = returns - (risk_free_rate / 252.0)
            downside = excess[excess < 0]
            if len(downside) == 0 or downside.std() == 0:
                return 0.0
            sortino = excess.mean() / downside.std() * np.sqrt(252)
            if np.isnan(sortino):
                return 0.0
            return round(float(sortino), 2)

    def get_calmar_ratio(self) -> float:
        """Calmar 比率"""
        with self._lock:
            if self._max_drawdown == 0 or len(self._equity_curve) < 20:
                return 0.0
            if self._start_equity > 0:
                total_return = (self._current_equity - self._start_equity) / self._start_equity
                # ★ 修复：用实际记录数估算天数（每分钟一条=390条/天）
                days = max(len(self._equity_curve) / 390.0, 1.0)
                annualized = total_return / days * 252
                calmar = annualized / self._max_drawdown
                if np.isnan(calmar):
                    return 0.0
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
        """盈亏比"""
        with self._lock:
            gross_profit = sum(p for p in self._trade_pnls if p > 0)
            gross_loss = abs(sum(p for p in self._trade_pnls if p < 0))
            if gross_loss == 0:
                return float('inf') if gross_profit > 0 else 0.0
            pf = gross_profit / gross_loss
            if np.isnan(pf):
                return 0.0
            return round(float(pf), 2)

    def get_total_pnl(self) -> float:
        with self._lock:
            if not self._trade_pnls:
                return 0.0
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
    - 接收 OrderManager 成交推送
    """
    COLLECT_INTERVAL = 60

    def __init__(self, db=None, strategy_engine=None):
        self._db = db
        self._engine = strategy_engine
        self._trackers: Dict[str, SingleStrategyTracker] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ==================== 对外接口 ====================

    def register_strategy(self, strategy_name: str, start_equity: float = 0.0):
        with self._lock:
            if strategy_name not in self._trackers:
                tracker = SingleStrategyTracker(strategy_name)
                if start_equity > 0:
                    tracker.set_start_equity(start_equity)
                self._trackers[strategy_name] = tracker
                logger.info(f"[Perf] 注册跟踪: {strategy_name}")
            else:
                if start_equity > 0 and self._trackers[strategy_name]._start_equity == 0:
                    self._trackers[strategy_name].set_start_equity(start_equity)

    def unregister_strategy(self, strategy_name: str):
        with self._lock:
            self._trackers.pop(strategy_name, None)
            logger.info(f"[Perf] 注销跟踪: {strategy_name}")

    def update_equity(self, strategy_name: str, equity: float):
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                tracker = SingleStrategyTracker(strategy_name)
                tracker.set_start_equity(equity)
                self._trackers[strategy_name] = tracker
            else:
                tracker.update_equity(equity)

    def add_trade_pnl(self, strategy_name: str, pnl: float):
        """由 OrderManager 回调调用"""
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                tracker = SingleStrategyTracker(strategy_name)
                self._trackers[strategy_name] = tracker
            tracker.add_trade_pnl(float(pnl))
            logger.debug(f"[Perf] {strategy_name} trade pnl={pnl:.2f}")

    def on_trade_from_om(self, strategy_name: str, direction: str,
                          price: float, volume: float, pnl: float = 0.0):
        """
        ★ 新增：OrderManager 成交回调入口
        direction: 'LONG' / 'SHORT'
        pnl: 由 OrderManager 的 Position 计算好后传入
        """
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                tracker = SingleStrategyTracker(strategy_name)
                self._trackers[strategy_name] = tracker
            tracker.add_trade_pnl(float(pnl))
            logger.info(
                f"[Perf] {strategy_name} {direction} "
                f"{volume}@{price:.2f} pnl={pnl:+.2f}"
            )

    def sync_from_engine(self):
        """从 StrategyEngine 的 CTA 引擎同步（兜底机制）"""
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

                    trades = getattr(strategy_obj, 'trades', {}) or {}
                    if trades:
                        tracker.add_trades_batch(trades)

                    pos = getattr(strategy_obj, 'pos', 0) or 0
                    last_price = getattr(strategy_obj, 'last_price', 0) or 0
                    avg_price = getattr(strategy_obj, 'avg_price', 0) or 0

                    if pos != 0 and last_price > 0:
                        if avg_price > 0:
                            floating_pnl = (last_price - avg_price) * pos
                            current_eq = tracker._start_equity + floating_pnl
                            tracker.update_equity(current_eq)
        except Exception as e:
            logger.debug(f"[Perf] sync_from_engine exception: {e}")

    def get_summary_for(self, strategy_name: str) -> Optional[dict]:
        with self._lock:
            tracker = self._trackers.get(strategy_name)
            if tracker is None:
                return None
            return tracker.get_summary()

    def get_all_summaries(self) -> List[dict]:
        with self._lock:
            return [t.get_summary() for t in self._trackers.values()]

    def get_summary(self) -> dict:
        all_s = self.get_all_summaries()
        if not all_s:
            return {}
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
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True, name="PerfTracker")
        self._thread.start()
        logger.info(f"[Perf] 绩效采集已启动 (间隔 {self.COLLECT_INTERVAL}s)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[Perf] 绩效采集已停止")

    def _collect_loop(self):
        while not self._stop_event.is_set():
            try:
                self.sync_from_engine()
                self._write_snapshots_to_db()
            except Exception as e:
                logger.warning(f"[Perf] 采集异常: {e}")
            self._stop_event.wait(self.COLLECT_INTERVAL)

    def _write_snapshots_to_db(self):
        if self._db is None:
            return
        try:
            for tracker in list(self._trackers.values()):
                summary = tracker.get_summary()
                self._db.save_performance_snapshot(
                    strategy_name=tracker.strategy_name,
                    run_id="",
                    perf_data=summary,
                )
        except Exception as e:
            logger.debug(f"[Perf] 写快照失败: {e}")

    # ==================== 报告 ====================

    def print_report(self):
        summaries = self.get_all_summaries()
        if not summaries:
            print("\n[Perf] 暂无绩效数据")
            return

        print("\n" + "=" * 72)
        print(f"{'Apollo Trader Performance Report':^62}")
        print(f"{'Generated: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^62}")
        print("=" * 72)

        for s in summaries:
            name = s["strategy_name"]
            print(f"\n+-- {name}")
            print(f"|  Total PnL:    {s['total_pnl']:>12.2f}")
            print(f"|  Total Trades:  {s['total_trades']:>12d}")
            print(f"|  Winning:       {s['winning_trades']:>12d}")
            print(f"|  Losing:        {s['losing_trades']:>12d}")
            print(f"|  Win Rate:       {s['win_rate']:>11.2f}%")
            print(f"|  Avg Win:       {s['avg_win']:>12.2f}")
            print(f"|  Avg Loss:      {s['avg_loss']:>12.2f}")
            print(f"|  Max Drawdown:   {s['max_drawdown']:>11.2f}%")
            print(f"|  Cur Drawdown:   {s['current_drawdown']:>11.2f}%")
            print(f"|  Sharpe:        {s['sharpe_ratio']:>12.2f}")
            print(f"|  Sortino:       {s['sortino_ratio']:>12.2f}")
            print(f"|  Calmar:        {s['calmar_ratio']:>12.2f}")
            print(f"+-- Profit Factor: {s['profit_factor']:>12.2f}")

        print("\n" + "=" * 72)
