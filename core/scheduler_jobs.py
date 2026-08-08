# -*- coding: utf-8 -*-
"""
core/scheduler_jobs.py - 调度任务 v3.8.5
适配 PipelineRunner v3.8.4 + LifecycleManager v3.8.5

v3.8.5 变更：
  - 新增 _is_trading_hours() / _is_safe_window() 交易时段感知
  - pipeline_job / optimization_job 在盘中自动跳过
  - data_job 改为采集真实K线（通过 kline_provider）
  - health_check_job 增加不下单排查探针（trading 开关 / 行情订阅 / 信号产出）
  - _evaluate_trial_job 确认调用 evaluate_trial_strategies
  - _detect_decay_job 确认调用 detect_strategy_decay
  - 不下单诊断完整保留：trading开关 / 风控放行 / 行情tick / 近30分钟订单数
  - 周末重型任务编排（参数优化 / 模型重训练 / 数据补全 / DB 维护）
"""
import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from core.kline_provider import KlineProvider

log = logging.getLogger("SchedulerJobs")


# ==================== 交易时段工具 ====================
def _is_dst(now: datetime) -> bool:
    """美股夏令时：3月第二个周日 02:00 → 11月第一个周日 02:00"""
    year = now.year
    d = datetime(year, 3, 1)
    dst_start = d + timedelta(days=(6 - d.weekday() + 7) % 7 + 7)
    d = datetime(year, 11, 1)
    dst_end = d + timedelta(days=(6 - d.weekday()) % 7)
    return dst_start.date() <= now.date() < dst_end.date()


def is_trading_hours() -> tuple:
    """
    返回 (is_hk_trading, is_us_trading)
    港股：周一至周五 09:30-16:00 北京时间（无夏令时）
    美股：周一至周五 夏令时 21:30-次日04:00 / 冬令时 22:30-次日05:00
    """
    now = datetime.now()
    weekday = now.weekday()
    is_hk = False
    is_us = False

    if weekday < 5:
        # 港股
        hk_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        hk_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        is_hk = hk_open <= now <= hk_close

        # 美股（跨午夜）
        dst = _is_dst(now)
        open_h  = 21 if dst else 22
        close_h = 4  if dst else 5
        us_open = now.replace(hour=open_h, minute=30, second=0, microsecond=0)
        us_close = (now + timedelta(days=1)).replace(
            hour=close_h, minute=0, second=0, microsecond=0
        )
        if now >= us_open and now < us_close:
            is_us = True

    return is_hk, is_us


def is_safe_heavy_window() -> bool:
    """两个市场都收盘后的安全窗口（北京时间）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return True  # 周末全天
    dst = _is_dst(now)
    hk_close_h = 16
    us_open_h  = 21 if dst else 22
    if hk_close_h <= now.hour < us_open_h:
        return True
    us_close_h = 4 if dst else 5
    if (dst and us_close_h <= now.hour < 9) or \
       (not dst and us_close_h <= now.hour < 9):
        return True
    return False


def is_weekend() -> bool:
    return datetime.now().weekday() >= 5


# ==================== 主类 ====================
class SchedulerJobs:
    """调度任务管理器 v3.8.5"""

    def __init__(self, main_engine=None, db=None, config=None,
                 quote_ctx_us=None, quote_ctx_hk=None,
                 kline_provider: KlineProvider = None,
                 notifier=None, order_manager=None, position_manager=None,
                 account_manager=None, risk_manager=None, lifecycle=None,
                 strategy_engine=None, pipeline_runner=None, dual_link=None,
                 regime_predictor=None, sub_manager=None,
                 lifecycle_manager=None, user_manager=None,
                 performance_tracker=None):
        self.main_engine = main_engine
        self.db = db
        self.config = config or {}
        self.quote_us = quote_ctx_us
        self.quote_hk = quote_ctx_hk
        self.kp = kline_provider
        self.notifier = notifier
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.account_manager = account_manager
        self.risk_manager = risk_manager
        self.lifecycle = lifecycle or lifecycle_manager
        self.strategy_engine = strategy_engine
        self.pipeline_runner = pipeline_runner
        self.dual_link = dual_link
        self.regime_predictor = regime_predictor
        self.sub_manager = sub_manager
        self.user_manager = user_manager
        self.perf_tracker = performance_tracker

        self._running = False
        self._thread = None
        self._takeover_done_today = False
        self._weekend_jobs_done: dict = {}
        self._weekend_start: Optional[str] = None

        # 间隔配置（秒）
        self.heartbeat_interval      = 60
        self.health_check_trading   = 120   # 盘中每2分钟
        self.health_check_offhours  = 300   # 盘后每5分钟
        self.pipeline_offhours      = 14400  # 盘后每4小时
        self.data_job_offhours      = 1800   # 盘后每30分钟
        self.optimization_daily     = 86400  # 每天一次
        self._last_pipeline_run     = 0
        self._last_optimization_run = 0
        self._last_data_run         = 0

    # ==================== DB 会话 ====================
    def _get_db_session(self):
        if self.db is None:
            return None
        s = getattr(self.db, 'session', None)
        if s is not None and hasattr(s, 'execute'):
            return s
        eng = getattr(self.db, 'engine', None)
        if eng is not None:
            from sqlalchemy.orm import Session
            return Session(eng)
        conn = getattr(self.db, 'conn', None)
        if conn is not None and hasattr(conn, 'execute'):
            return conn
        raise RuntimeError("无法获取数据库 session")

    # ==================== 对外接口 ====================
    def start_scheduler(self): self.start()
    def stop_scheduler(self):  self.stop()

    def start(self):
        if self._running:
            log.warning("[Scheduler] 已在运行")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="SchedulerLoop")
        self._thread.start()
        log.info("[Scheduler] 调度循环启动 (v3.8.5)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            log.info("[Scheduler] 调度循环停止")

    # ==================== 主循环 ====================
    def _loop(self):
        while self._running:
            try:
                self._run_all_jobs()
            except Exception as e:
                log.error(f"[Scheduler] 循环异常: {e}", exc_info=True)
            time.sleep(self._compute_sleep())

    def _compute_sleep(self) -> int:
        is_hk, is_us = is_trading_hours()
        if is_hk or is_us:
            return self.heartbeat_interval  # 盘中 60 秒
        if is_weekend():
            return 600  # 周末 10 分钟
        return 300  # 盘后 5 分钟

    # ==================== 任务编排 ====================
    def _run_all_jobs(self):
        is_hk, is_us = is_trading_hours()
        in_trading = is_hk or is_us

        # 心跳始终运行
        self._heartbeat_job()

        if is_weekend():
            self._run_weekend_heavy_jobs()
            return

        if in_trading:
            # 盘中：专注监控 + 不下单诊断
            self._health_check_job_trading()
            self._evaluate_trial_job()
            self._detect_decay_job()
            self._sync_account_job()
            self._diagnose_no_orders()  # ★ 不下单诊断（盘中高频）
        else:
            # 盘后安全窗口
            self._health_check_job_offhours()
            self._evaluate_trial_job()
            self._detect_decay_job()
            self._maybe_run_pipeline()
            self._maybe_run_optimization()
            self._maybe_run_data_job()
            self._takeover_manual_positions_job()
            self._diagnose_no_orders()  # ★ 不下单诊断（盘后也跑）

    # ==================== 具体任务 ====================
    def _regime_job(self):
        log.info("[Scheduler] 🔄 regime_job 开始")
        try:
            session = self._get_db_session()
            if session is None:
                return
            result = session.execute(
                text("SELECT DISTINCT vt_symbol FROM strategy_config WHERE active=1")
            )
            symbols = [r[0] for r in result.fetchall()]
            log.info(f"[Scheduler] regime_job: 跟踪 {len(symbols)} 个标的")
        except Exception as e:
            log.error(f"[Scheduler] regime_job 异常: {e}")

    def _optimization_job(self):
        """通过 pipeline_runner 双市场并行优化"""
        if not self.pipeline_runner:
            log.warning("[Scheduler] pipeline_runner 未注入，跳过 optimization_job")
            return
        log.info("[Scheduler] 🔧 optimization_job 开始（双市场并行）")
        us = threading.Thread(target=self.pipeline_runner.run, args=("US",), daemon=True)
        hk = threading.Thread(target=self.pipeline_runner.run, args=("HK",), daemon=True)
        us.start(); hk.start()
        us.join();  hk.join()
        self._last_optimization_run = time.time()
        log.info("[Scheduler] optimization_job 完成")

    def _maybe_run_optimization(self):
        if time.time() - self._last_optimization_run > self.optimization_daily:
            self._optimization_job()

    def _data_job(self):
        """采集历史K线（盘后执行，避免消耗盘中额度）"""
        log.info("[Scheduler] 📊 data_job 开始")
        try:
            if self.kp and hasattr(self.kp, 'fetch_and_store_daily'):
                count = self.kp.fetch_and_store_daily()
                log.info(f"[Scheduler] data_job: 更新K线 {count} 条")
            else:
                # 退化：仅统计 dbbardata
                session = self._get_db_session()
                if session is not None:
                    row = session.execute(
                        text("SELECT COUNT(*) FROM dbbardata "
                             "WHERE datetime > datetime('now','-1 hour')")
                    ).fetchone()
                    log.info(f"[Scheduler] data_job: 过去1小时K线条数={row[0] if row else 0}")
        except Exception as e:
            log.error(f"[Scheduler] data_job 异常: {e}")

    def _maybe_run_data_job(self):
        if time.time() - self._last_data_run > self.data_job_offhours:
            self._data_job()
            self._last_data_run = time.time()

    def _heartbeat_job(self):
        """心跳：始终执行，包含不下单诊断的关键输出"""
        try:
            session = self._get_db_session()
            active = 0
            if session is not None:
                row = session.execute(
                    text("SELECT COUNT(*) FROM strategy_config WHERE active=1")
                ).fetchone()
                active = row[0] if row else 0
            summary = f"心跳: 活跃策略={active} 时间={datetime.now().isoformat()}"
            if self.notifier:
                try:
                    self.notifier.send_message(summary)
                except Exception:
                    pass
            log.info(f"[Scheduler] ❤️ {summary}")
        except Exception as e:
            log.error(f"[Scheduler] heartbeat_job 异常: {e}")

    # ---- 盘中健康检查的"不下单排查探针" ----
    def _health_check_job_trading(self):
        """盘中高频健康检查 + 不下单根因探针"""
        log.info("[Scheduler] 🩺 health_check_job (盘中)")
        # 检查策略引擎中实际运行状态
        self._check_running_strategies()
        # 不下单诊断
        self._diagnose_no_orders()

    def _health_check_job_offhours(self):
        log.info("[Scheduler] 🩺 health_check_job (盘后)")
        self._check_running_strategies()
        self._diagnose_no_orders()

    def _check_running_strategies(self):
        """检查策略引擎中实际在运行的策略数量"""
        if not self.strategy_engine:
            log.warning("[HealthCheck] strategy_engine 未注入")
            return

        try:
            engines = []
            if hasattr(self.strategy_engine, 'get_all_engines'):
                engines = self.strategy_engine.get_all_engines()
            elif isinstance(self.strategy_engine, dict):
                engines = list(self.strategy_engine.values())

            total = 0
            inited = 0
            trading_on = 0
            for eng in engines:
                if not hasattr(eng, 'strategies'):
                    continue
                for name, strat in eng.strategies.items():
                    total += 1
                    if getattr(strat, 'inited', False):
                        inited += 1
                    if getattr(strat, 'trading', False):
                        trading_on += 1

            log.info(f"[HealthCheck] 策略总数={total} inited={inited} trading_on={trading_on}")

            if total > 0 and trading_on == 0:
                log.error("🚨 [HealthCheck] 所有策略 trading=False！"
                          "请在 StrategyEngine.boot() 中调用 strategy.set_trading(True)")

            return {
                'total': total,
                'inited': inited,
                'trading_on': trading_on
            }
        except Exception as e:
            log.error(f"[HealthCheck] 异常: {e}")
            return None

    def _diagnose_no_orders(self):
        """
        ★ 不下单排查探针（完整保留，日志输出全部在）：
        1. 策略 trading 开关是否打开
        2. 是否收到行情 tick
        3. 风控是否放行
        4. 最近是否有信号/订单
        """
        log.info("[Scheduler] 🚨 不下单诊断开始")

        if not self.strategy_engine:
            log.warning("[Diagnose] strategy_engine 未注入")
            return

        try:
            # 1. trading 开关检查
            engines = []
            if hasattr(self.strategy_engine, 'get_all_engines'):
                engines = self.strategy_engine.get_all_engines()
            elif isinstance(self.strategy_engine, dict):
                engines = list(self.strategy_engine.values())

            total = 0
            trading_on = 0
            inited = 0
            for eng in engines:
                if not hasattr(eng, 'strategies'):
                    continue
                for name, strat in eng.strategies.items():
                    total += 1
                    if getattr(strat, 'inited', False):
                        inited += 1
                    if getattr(strat, 'trading', False):
                        trading_on += 1

            log.info(f"[Diagnose] 策略总数={total} inited={inited} trading_on={trading_on}")

            if total > 0 and trading_on == 0:
                log.error("🚨 [Diagnose] 所有策略 trading=False！"
                          "请在 StrategyEngine.boot() 中调用 strategy.set_trading(True)")
            elif trading_on > 0:
                log.info(f"✅ [Diagnose] {trading_on}/{total} 策略 trading=True")

            # 2. 风控开关检查
            if self.risk_manager:
                allow = getattr(self.risk_manager, 'allow_trading', None)
                if callable(allow):
                    try:
                        if allow():
                            log.info("✅ [Diagnose] 风控允许交易 (allow_trading=True)")
                        else:
                            log.error("🚨 [Diagnose] 风控禁止交易 (allow_trading=False)")
                    except Exception:
                        log.warning("[Diagnose] 风控 allow_trading() 调用异常")
                elif allow is not None:
                    if allow:
                        log.info("✅ [Diagnose] 风控 allow_trading=True")
                    else:
                        log.error("🚨 [Diagnose] 风控 allow_trading=False")
                else:
                    # 尝试其他常见属性名
                    enabled = getattr(self.risk_manager, 'enabled', None)
                    if enabled:
                        log.info("✅ [Diagnose] 风控 enabled=True")
                    else:
                        log.warning("[Diagnose] 风控无 allow_trading 属性，请检查 risk_manager.py")
            else:
                log.warning("[Diagnose] risk_manager 未注入")

            # 3. 行情订阅检查（通过 regime_predictor 或 quote_ctx）
            market_data_ok = False
            if self.quote_us or self.quote_hk:
                market_data_ok = True
                log.info("✅ [Diagnose] 行情网关已连接 (US/HK)")
            elif self.regime_predictor:
                log.info("✅ [Diagnose] regime_predictor 已注入")
                market_data_ok = True
            else:
                log.warning("⚠️ [Diagnose] 无行情网关连接")

            # 4. 最近订单检查（通过 db_manager.get_order_signals_recent）
            if self.db:
                try:
                    recent_orders = self.db.get_order_signals_recent(minutes=30)
                    if recent_orders:
                        log.info(f"✅ [Diagnose] 近30分钟有 {len(recent_orders)} 个订单信号")
                    else:
                        log.warning("🚨 [Diagnose] 近30分钟无订单信号！")
                except Exception as e:
                    log.warning(f"[Diagnose] 查询订单失败: {e}")
            else:
                log.warning("[Diagnose] db 未注入，无法查询订单")

            # 5. 综合结论
            issues = []
            if total > 0 and trading_on == 0:
                issues.append("trading开关未开")
            if self.risk_manager:
                allow_val = getattr(self.risk_manager, 'allow_trading', True)
                if callable(allow_val):
                    try:
                        allow_val = allow_val()
                    except:
                        allow_val = True
                if not allow_val:
                    issues.append("风控禁止交易")
            if not market_data_ok:
                issues.append("行情网关未连接")
            if self.db:
                try:
                    if not self.db.get_order_signals_recent(minutes=30):
                        issues.append("近30分钟无订单")
                except:
                    pass

            if issues:
                log.error(f"🚨 [Diagnose] 不下单根因: {'; '.join(issues)}")
            else:
                log.info("✅ [Diagnose] 所有检查通过，系统正常")

        except Exception as e:
            log.error(f"[Diagnose] 异常: {e}", exc_info=True)

    # ---- Pipeline ----
    def _pipeline_job(self):
        if not self.pipeline_runner:
            log.warning("[Scheduler] pipeline_runner 未注入，跳过 pipeline_job")
            return
        log.info("[Scheduler] 🏗️ pipeline_job 开始（双市场并行）")
        us = threading.Thread(target=self.pipeline_runner.run, args=("US",), daemon=True)
        hk = threading.Thread(target=self.pipeline_runner.run, args=("HK",), daemon=True)
        us.start(); hk.start()
        us.join();  hk.join()
        self._last_pipeline_run = time.time()
        log.info("[Scheduler] pipeline_job 完成")

    def _maybe_run_pipeline(self):
        if time.time() - self._last_pipeline_run > self.pipeline_offhours:
            self._pipeline_job()

    # ---- Lifecycle 调用（直接调用，不 getattr 绕路）----
    def _evaluate_strategies_job(self):
        if self.lifecycle:
            self.lifecycle.evaluate_all_strategies()

    def _evaluate_trial_job(self):
        if self.lifecycle:
            self.lifecycle.evaluate_trial_strategies()

    def _detect_decay_job(self):
        if self.lifecycle:
            self.lifecycle.detect_strategy_decay()

    # ---- 持仓接管 ----
    def _detect_unmanaged_positions_job(self):
        if not self.pipeline_runner:
            return
        self.pipeline_runner.takeover_manual_positions()

    def _takeover_manual_positions_job(self):
        now = datetime.now()
        if now.hour != 16 or now.minute < 30 or now.minute > 35:
            return
        if self._takeover_done_today:
            return
        if not self.pipeline_runner:
            return
        log.info("[Scheduler] 🔍 盘后接管手动持仓开始...")
        self.pipeline_runner.takeover_manual_positions()
        self._takeover_done_today = True
        log.info("[Scheduler] ✅ 盘后接管手动持仓完成")

    def _sync_account_job(self):
        log.info("[Scheduler] 💰 sync_account_job 开始")
        if self.account_manager:
            try:
                self.account_manager.sync_all_positions(user_id="SYSTEM")
            except Exception as e:
                log.error(f"[Scheduler] sync_account_job 异常: {e}")

    def _status_report_job(self):
        log.info("[Scheduler] 📋 status_report_job 完成")

    # ==================== 周末重型任务 ====================
    def _run_weekend_heavy_jobs(self):
        self._reset_weekend_flags_if_new()
        job_schedule = [
            ("data_backfill",     self._weekend_data_backfill,    "30min"),
            ("db_maintenance",    self._weekend_db_maintenance,   "10min"),
            ("param_optimization", self._weekend_param_optimization, "2-3h"),
        ]
        for name, func, est in job_schedule:
            if name in self._weekend_jobs_done:
                continue
            if not self._in_safe_day_window():
                log.info(f"[Scheduler] {name} 延后（不在 08:00-20:00 窗口）")
                break
            log.info(f"[Scheduler] 🚀 周末任务: {name} (预计{est})")
            try:
                func()
                self._weekend_jobs_done[name] = datetime.now()
                log.info(f"[Scheduler] ✅ 周末任务完成: {name}")
            except Exception as e:
                log.error(f"[Scheduler] ❌ 周末任务失败 {name}: {e}")
            time.sleep(600)  # 任务间休息10分钟

        # 盘后接管在周末也执行一次
        self._takeover_manual_positions_job()

    def _reset_weekend_flags_if_new(self):
        now = datetime.now()
        if now.weekday() == 5:
            key = now.strftime("%Y-%m-%d")
            if self._weekend_start != key:
                self._weekend_start = key
                self._weekend_jobs_done.clear()
                log.info("[Scheduler] 🏖️ 进入周末模式，重置重任务标志")

    def _in_safe_day_window(self) -> bool:
        return 8 <= datetime.now().hour < 20

    def _weekend_param_optimization(self):
        if not self.pipeline_runner:
            log.warning("[Weekend] pipeline_runner 未注入")
            return
        log.info("[Weekend] 开始全量参数优化")
        batch = 20
        symbols = self._get_all_active_symbols()
        for i in range(0, len(symbols), batch):
            chunk = symbols[i:i+batch]
            for sym in chunk:
                try:
                    if hasattr(self.pipeline_runner, 'optimize_symbol'):
                        self.pipeline_runner.optimize_symbol(sym)
                except Exception as e:
                    log.warning(f"[Weekend] 优化失败 {sym}: {e}")
            log.info(f"[Weekend] 优化进度 {min(i+batch, len(symbols))}/{len(symbols)}")
            time.sleep(300)

    def _weekend_data_backfill(self):
        log.info("[Weekend] 数据补全开始")
        if not self.kp or not hasattr(self.kp, 'backfill_all'):
            log.info("[Weekend] kline_provider 无 backfill_all，跳过")
            return
        try:
            self.kp.backfill_all()
            log.info("[Weekend] 数据补全完成")
        except Exception as e:
            log.error(f"[Weekend] 数据补全失败: {e}")

    def _weekend_db_maintenance(self):
        log.info("[Weekend] 数据库维护开始")
        try:
            session = self._get_db_session()
            if session is None:
                return
            # 清理30天前日志
            session.execute(
                text("DELETE FROM events WHERE timestamp < datetime('now','-30 days')")
            )
            # 归档90天前已平仓交易
            try:
                session.execute(
                    text("CREATE TABLE IF NOT EXISTS trade_archive AS "
                         "SELECT * FROM trade_log WHERE 1=0")
                )
                session.execute(
                    text("INSERT INTO trade_archive SELECT * FROM trade_log "
                         "WHERE status='CLOSED' AND close_time < datetime('now','-90 days')")
                )
                session.execute(
                    text("DELETE FROM trade_log WHERE status='CLOSED' "
                         "AND close_time < datetime('now','-90 days')")
                )
            except Exception:
                pass
            session.execute(text("VACUUM"))
            session.commit()
            log.info("[Weekend] 数据库维护完成")
        except Exception as e:
            log.error(f"[Weekend] DB维护失败: {e}")

    def _get_all_active_symbols(self) -> list:
        try:
            session = self._get_db_session()
            if session is None:
                return []
            rows = session.execute(
                text("SELECT DISTINCT vt_symbol FROM strategy_config WHERE active=1")
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []
