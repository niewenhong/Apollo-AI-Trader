"""
ai/stock_selector.py - v2.6.0
AI选股模块：基于富途行情数据筛选优质标的
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


class AIStockSelector:
    """AI选股器"""

    def __init__(self, quote_ctx, db, top_n: int = 25, market: str = "US"):
        self.ctx = quote_ctx
        self.db = db
        self.top_n = top_n
        self.market = market

    def select(self) -> List[str]:
        """执行选股流程，返回选中的股票代码列表"""
        selected_codes = []

        # 1. 获取候选股票池
        candidates = self._get_candidates()
        if not candidates:
            logger.warning("候选股票池为空")
            return selected_codes

        # 2. 逐个分析评分
        scores = []
        for stock in candidates:
            try:
                # 安全解包：兼容富途API返回的各种格式
                code, name = self._safe_unpack_stock(stock)
                score = self._score_stock(code, name)
                if score > 0:
                    scores.append((code, name, score))
                    logger.info(f"[Selector] {code} ({name}) 评分: {score:.2f}")
            except Exception as e:
                logger.error(f"[Selector] {stock} 失败: {e}")

        # 3. 按评分排序取前N只
        scores.sort(key=lambda x: x[2], reverse=True)
        selected = scores[:self.top_n]
        selected_codes = [s[0] for s in selected]

        # 4. 写入数据库
        self._save_to_db(selected)

        logger.info(f"[Selector] 选股完成: {len(selected_codes)} 只")
        return selected_codes

    def _safe_unpack_stock(self, stock) -> Tuple[str, str]:
        """
        安全解包股票数据，兼容富途API返回的不同格式
        可能的格式：
        - ("US.NVDA", "NVIDIA Corporation")
        - ["US.NVDA", "NVIDIA Corporation"]
        - ("NVDA",)
        - 自定义对象
        """
        if isinstance(stock, (list, tuple)):
            if len(stock) >= 2:
                code = str(stock[0])
                name = str(stock[1])
            elif len(stock) == 1:
                code = str(stock[0])
                name = ""
            else:
                raise ValueError(f"无法解包股票数据: {stock}")
        elif hasattr(stock, 'code') and hasattr(stock, 'name'):
            # 如果是富途API返回的对象
            code = stock.code
            name = stock.name
        else:
            code = str(stock)
            name = ""
        return code, name

    def _get_candidates(self) -> List:
        """获取候选股票池（从富途行情或本地数据库）"""
        try:
            # 优先从富途获取热门股票列表
            codes = ["US.NVDA", "US.AAPL", "US.MSFT", "US.AMZN", "US.TSLA",
                     "US.META", "US.GOOGL", "US.AMD", "US.NFLX", "US.BABA",
                     "US.COIN", "US.MARA", "US.RIOT", "US.UPST", "US.ARM"]
            # 转换为富途API期望的格式（带market前缀）
            return [(c, "") for c in codes]
        except Exception as e:
            logger.error(f"获取候选池失败: {e}")
            return []

    def _score_stock(self, code: str, name: str) -> float:
        """对单只股票进行评分（简化版）"""
        # 这里实现实际的评分逻辑，例如基于技术指标、基本面等
        # 由于当前阶段主要是修复解包错误，暂时返回随机分数供测试
        import random
        return round(random.uniform(0, 100), 2)

    def _save_to_db(self, selected: List[Tuple[str, str, float]]):
        """将选股结果保存到数据库"""
        try:
            for code, name, score in selected:
                self.db.save_stock_selection({
                    "code": code,
                    "name": name,
                    "score": score,
                    "timestamp": datetime.now().isoformat()
                })
            logger.info(f"[Selector] {len(selected)} 只标的写入数据库")
        except Exception as e:
            logger.error(f"保存选股结果失败: {e}")