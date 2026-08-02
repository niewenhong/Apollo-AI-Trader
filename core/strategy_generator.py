"""
core/strategy_generator.py - Apollo Trader v3.1.6 策略生成器
"""
import json
import logging
import traceback
from datetime import datetime
from typing import List, Dict, Optional, Any

from core.db_manager import DBManager

logger = logging.getLogger("StrategyGenerator")


class StrategyGenerator:
    """策略生成器 - 从选股结果生成策略配置并写入 strategy_config 表"""

    def __init__(self, quote_ctx=None, matcher=None, param_advisor=None, db=None, db_path=""):
        self.quote_ctx = quote_ctx
        self.matcher = matcher
        self.param_advisor = param_advisor
        self.db = db or DBManager(db_path or "data/history.db")

    def generate_from_selector(self, selected: List[Dict]) -> int:
        """从选股结果生成策略并写入数据库，返回写入数量"""
        count = 0
        for item in selected:
            # 支持多种字段名
            raw_symbol = item.get("symbol", "") or item.get("vt_symbol", "") or item.get("stock_code", "")
            score = item.get("score", 0.0)
            raw_diagnosis = item.get("diagnosis", item.get("reason", ""))
            market = item.get("market", "US")

            if not raw_symbol:
                continue

            # ★ 提取纯股票代码（去除交易所后缀，如 .SMART）
            symbol = raw_symbol.split(".")[0] if "." in raw_symbol else raw_symbol

            # ★ 构造正确的 vt_symbol（只含一个点）
            if market == "US":
                vt_symbol = f"{symbol}.SMART"
            else:
                vt_symbol = f"{symbol}.SEHK"

            # ★ 确保 diagnosis 是字符串
            diagnosis_str = str(raw_diagnosis) if not isinstance(raw_diagnosis, str) else raw_diagnosis

            # 1. 保存诊断
            try:
                self.db.save_diagnosis(
                    symbol=symbol,
                    diagnosis=diagnosis_str,
                    market=market,
                    score=float(score) if score else 0.0
                )
            except Exception as e:
                logger.warning(f"[Gen] save_diagnosis 失败 {symbol}: {e}")

            # 2. 选择策略类型
            class_name = "TrendStrategy"
            if self.matcher:
                try:
                    match_result = self.matcher.match(symbol, market)
                    class_name = match_result.get("class_name", "TrendStrategy")
                except Exception:
                    pass

            # 3. 策略名称（使用纯 symbol）
            strategy_name = f"{class_name}_{symbol}"

            # 4. 生成参数
            params = self._generate_params(symbol, class_name, market)

            # 5. 写入 strategy_config
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor = self.db.conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO strategy_config
                    (strategy_name, class_name, vt_symbol, market, params,
                     enabled, active, version, current_version, status,
                     source, modifier, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?,
                            1, 1, 1, 1, 'PENDING',
                            'pipeline', 'system:pipeline', ?, ?)
                """, (
                    strategy_name,
                    class_name,
                    vt_symbol,
                    market,
                    json.dumps(params, ensure_ascii=False),
                    now,
                    now
                ))
                self.db.conn.commit()
                count += 1
                logger.info(f"[Gen] ✅ {strategy_name} → {class_name}({vt_symbol}) regime=range")
            except Exception as e:
                logger.error(f"[Gen] 写库失败 {strategy_name}: {e}")

        logger.info(f"[Gen] 🎉 共写入 {count} 个策略到 strategy_config 表")
        return count

    def _generate_params(self, symbol: str, class_name: str, market: str) -> dict:
        params = {
            "fast_window": 10,
            "slow_window": 30,
            "adx_period": 20,
        }
        if self.param_advisor:
            try:
                suggested = self.param_advisor.suggest(symbol, class_name, params)
                if suggested:
                    params.update(suggested)
            except Exception:
                pass
        return params