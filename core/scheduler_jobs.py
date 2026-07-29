"""
core/scheduler_jobs.py - v3.0.2
定时调度任务：regime更新 / 策略优化 / 数据检查 / 心跳
修复 v3.0.1：
  1. 顶部导入 typing.Optional（不再 NameError）
  2. regime_job 从 DB 读取 symbol 列表
  3. optimization_job 调用 StrategyGenerator.regenerate_all
  4. 所有任务异常隔离，不阻塞 scheduler
"""
import time
import logging
from typing import Optional, Dict, Any

log = logging.getLogger("SchedulerJobs")


class SchedulerJobs:
    """定时任务集合"""

    def __init__(self, config: Optional[dict] = None,
                 db=None, quote_ctx_us=None, quote_ctx_hk=None,
                 strategy_engine=None, notifier=None):
        self.config = config or {}
        self.db = db
        self.quote_us = quote_ctx_us
        self.quote_hk = quote_ctx_hk
        self.engine = strategy_engine
        self.notifier = notifier

        # 间隔配置（秒）
        self.regime_interval = self.config.get("regime_interval", 300)
        self.opt_interval = self.config.get("opt_interval", 1800)
        self.data_interval = self.config.get("data_interval", 600)
        self.heartbeat_interval = self.config.get("heartbeat_interval", 120)

    # ==================== 任务 1：Regime 更新 ====================

    def regime_job(self):
        """定期重新计算所有持仓股的 regime"""
        log.info("[Scheduler] 🔄 regime_job 开始")
        try:
            from core.regime_trainer import RegimeTrainer
            rt = RegimeTrainer(config=self.config.get("regime", {}), db=self.db)

            symbols = self._get_tracked_symbols()
            if not symbols:
                log.info("[Scheduler] regime_job: 无跟踪标的")
                return

            for sym in symbols:
                try:
                    regime, conf = rt.predict(sym)
                    log.info(f"[Scheduler] {sym} → {regime} (conf={conf:.2f})")
                except Exception as e:
                    log.warning(f"[Scheduler] {sym} regime 失败: {e}")
                time.sleep(0.5)

            log.info(f"[Scheduler] ✅ regime_job 完成 ({len(symbols)} 只)")
        except Exception as e:
            log.error(f"[Scheduler] regime_job 异常: {e}")

    # ==================== 任务 2：策略优化 ====================

    def optimization_job(self):
        """定期重新生成策略（诊股 + regime 可能已变化）"""
        log.info("[Scheduler] 🔧 optimization_job 开始")
        try:
            if not self.engine:
                log.warning("[Scheduler] strategy_engine 未初始化，跳过")
                return

            from core.strategy_generator import StrategyGenerator
            gen = StrategyGenerator(db=self.db, config=self.config)

            # 获取当前选股池
            symbols = self._get_pool_symbols()
            if symbols:
                count = gen.generate_from_selection(symbols)
                log.info(f"[Scheduler] ✅ optimization_job 生成 {count} 个策略")

                # 热加载会检测到变化并自动部署
            else:
                log.info("[Scheduler] optimization_job: 选股池为空")
        except Exception as e:
            log.error(f"[Scheduler] optimization_job 异常: {e}")

    # ==================== 任务 3：数据检查 ====================

    def data_job(self):
        """检查数据完整性，必要时补充历史数据"""
        log.info("[Scheduler] 📊 data_job 开始")
        try:
            if self.db:
                # 检查 dbbardata 最近更新时间
                conn = self.db.conn if hasattr(self.db, 'conn') else self.db
                cur = conn.execute(
                    "SELECT COUNT(*) FROM dbbardata WHERE datetime > "
                    "datetime('now', '-1 hour')"
                )
                count = cur.fetchone()[0]
                log.info(f"[Scheduler] 最近1小时 K线数据: {count} 条")

                if count == 0:
                    log.warning("[Scheduler] ⚠️ 近1小时无新K线数据")
                    if self.notifier:
                        self.notifier.send("⚠️ 数据流可能中断，请检查 Futu OpenD")
            else:
                log.warning("[Scheduler] db 未初始化")
        except Exception as e:
            log.error(f"[Scheduler] data_job 异常: {e}")

    # ==================== 任务 4：心跳 ====================

    def heartbeat_job(self):
        """定期心跳：报告系统状态"""
        try:
            status = {"timestamp": time.time()}
            if self.engine:
                status["strategies"] = self.engine.get_status()

            conn = self.db.conn if hasattr(self.db, 'conn') else None
            if conn:
                cur = conn.execute("SELECT COUNT(*) FROM strategy_config WHERE active=1")
                status["active_strategies"] = cur.fetchone()[0]

                cur = conn.execute("SELECT COUNT(*) FROM ai_stock_pool")
                status["stock_pool_size"] = cur.fetchone()[0]

            log.info(f"[Scheduler] 💓 心跳: {status}")

            if self.notifier:
                summary = (
                    f"💓 Apollo 运行中\n"
                    f"  • 活跃策略: {status.get('active_strategies', '?')}\n"
                    f"  • 选股池: {status.get('stock_pool_size', '?')}\n"
                    f"  • 已部署: {status.get('strategies', {}).get('count', '?')}"
                )
                self.notifier.send(summary)
        except Exception as e:
            log.error(f"[Scheduler] heartbeat_job 异常: {e}")

    # ==================== 工具方法 ====================

    def _get_tracked_symbols(self) -> list:
        """获取所有需要跟踪的 symbol（从 strategy_config + ai_stock_pool）"""
        symbols = []
        try:
            conn = self.db.conn if hasattr(self.db, 'conn') else self.db
            cur = conn.execute(
                "SELECT DISTINCT vt_symbol FROM strategy_config WHERE active=1"
            )
            symbols = [r[0] for r in cur.fetchall() if r[0]]
            if not symbols:
                cur = conn.execute("SELECT DISTINCT symbol FROM ai_stock_pool ORDER BY score DESC LIMIT 20")
                symbols = [r[0] for r in cur.fetchall() if r[0]]
        except Exception as e:
            log.warning(f"[Scheduler] 获取跟踪标的失败: {e}")
        return symbols

    def _get_pool_symbols(self) -> list:
        """从选股池构建 generate_from_selection 需要的格式"""
        items = []
        try:
            conn = self.db.conn if hasattr(self.db, 'conn') else self.db
            cur = conn.execute(
                "SELECT symbol FROM ai_stock_pool ORDER BY score DESC LIMIT 30"
            )
            for r in cur.fetchall():
                if r[0]:
                    items.append({"vt_symbol": r[0]})
        except Exception:
            pass
        return items
