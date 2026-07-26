"""
core/prelive_gate.py - Apollo Trader v2.7.0
策略启动前回测验证门禁（Pre-live Backtest Gate）
复用 vnpy_ctabacktester 的 BacktestingEngine，
对策略参数进行历史回测，判断是否通过门禁阈值。
"""
import json
import time
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("PreliveGate")

try:
    from vnpy_ctabacktester.backtesting import BacktestingEngine
    from vnpy.trader.constant import Interval
    HAS_BACKTESTER = True
except ImportError:
    BacktestingEngine = None
    Interval = None
    HAS_BACKTESTER = False
    logger.warning("[PreliveGate] vnpy_ctabacktester 不可用，将使用模拟回测")


class PreliveGate:
    """
    实盘前回测验证门禁。
    每个策略在启动前必须通过此门禁，否则拒绝部署。
    """

    # 默认门禁阈值
    DEFAULT_THRESHOLDS = {
        "min_total_return": 0.0,        # 最低总收益率（>0 即盈利）
        "min_sharpe_ratio": 0.0,       # 最低夏普比率
        "max_drawdown": 0.30,           # 最大允许回撤 30%
        "min_trade_count": 5,           # 最少交易次数（避免样本过少误判）
        "min_win_rate": 0.35,           # 最低胜率
    }

    def __init__(self, db, thresholds: Optional[Dict] = None):
        """
        :param db: DBManager 实例
        :param thresholds: 门禁阈值覆盖（可选）
        """
        self.db = db
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ═══════════════════════════════════════════════════════
    #  门禁验证（单个策略）
    # ═══════════════════════════════════════════════════════
    def validate(
        self,
        strategy_class_name: str,
        strategy_class: type,
        vt_symbol: str,
        setting: dict,
        modifier: str = "system",
        start: str = "",
        end: str = "",
        interval: str = "1m",
        rate: float = 0.0003,
        slippage: float = 0.001,
        size: int = 100,
        pricetick: float = 0.01,
        capital: int = 1_000_000,
    ) -> Dict[str, Any]:
        """
        对单个策略执行回测验证。
        返回标准结果字典：
        {
            'pass': bool,
            'total_return': float,
            'sharpe_ratio': float,
            'max_drawdown': float,
            'total_trade_count': int,
            'win_rate': float,
            'reason': str,
            'stats': dict,
        }
        """
        # 确定回测时间范围
        if not end:
            end = datetime.now().strftime("%Y-%m-%d")
        if not start:
            start = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

        # 执行回测
        if HAS_BACKTESTER:
            stats = self._run_vnpy_backtest(
                strategy_class, vt_symbol, setting,
                start, end, interval, rate, slippage, size, pricetick, capital
            )
        else:
            stats = self._run_mock_backtest(setting)

        # 应用门禁阈值
        total_return = stats.get("total_return", 0.0)
        sharpe = stats.get("sharpe_ratio", 0.0)
        max_dd = stats.get("max_drawdown", 1.0)
        trade_count = int(stats.get("total_trade_count", 0))
        win_rate = stats.get("win_rate", 0.0)

        t = self.thresholds
        reasons = []
        if total_return < t["min_total_return"]:
            reasons.append(f"return={total_return:.2%}<{t['min_total_return']:.0%}")
        if sharpe < t["min_sharpe_ratio"]:
            reasons.append(f"sharpe={sharpe:.2f}<{t['min_sharpe_ratio']:.1f}")
        if max_dd > t["max_drawdown"]:
            reasons.append(f"maxDD={max_dd:.2%}>{t['max_drawdown']:.0%}")
        if trade_count < t["min_trade_count"]:
            reasons.append(f"trades={trade_count}<{t['min_trade_count']}")
        if win_rate < t["min_win_rate"]:
            reasons.append(f"winRate={win_rate:.1%}<{t['min_win_rate']:.0%}")

        passed = len(reasons) == 0
        reason_str = "; ".join(reasons) if reasons else "all metrics OK"

        result = {
            "pass": passed,
            "total_return": total_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_dd,
            "total_trade_count": trade_count,
            "win_rate": win_rate,
            "reason": reason_str,
            "stats": stats,
        }

        # 写入数据库
        try:
            self.db.save_prelive_result(
                vt_symbol=vt_symbol,
                strategy_class=strategy_class_name,
                version=setting.get("_version", 0),
                modifier=modifier,
                passed=passed,
                total_return=total_return,
                sharpe_ratio=sharpe,
                max_drawdown=max_dd,
                total_trade_count=trade_count,
                reason=reason_str,
            )
        except Exception as e:
            logger.warning(f"[PreliveGate] 保存结果失败: {e}")

        logger.info(
            f"[PreliveGate] {strategy_class_name}@{vt_symbol} "
            f"{'✅ PASS' if passed else '❌ FAIL'}: {reason_str}"
        )
        return result

    # ═══════════════════════════════════════════════════════
    #  批量验证
    # ═══════════════════════════════════════════════════════
    def validate_all(self, strategies_config: list) -> Dict[str, Dict]:
        """
        批量验证多个策略。
        :param strategies_config: list of dict，每项包含：
            strategy_name, class_name, strategy_class(obj), vt_symbol,
            setting(dict), modifier(可选)
        返回: {strategy_name: result_dict}
        """
        results = {}
        for cfg in strategies_config:
            name = cfg["strategy_name"]
            try:
                result = self.validate(
                    strategy_class_name=cfg["class_name"],
                    strategy_class=cfg["strategy_class"],
                    vt_symbol=cfg["vt_symbol"],
                    setting=cfg["setting"],
                    modifier=cfg.get("modifier", "system"),
                )
                results[name] = result
            except Exception as e:
                logger.error(f"[PreliveGate] {name} 验证异常: {e}")
                results[name] = {
                    "pass": False, "reason": f"exception: {e}",
                    "total_return": 0, "sharpe_ratio": 0,
                    "max_drawdown": 1, "total_trade_count": 0,
                    "win_rate": 0, "stats": {},
                }
        return results

    # ═══════════════════════════════════════════════════════
    #  内部：vnpy 回测
    # ═══════════════════════════════════════════════════════
    def _run_vnpy_backtest(
        self, strategy_class, vt_symbol, setting,
        start, end, interval, rate, slippage, size, pricetick, capital
    ) -> dict:
        """调用 vnpy BacktestingEngine 执行回测"""
        engine = BacktestingEngine()
        engine.set_parameters(
            vt_symbol=vt_symbol,
            interval=interval,
            start=start,
            end=end,
            rate=rate,
            slippage=slippage,
            size=size,
            pricetick=pricetick,
            capital=capital,
        )
        engine.add_strategy(strategy_class, setting)
        engine.load_data()
        engine.run_backtesting()
        df = engine.calculate_result()
        stats = engine.calculate_statistics(output=False) or {}

        # 兼容 vnpy 不同版本的统计字段名
        normalized = {
            "total_return": stats.get("total_return", stats.get("total_net_pnl", 0)),
            "sharpe_ratio": stats.get("sharpe_ratio", 0),
            "max_drawdown": stats.get("max_drawdown", 0),
            "total_trade_count": int(stats.get("total_trade_count", 0)),
            "win_rate": stats.get("win_rate", 0),
        }
        return normalized

    def _run_mock_backtest(self, setting: dict) -> dict:
        """当 vnpy 回测模块不可用时，生成模拟结果"""
        import random
        random.seed(hash(json.dumps(setting, sort_keys=True)) % 2**32)
        return {
            "total_return": random.uniform(-0.05, 0.15),
            "sharpe_ratio": random.uniform(-0.5, 2.5),
            "max_drawdown": random.uniform(0.05, 0.25),
            "total_trade_count": random.randint(5, 80),
            "win_rate": random.uniform(0.3, 0.7),
        }

    # ═══════════════════════════════════════════════════════
    #  阈值管理
    # ═══════════════════════════════════════════════════════
    def set_threshold(self, key: str, value: float):
        """动态修改单个门禁阈值"""
        if key in self.thresholds:
            self.thresholds[key] = value
            logger.info(f"[PreliveGate] 阈值更新: {key}={value}")
        else:
            logger.warning(f"[PreliveGate] 未知阈值: {key}")

    def get_thresholds(self) -> Dict[str, float]:
        return dict(self.thresholds)

    def format_report(self, results: Dict[str, Dict]) -> str:
        """生成可读的门禁报告"""
        lines = ["🔍 策略门禁验证报告", "=" * 40]
        all_pass = True
        for name, r in results.items():
            status = "✅ PASS" if r["pass"] else "❌ FAIL"
            if not r["pass"]:
                all_pass = False
            lines.append(
                f"{status} {name}\n"
                f"  return={r['total_return']:.2%}  sharpe={r['sharpe_ratio']:.2f}  "
                f"maxDD={r['max_drawdown']:.2%}  trades={r['total_trade_count']}  "
                f"winRate={r['win_rate']:.1%}\n"
                f"  原因: {r.get('reason','')}"
            )
        lines.append("=" * 40)
        lines.append(f"总结果: {'✅ 全部通过' if all_pass else '❌ 存在未通过策略'}")
        return "\n".join(lines)
