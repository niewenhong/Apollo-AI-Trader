# -*- coding: utf-8 -*-
"""
core/strategy_lifecycle_manager.py - Apollo Trader v3.8.5
策略生命周期管理器（完整实现版）

v3.8.5 变更：
  - evaluate_all_strategies: 从 strategy_runs + performance_snapshot 读取真实数据
  - evaluate_trial_strategies: 试用期策略评估 + 自动晋升/淘汰
  - detect_strategy_decay: 基于夏普/回撤/胜率/盈亏比的真实衰减检测
  - _get_recent_trades: 从 trade_log 聚合近 N 笔交易绩效
  - promote / demote / kill_strategy: 真正操作 DB + 通知 + 回调
  - 所有方法均依赖 db_manager 的真实表结构（strategy_runs / performance_snapshot /
    strategy_daily_pnl / trade_log / strategy_config / positions）
"""
import logging
from enum import Enum, auto
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any, Tuple

logger = logging.getLogger("LifecycleManager")


class StrategyTier(Enum):
    FREE = auto()
    BASIC = auto()
    PREMIUM = auto()
    ENTERPRISE = auto()
    CUSTOM = auto()


class LifecycleAction(Enum):
    DEPLOY = auto()
    REMOVE = auto()
    PAUSE = auto()
    RESUME = auto()
    PROMOTE = auto()
    DEMOTE = auto()
    KILL = auto()
    ERROR = auto()
    WARNING = auto()
    INFO = auto()


# ============ 可调阈值（后续可移到 config） ============
TRIAL_MIN_TRADES   = 10      # 试用期最少交易笔数才评估
TRIAL_MIN_DAYS     = 5       # 试用期最少运行天数
PROMOTE_WIN_RATE   = 0.40    # 晋升最低胜率
PROMOTE_PF         = 1.10    # 晋升最低盈亏比
PROMOTE_SHARPE     = 0.50    # 晋升最低夏普
DECAY_WIN_RATE     = 0.30    # 衰减判定：胜率过低
DECAY_PF           = 0.80    # 衰减判定：盈亏比过低
DECAY_MAX_DD       = 0.15    # 衰减判定：最大回撤过高（15%）
DECAY_SHARPE       = 0.30    # 衰减判定：夏普过低
DECAY_LOSS_LIMIT   = -500.0  # 衰减判定：累计亏损超 500
GRACE_PERIOD_DAYS  = 7       # 新策略宽限期，宽限期内不判衰减


class StrategyLifecycleManager:
    """策略生命周期管理器 v3.8.5（完整实现）"""

    def __init__(self, db_manager=None, telegram_notifier=None,
                 user_manager=None, risk_manager=None, order_manager=None,
                 account_manager=None, performance_tracker=None):
        self.db = db_manager
        self.telegram = telegram_notifier
        self.user_mgr = user_manager
        self.risk_mgr = risk_manager
        self.order_mgr = order_manager
        self.account_mgr = account_manager
        self.perf_tracker = performance_tracker

        self._callbacks: Dict[str, List[Callable]] = {
            'deploy': [], 'remove': [], 'pause': [], 'resume': [],
            'promote': [], 'demote': [], 'kill': [],
            'error': [], 'warning': [], 'info': [],
        }
        self._strategy_states: Dict[str, dict] = {}
        self.strategy_engine = None

        # 衰减告警冷却（避免每60秒重复告警）
        self._decay_alert_cooldown: Dict[str, datetime] = {}
        self._decay_cooldown_minutes = 30

        logger.info("[LifecycleManager] ✅ 初始化完成 (v3.8.5 完整实现)")

    # ==================== 工具 ====================
    def _get_session(self):
        if self.db is None:
            raise RuntimeError("DBManager 未注入")
        return self.db.Session()

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _in_grace_period(self, deployed_at: Optional[str]) -> bool:
        """新策略宽限期内不做衰减判定"""
        if not deployed_at:
            return True
        try:
            dt = datetime.strptime(deployed_at[:19], "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - dt).days < GRACE_PERIOD_DAYS
        except Exception:
            return False

    def _safe_get(self, d: dict, key: str, default=0.0):
        v = d.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    # ==================== 事件注册 ====================
    def register_callback(self, event_type: str, callback: Callable):
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)

    def unregister_callback(self, event_type: str, callback: Callable):
        if event_type in self._callbacks:
            try:
                self._callbacks[event_type].remove(callback)
            except ValueError:
                pass

    # ==================== 部署/暂停/恢复/移除 ====================
    def on_strategy_deployed(self, strategy_name: str, **kwargs):
        logger.info(f"[Lifecycle] 📦 策略已部署: {strategy_name} | {kwargs}")
        self._strategy_states[strategy_name] = {
            'status': 'RUNNING',
            'deployed_at': self._now(),
            'details': kwargs,
        }
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'RUNNING',
                                             f"部署成功 {kwargs.get('market', '')}")
                # 同时在 strategy_runs 开一条运行记录
                self.db.start_run(
                    strategy_name=kwargs.get('class_name', '') + '_' + strategy_name,
                    class_name=kwargs.get('class_name', ''),
                    vt_symbol=kwargs.get('vt_symbol', ''),
                    market=kwargs.get('market', 'US'),
                    params=kwargs.get('params', {}),
                    user_id=kwargs.get('user_id', 'SYSTEM')
                )
            except Exception as e:
                logger.warning(f"[Lifecycle] DB 状态更新失败: {e}")

        if self.risk_mgr and hasattr(self.risk_mgr, 'on_strategy_deployed'):
            try:
                self.risk_mgr.on_strategy_deployed(strategy_name, **kwargs)
            except Exception as e:
                logger.warning(f"[Lifecycle] RiskMgr 通知失败: {e}")

        if self.telegram:
            try:
                market = kwargs.get('market', '')
                self.telegram.send_message(
                    f"✅ 策略部署成功\n名称: {strategy_name}\n市场: {market}"
                )
            except Exception:
                pass

        for cb in self._callbacks.get('deploy', []):
            try:
                cb(strategy_name, **kwargs)
            except Exception as e:
                logger.error(f"[Lifecycle] 回调异常: {e}")

    def on_strategy_removed(self, strategy_name: str, reason: str = "manual",
                            operator: str = "system"):
        logger.info(f"[Lifecycle] 🗑️ 策略已移除: {strategy_name} (reason={reason})")
        self._strategy_states.pop(strategy_name, None)
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'REMOVED', reason)
                # 结束对应的 strategy_runs
                self._end_active_runs(strategy_name, exit_reason=reason)
            except Exception as e:
                logger.warning(f"[Lifecycle] DB 移除更新失败: {e}")
        for cb in self._callbacks.get('remove', []):
            try:
                cb(strategy_name, reason=reason, operator=operator)
            except Exception:
                pass

    def on_strategy_paused(self, strategy_name: str, reason: str = ""):
        logger.info(f"[Lifecycle] ⏸️ 策略已暂停: {strategy_name} ({reason})")
        state = self._strategy_states.get(strategy_name, {})
        state['status'] = 'PAUSED'
        state['paused_at'] = self._now()
        self._strategy_states[strategy_name] = state
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'PAUSED', reason)
            except Exception:
                pass
        for cb in self._callbacks.get('pause', []):
            try:
                cb(strategy_name, reason=reason)
            except Exception:
                pass

    def on_strategy_resumed(self, strategy_name: str):
        logger.info(f"[Lifecycle] ▶️ 策略已恢复: {strategy_name}")
        state = self._strategy_states.get(strategy_name, {})
        state['status'] = 'RUNNING'
        state['resumed_at'] = self._now()
        self._strategy_states[strategy_name] = state
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'RUNNING', '已恢复')
            except Exception:
                pass
        for cb in self._callbacks.get('resume', []):
            try:
                cb(strategy_name)
            except Exception:
                pass

    def on_strategy_error(self, strategy_name: str, error_msg: str):
        logger.error(f"[Lifecycle] ❌ 策略错误: {strategy_name} -> {error_msg}")
        state = self._strategy_states.get(strategy_name, {})
        state['status'] = 'ERROR'
        state['last_error'] = error_msg
        state['error_at'] = self._now()
        self._strategy_states[strategy_name] = state
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'ERROR', error_msg[:200])
            except Exception:
                pass
        for cb in self._callbacks.get('error', []):
            try:
                cb(strategy_name, error_msg=error_msg)
            except Exception:
                pass

    def on_strategy_warning(self, strategy_name: str, warning_msg: str):
        logger.warning(f"[Lifecycle] ⚠️ 策略警告: {strategy_name} -> {warning_msg}")
        state = self._strategy_states.get(strategy_name, {})
        state['last_warning'] = warning_msg
        state['warning_at'] = self._now()
        self._strategy_states[strategy_name] = state
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, 'WARNING', warning_msg[:200])
            except Exception:
                pass
        for cb in self._callbacks.get('warning', []):
            try:
                cb(strategy_name, warning_msg=warning_msg)
            except Exception:
                pass

    def on_strategy_info(self, strategy_name: str, info_msg: str):
        logger.info(f"[Lifecycle] ℹ️ 策略信息: {strategy_name} -> {info_msg}")
        for cb in self._callbacks.get('info', []):
            try:
                cb(strategy_name, info_msg=info_msg)
            except Exception:
                pass

    # ==================== 状态查询 ====================
    def get_strategy_state(self, strategy_name: str) -> Optional[dict]:
        return self._strategy_states.get(strategy_name)

    def get_all_states(self) -> Dict[str, dict]:
        return dict(self._strategy_states)

    def get_running_count(self) -> int:
        return sum(1 for s in self._strategy_states.values()
                   if s.get('status') == 'RUNNING')

    def get_summary(self) -> dict:
        states = list(self._strategy_states.values())
        return {
            'total': len(states),
            'running': sum(1 for s in states if s.get('status') == 'RUNNING'),
            'paused': sum(1 for s in states if s.get('status') == 'PAUSED'),
            'error': sum(1 for s in states if s.get('status') == 'ERROR'),
            'removed': sum(1 for s in states if s.get('status') == 'REMOVED'),
            'unknown': sum(1 for s in states if s.get('status') not in
                          ('RUNNING', 'PAUSED', 'ERROR', 'REMOVED')),
        }

    def clear_states(self):
        self._strategy_states.clear()
        logger.info("[Lifecycle] 状态缓存已清空")

    def set_strategy_engine(self, strategy_engine):
        self.strategy_engine = strategy_engine
        logger.info("[Lifecycle] ✅ 策略引擎引用已设置")

    # ==================== 核心：评估所有策略 ====================
    def evaluate_all_strategies(self):
        """
        遍历所有 RUNNING 策略，从 strategy_runs / performance_snapshot 读取真实绩效，
        对达标者晋升、对衰减者降级/淘汰。
        """
        logger.info("[Lifecycle] 评估所有策略...")
        if self.db is None:
            logger.warning("[Lifecycle] 无 DB，跳过评估")
            return

        session = self._get_session()
        try:
            rows = session.execute(
                text("SELECT strategy_name, vt_symbol, class_name, market, status, "
                     "updated_at FROM strategy_config WHERE active=1 AND status='RUNNING'")
            ).fetchall()
        except Exception as e:
            logger.error(f"[Lifecycle] 读取策略列表失败: {e}")
            self.db.Session.remove()
            return
        finally:
            self.db.Session.remove()

        if not rows:
            logger.info("[Lifecycle] 无 RUNNING 策略可评估")
            return

        results = {'promoted': [], 'decayed': [], 'killed': [], 'ok': []}
        for (name, vt_symbol, class_name, market, status, updated_at) in rows:
            perf = self._collect_performance(name)
            if perf is None:
                logger.debug(f"[Lifecycle] {name}: 暂无绩效数据，跳过")
                continue

            # 宽限期内不判衰减
            if self._in_grace_period(updated_at):
                logger.debug(f"[Lifecycle] {name}: 宽限期内，跳过衰减检测")
                continue

            # 衰减检测
            reasons = self._check_decay(name, perf)
            if reasons:
                results['decayed'].append((name, reasons))
                self._handle_decayed(name, reasons, perf)
                continue

            # 晋升检测（试用期 → 正式）
            if self._is_trial(name):
                if self._qualifies_promotion(name, perf):
                    self._promote(name, perf)
                    results['promoted'].append(name)
                else:
                    results['ok'].append(name)
            else:
                results['ok'].append(name)

        # 汇总日志
        logger.info(
            f"[Lifecycle] 评估完成: 晋升={len(results['promoted'])} "
            f"衰减={len(results['decayed'])} "
            f"正常={len(results['ok'])}"
        )
        if results['promoted']:
            for n in results['promoted']:
                logger.info(f"[Lifecycle] ⬆️ 晋升: {n}")
        if results['decayed']:
            for n, r in results['decayed']:
                logger.warning(f"[Lifecycle] ⬇️ 衰减: {n} -> {'; '.join(r)}")

        # Telegram 汇总
        if self.telegram and (results['promoted'] or results['decayed']):
            msg = "📊 策略评估汇总\n"
            if results['promoted']:
                msg += "⬆️ 晋升:\n" + "\n".join(f"  • {n}" for n in results['promoted']) + "\n"
            if results['decayed']:
                msg += "⬇️ 衰减:\n" + "\n".join(f"  • {n}" for n, _ in results['decayed'])
            try:
                self.telegram.send_message(msg)
            except Exception:
                pass

    # ==================== 试用期评估 ====================
    def evaluate_trial_strategies(self):
        """
        专门评估 TRIAL 状态的策略。
        达标 → 晋升 RUNNING；不达标且超过宽限期 → 淘汰。
        """
        logger.info("[Lifecycle] 评估试用期策略...")
        if self.db is None:
            return

        session = self._get_session()
        try:
            rows = session.execute(
                text("SELECT strategy_name, vt_symbol, class_name, market, updated_at "
                     "FROM strategy_config WHERE status='TRIAL' AND active=1")
            ).fetchall()
        except Exception as e:
            logger.error(f"[Lifecycle] 读取 TRIAL 策略失败: {e}")
            self.db.Session.remove()
            return
        finally:
            self.db.Session.remove()

        if not rows:
            logger.info("[Lifecycle] 无 TRIAL 策略")
            return

        for (name, vt_symbol, class_name, market, updated_at) in rows:
            perf = self._collect_performance(name)
            if perf is None:
                # 检查是否超过宽限期但仍无数据
                if not self._in_grace_period(updated_at):
                    logger.warning(f"[Lifecycle] {name}: 试用期结束仍无绩效，标记淘汰")
                    self._kill_strategy(name, reason="试用期无交易数据")
                continue

            trades = self._safe_get(perf, 'total_trades', 0)
            days = self._running_days(name)

            if trades < TRIAL_MIN_TRADES or days < TRIAL_MIN_DAYS:
                logger.debug(f"[Lifecycle] {name}: 试用期数据不足 "
                             f"(trades={trades}, days={days})")
                continue

            if self._qualifies_promotion(name, perf):
                self._promote(name, perf)
                logger.info(f"[Lifecycle] ⬆️ 试用期晋升: {name}")
            else:
                # 数据足够但不达标 → 淘汰
                self._kill_strategy(name, reason="试用期绩效不达标")

    # ==================== 衰减检测（主入口）====================
    def detect_strategy_decay(self):
        """
        显式衰减检测入口（供 scheduler_jobs._detect_decay_job 调用）。
        逻辑与 evaluate_all_strategies 中的衰减分支一致，但只做衰减，
        不影响晋升逻辑。
        """
        logger.info("[Lifecycle] 检测策略衰减...")
        if self.db is None:
            logger.warning("[Lifecycle] 无 DB，跳过衰减检测")
            return

        session = self._get_session()
        try:
            rows = session.execute(
                text("SELECT strategy_name, updated_at FROM strategy_config "
                     "WHERE active=1 AND status='RUNNING'")
            ).fetchall()
        except Exception as e:
            logger.error(f"[Lifecycle] 读取策略失败: {e}")
            self.db.Session.remove()
            return
        finally:
            self.db.Session.remove()

        decayed_list = []
        for name, updated_at in rows:
            if self._in_grace_period(updated_at):
                continue
            perf = self._collect_performance(name)
            if perf is None:
                continue
            reasons = self._check_decay(name, perf)
            if reasons:
                decayed_list.append((name, reasons))
                self._handle_decayed(name, reasons, perf)

        if decayed_list:
            logger.warning(f"[Lifecycle] 共检测到 {len(decayed_list)} 个策略衰减")
            if self.telegram:
                msg = "⚠️ 策略衰减告警\n" + "\n".join(
                    f"  • {n}: {'; '.join(r)}" for n, r in decayed_list
                )
                try:
                    self.telegram.send_message(msg)
                except Exception:
                    pass
        else:
            logger.info("[Lifecycle] 未检测到策略衰减")

    # 兼容旧调用名
    def detect_decay_all(self):
        self.detect_strategy_decay()

    # ==================== 晋升检查 ====================
    def promote_check(self):
        """仅做晋升检查（供 scheduler 单独调用）"""
        logger.info("[Lifecycle] 检查策略晋升...")
        if self.db is None:
            return
        session = self._get_session()
        try:
            rows = session.execute(
                text("SELECT strategy_name FROM strategy_config "
                     "WHERE status='TRIAL' AND active=1")
            ).fetchall()
        except Exception:
            self.db.Session.remove()
            return
        finally:
            self.db.Session.remove()

        for (name,) in rows:
            perf = self._collect_performance(name)
            if perf and self._qualifies_promotion(name, perf):
                self._promote(name, perf)

    # ==================== 内部：绩效采集 ====================
    def _collect_performance(self, strategy_name: str) -> Optional[dict]:
        """
        从 performance_snapshot（优先）或 strategy_runs 聚合绩效。
        返回标准化 dict 或 None。
        """
        # 1. 优先 performance_snapshot
        snap = None
        if self.perf_tracker and hasattr(self.perf_tracker, 'get_snapshot'):
            try:
                snap = self.perf_tracker.get_snapshot(strategy_name)
            except Exception:
                snap = None

        if not snap and self.db:
            snap = self.db.get_latest_performance(strategy_name)

        if snap:
            return {
                'total_pnl':        self._safe_get(snap, 'total_pnl'),
                'total_trades':     int(self._safe_get(snap, 'total_trades')),
                'winning_trades':   int(self._safe_get(snap, 'winning_trades')),
                'losing_trades':    int(self._safe_get(snap, 'losing_trades')),
                'win_rate':         self._safe_get(snap, 'win_rate'),
                'avg_win':          self._safe_get(snap, 'avg_win'),
                'avg_loss':         self._safe_get(snap, 'avg_loss'),
                'max_drawdown':     self._safe_get(snap, 'max_drawdown'),
                'sharpe_ratio':     self._safe_get(snap, 'sharpe_ratio'),
                'profit_factor':    self._safe_get(snap, 'profit_factor'),
                'open_positions':   int(self._safe_get(snap, 'open_positions')),
            }

        # 2. 退化方案：从 strategy_runs 最近一条
        if self.db:
            runs = self.db.get_run_history(strategy_name, limit=1)
            if runs:
                r = runs[0]
                return {
                    'total_pnl':     self._safe_get(r, 'total_pnl'),
                    'total_trades':  int(self._safe_get(r, 'total_trades')),
                    'win_rate':      self._safe_get(r, 'win_rate'),
                    'max_drawdown':  self._safe_get(r, 'max_drawdown'),
                    'sharpe_ratio':  self._safe_get(r, 'sharpe_ratio'),
                    'profit_factor': self._safe_get(r, 'profit_factor', 1.0),
                    'avg_win':       self._safe_get(r, 'avg_win'),
                    'avg_loss':      self._safe_get(r, 'avg_loss'),
                    'winning_trades': int(self._safe_get(r, 'winning_trades')),
                    'losing_trades':  int(self._safe_get(r, 'losing_trades')),
                    'open_positions': 0,
                }

        # 3. 最后手段：从 trade_log 实时聚合
        return self._aggregate_from_trade_log(strategy_name)

    def _aggregate_from_trade_log(self, strategy_name: str) -> Optional[dict]:
        """从 trade_log 聚合近 30 笔平仓交易"""
        if self.db is None:
            return None
        session = self._get_session()
        try:
            rows = session.execute(
                text("SELECT pnl FROM trade_log "
                     "WHERE strategy_name=:sn AND status='CLOSED' "
                     "ORDER BY close_time DESC LIMIT 30"),
                {"sn": strategy_name}
            ).fetchall()
        except Exception as e:
            logger.debug(f"[Lifecycle] trade_log 聚合失败 {strategy_name}: {e}")
            self.db.Session.remove()
            return None
        finally:
            self.db.Session.remove()

        if not rows:
            return None

        pnls = [float(r[0]) for r in rows if r[0] is not None]
        if not pnls:
            return None

        wins   = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        total   = sum(pnls)
        wr      = len(wins) / len(pnls)
        avg_w   = sum(wins) / len(wins) if wins else 0.0
        avg_l   = abs(sum(losses) / len(losses)) if losses else 1.0
        pf      = avg_w / avg_l if avg_l > 0 else 0.0

        # 简单回撤
        cum = 0.0; peak = 0.0; mdd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = (peak - cum) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)

        # 近似夏普
        mean = total / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        std = var ** 0.5
        sharpe = (mean / std) * (252 ** 0.5) if std > 0 else 0.0

        return {
            'total_pnl': total, 'total_trades': len(pnls),
            'winning_trades': len(wins), 'losing_trades': len(losses),
            'win_rate': wr, 'avg_win': avg_w, 'avg_loss': avg_l,
            'max_drawdown': mdd, 'sharpe_ratio': sharpe, 'profit_factor': pf,
            'open_positions': 0,
        }

    # ==================== 内部：判定逻辑 ====================
    def _check_decay(self, strategy_name: str, perf: dict) -> List[str]:
        reasons = []
        if perf['win_rate']     < DECAY_WIN_RATE:
            reasons.append(f"胜率过低({perf['win_rate']:.0%}<{DECAY_WIN_RATE:.0%})")
        if perf['profit_factor'] < DECAY_PF:
            reasons.append(f"盈亏比过低({perf['profit_factor']:.2f}<{DECAY_PF:.2f})")
        if perf['max_drawdown'] > DECAY_MAX_DD:
            reasons.append(f"回撤过大({perf['max_drawdown']:.0%}>{DECAY_MAX_DD:.0%})")
        if perf['sharpe_ratio']  < DECAY_SHARPE:
            reasons.append(f"夏普过低({perf['sharpe_ratio']:.2f}<{DECAY_SHARPE:.2f})")
        if perf['total_pnl']    < DECAY_LOSS_LIMIT:
            reasons.append(f"累计亏损({perf['total_pnl']:.0f}<{DECAY_LOSS_LIMIT:.0f})")
        return reasons

    def _is_trial(self, strategy_name: str) -> bool:
        if self.db is None:
            return False
        try:
            st = self.db.get_strategy_status(strategy_name)
            return st == 'TRIAL'
        except Exception:
            return False

    def _running_days(self, strategy_name: str) -> int:
        if self.db is None:
            return 0
        try:
            runs = self.db.get_run_history(strategy_name, limit=1)
            if not runs:
                return 0
            started = runs[0].get('started_at', '')[:10]
            try:
                dt = datetime.strptime(started, "%Y-%m-%d")
                return (datetime.now() - dt).days
            except Exception:
                return 0
        except Exception:
            return 0

    def _qualifies_promotion(self, strategy_name: str, perf: dict) -> bool:
        return (perf['win_rate']     >= PROMOTE_WIN_RATE and
                perf['profit_factor'] >= PROMOTE_PF and
                perf['sharpe_ratio']  >= PROMOTE_SHARPE and
                perf['total_trades']  >= TRIAL_MIN_TRADES)

    # ==================== 内部：动作执行 ====================
    def _handle_decayed(self, name: str, reasons: List[str], perf: dict):
        """根据衰减程度决定降级或淘汰"""
        severe = (perf['max_drawdown'] > 0.25 or
                  perf['total_pnl']    < -1000 or
                  perf['win_rate']     < 0.20)
        if severe:
            self._kill_strategy(name, reason="严重衰减: " + "; ".join(reasons))
        else:
            self._demote(name, reason="; ".join(reasons))

    def _promote(self, name: str, perf: dict):
        logger.info(f"[Lifecycle] ⬆️ 晋升 {name}: wr={perf['win_rate']:.0%} "
                    f"pf={perf['profit_factor']:.2f} sr={perf['sharpe_ratio']:.2f}")
        if self.db:
            try:
                self.db.set_strategy_status(name, 'RUNNING', '试用期晋升')
            except Exception as e:
                logger.warning(f"[Lifecycle] 晋升 DB 更新失败: {e}")
        state = self._strategy_states.get(name, {})
        state['status'] = 'RUNNING'
        state['promoted_at'] = self._now()
        self._strategy_states[name] = state

        if self.telegram:
            try:
                self.telegram.send_message(
                    f"⬆️ 策略晋升\n名称: {name}\n"
                    f"胜率: {perf['win_rate']:.0%}\n"
                    f"盈亏比: {perf['profit_factor']:.2f}\n"
                    f"夏普: {perf['sharpe_ratio']:.2f}"
                )
            except Exception:
                pass
        for cb in self._callbacks.get('promote', []):
            try:
                cb(name, perf=perf)
            except Exception:
                pass

    def _demote(self, name: str, reason: str):
        logger.warning(f"[Lifecycle] ⬇️ 降级 {name}: {reason}")
        if self.db:
            try:
                self.db.set_strategy_status(name, 'WARNING', reason)
            except Exception:
                pass
        for cb in self._callbacks.get('demote', []):
            try:
                cb(name, reason=reason)
            except Exception:
                pass

    def _kill_strategy(self, name: str, reason: str):
        logger.error(f"[Lifecycle] 💀 淘汰 {name}: {reason}")
        if self.db:
            try:
                # 移到 history
                perf = self._collect_performance(name) or {}
                self.db.move_strategy_to_history(
                    name, perf_data=perf, removed_by='lifecycle', reason=reason
                )
            except Exception as e:
                logger.error(f"[Lifecycle] 淘汰 DB 操作失败: {e}")
        self._strategy_states.pop(name, None)

        if self.telegram:
            try:
                self.telegram.send_message(f"💀 策略淘汰\n名称: {name}\n原因: {reason}")
            except Exception:
                pass
        for cb in self._callbacks.get('kill', []):
            try:
                cb(name, reason=reason)
            except Exception:
                pass

    def _end_active_runs(self, strategy_name: str, exit_reason: str = "removed"):
        """结束该策略所有 RUNNING 的 strategy_runs"""
        if self.db is None:
            return
        try:
            runs = self.db.get_run_history(strategy_name, limit=5)
            for r in runs:
                if r.get('status') == 'RUNNING':
                    self.db.end_run(r.get('run_id', ''), exit_reason=exit_reason)
        except Exception as e:
            logger.warning(f"[Lifecycle] 结束 runs 失败: {e}")


# ==================== 工厂函数 ====================
def create_lifecycle_manager(db_manager=None, telegram_notifier=None,
                              user_manager=None, risk_manager=None,
                              order_manager=None, account_manager=None,
                              performance_tracker=None) -> StrategyLifecycleManager:
    return StrategyLifecycleManager(
        db_manager=db_manager, telegram_notifier=telegram_notifier,
        user_manager=user_manager, risk_manager=risk_manager,
        order_manager=order_manager, account_manager=account_manager,
        performance_tracker=performance_tracker,
    )
