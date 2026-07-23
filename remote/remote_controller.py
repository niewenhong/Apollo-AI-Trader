"""
remote/remote_controller.py — Apollo-AI-Trader v2.5.0-FINAL
远程控制器（兼容基线 main.py 的 RemoteController(notifier, config) 调用）
含 cancel 三阶段兜底 + debug_buy / debug_sell 命令
"""
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("RemoteController")


class RemoteController:
    def __init__(self, notifier, config: dict):
        self.notifier = notifier
        self.config = config
        self.admin_id = config.get("telegram_chat_id", 0)
        self.cta_engine = None
        self.cta_engines = {}
        self.strategy_config = None
        self.strategy_class = None
        self._shutdown_callback = None
        self.enabled = bool(self.admin_id)
        logger.info(f"[RemoteController] 已初始化 | admin={self.admin_id}")

    def set_shutdown_callback(self, callback: Callable):
        self._shutdown_callback = callback

    # ── 命令处理 ──
    def handle_command(self, command: str, args: str = "") -> str:
        cmd = command.strip().lower()
        if cmd == "status":
            return self._get_status()
        elif cmd == "shutdown":
            if self._shutdown_callback:
                self._shutdown_callback()
            return "系统关闭中..."
        elif cmd == "debug_buy":
            return self._debug_buy(args)
        elif cmd == "debug_sell":
            return self._debug_sell(args)
        elif cmd == "cancel":
            return self._cancel_order(args)
        elif cmd == "help":
            return "命令: status / shutdown / debug_buy <name> / debug_sell <name> / cancel <name> / help"
        else:
            return f"未知命令: {command}"

    def _get_status(self) -> str:
        lines = ["📊 系统状态"]
        for market, engine in self.cta_engines.items():
            strategies = getattr(engine, 'strategies', {})
            active = sum(1 for s in strategies.values() if getattr(s, 'active', False))
            lines.append(f"{market}: {active}/{len(strategies)} 活跃")
        return "\n".join(lines)

    def _debug_buy(self, name: str) -> str:
        """手动触发指定策略买入（调试用）"""
        strat = self._find_strategy(name)
        if not strat:
            return f"策略未找到: {name}"
        try:
            price = strat.tick.last_price * 1.01 if hasattr(strat, 'tick') else 100.0
            strat.buy(price, strat.fixed_size)
            return f"✅ {name} 买入委托 price={price:.2f} vol={strat.fixed_size}"
        except Exception as e:
            return f"❌ {name} 买入失败: {e}"

    def _debug_sell(self, name: str) -> str:
        """手动触发指定策略卖出（调试用）"""
        strat = self._find_strategy(name)
        if not strat:
            return f"策略未找到: {name}"
        try:
            price = strat.tick.last_price * 0.99 if hasattr(strat, 'tick') else 100.0
            vol = abs(strat.pos) if strat.pos != 0 else strat.fixed_size
            strat.sell(price, vol)
            return f"✅ {name} 卖出委托 price={price:.2f} vol={vol}"
        except Exception as e:
            return f"❌ {name} 卖出失败: {e}"

    def _cancel_order(self, name: str) -> str:
        """
        三阶段撤单兜底：
        1) 从策略本地缓存找 vt_orderid
        2) 通过 main_engine 撤单
        3) 直接调富途 trade_ctx modify_order
        """
        strat = self._find_strategy(name)
        if not strat:
            return f"策略未找到: {name}"

        # 1) 本地缓存
        orders = getattr(strat, 'orders', {})
        if not orders:
            return f"⚠️ {name} 无挂单缓存"

        results = []
        for oid, order in list(orders.items()):
            vt_oid = getattr(order, 'vt_orderid', oid)
            # 2) 通过 main_engine
            try:
                if self.cta_engine:
                    self.cta_engine.cancel_order(vt_oid)
                    results.append(f"  ✅ cancel via engine: {vt_oid}")
                    continue
            except Exception as e:
                results.append(f"  ⚠️ engine cancel failed: {e}")

            # 3) 直接调富途 trade_ctx
            try:
                gw = getattr(strat, 'main_engine', None)
                if gw:
                    trade_ctx = getattr(gw, 'trade_ctx', None)
                    if trade_ctx:
                        raw_id = int(vt_oid.split(".")[-1]) if "." in str(vt_oid) else int(vt_oid)
                        code, data = trade_ctx.modify_order(2, raw_id, 0, 0)  # 2=CANCEL
                        if code == 0:
                            results.append(f"  ✅ cancel via futu API: {raw_id}")
                        else:
                            results.append(f"  ❌ futu cancel failed: {data}")
            except Exception as e:
                results.append(f"  ❌ futu cancel error: {e}")

        return f"📤 {name} 撤单结果:\n" + "\n".join(results)

    def _find_strategy(self, name: str):
        """跨 US/HK 引擎查找策略"""
        for engine in self.cta_engines.values():
            if name in engine.strategies:
                return engine.strategies[name]
        # 模糊匹配
        for engine in self.cta_engines.values():
            for n, s in engine.strategies.items():
                if name.lower() in n.lower():
                    return s
        return None

    def start(self):
        logger.info("[RemoteController] 已启动")

    def stop(self):
        logger.info("[RemoteController] 已停止")
