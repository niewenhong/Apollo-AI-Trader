"""
core/scheduler_jobs.py — 定时任务调度器
"""
import logging
from datetime import datetime

log = logging.getLogger("Scheduler")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False


def create_scheduler(config: dict, symbols: list, db_path: str = "trading.db"):
    """创建并配置调度器"""
    if not APSCHEDULER_AVAILABLE:
        log.warning("APScheduler未安装")
        return None

    scheduler = BackgroundScheduler()

    def regime_job():
        log.info("⏰ Regime更新")
        from core.regime_trainer import detect_and_store
        market = config.get("market", "US")
        for sym in symbols:
            proba = detect_and_store(db_path, sym, market)
            log.info(f"  {sym}: {proba}")

    def optimization_job():
        log.info("⏰ 参数优化")
        from core.param_optimizer import ParamOptimizer
        opt = ParamOptimizer(config)
        strategies = config.get("optimization", {}).get(
            "strategies", ["TrendStrategy", "GridStrategy"])
        for sym in symbols:
            for strat in strategies:
                opt.optimize(sym, strat, n_trials=20, db_path=db_path)

    def data_job():
        log.info("⏰ 数据采集（由 main.py 中的 FutuGateway 处理）")

    def heartbeat_job():
        log.info(f"💓 {datetime.now().strftime('%H:%M:%S')}")

    scheduler.add_job(regime_job, 'interval', minutes=30, id='regime_update')
    scheduler.add_job(optimization_job, 'cron', hour=22, minute=0, id='nightly_opt')
    scheduler.add_job(data_job, 'interval', minutes=30, id='data_fetch')
    scheduler.add_job(heartbeat_job, 'interval', minutes=5, id='heartbeat')

    return scheduler


class SchedulerJobs:
    """兼容旧接口"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.symbols = self.config.get("symbols", [])
        self.db_path = self.config.get("database", {}).get("path", "trading.db")
        self.scheduler = create_scheduler(self.config, self.symbols, self.db_path)

    def start(self):
        if self.scheduler:
            self.scheduler.start()
            log.info("调度器已启动")

    def stop(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            log.info("调度器已停止")