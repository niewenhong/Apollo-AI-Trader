# -*- coding: utf-8 -*-
"""
历史数据加载器
支持 CSV / SQLite / PostgreSQL 数据源
"""
import csv
import os
import logging
from datetime import datetime
from typing import List

from backtest.engine import Bar

logger = logging.getLogger("backtest.data_loader")


def load_csv(filepath: str, datetime_format: str = "%Y-%m-%d %H:%M:%S") -> List[Bar]:
    """
    从 CSV 加载 K 线数据
    要求列: datetime,open,high,low,close,volume
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"数据文件不存在: {filepath}")

    bars = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.strptime(row["datetime"], datetime_format)
            bar = Bar(
                dt=dt,
                o=float(row["open"]),
                h=float(row["high"]),
                l=float(row["low"]),
                c=float(row["close"]),
                v=float(row.get("volume", 0))
            )
            bars.append(bar)

    logger.info(f"[DataLoader] 加载 {len(bars)} 根 K 线: {filepath}")
    return bars


def load_database(db_path: str, symbol: str,
                 start_date: str = "", end_date: str = "") -> List[Bar]:
    """
    从 SQLite 数据库加载 K 线数据
    表结构: market_data(symbol, datetime, open, high, low, close, volume)
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    query = "SELECT datetime, open, high, low, close, volume FROM market_data WHERE symbol = ?"
    params = [symbol]
    if start_date:
        query += " AND datetime >= ?"
        params.append(start_date)
    if end_date:
        query += " AND datetime <= ?"
        params.append(end_date)
    query += " ORDER BY datetime ASC"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    bars = []
    for dt_str, o, h, l, c, v in rows:
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
        bars.append(Bar(dt=dt, o=o, h=h, l=l, c=c, v=v))

    logger.info(f"[DataLoader] 从数据库加载 {len(bars)} 根 K 线: {symbol}")
    return bars


def load_from_vnpy(vt_symbol: str, start: datetime, end: datetime) -> List[Bar]:
    """
    从 vnpy 数据库加载历史数据
    """
    try:
        from vnpy.trader.database import get_database
        db = get_database()
        bars = db.load_bar_data(
            symbol=vt_symbol.split(".")[0],
            exchange=vt_symbol.split(".")[1] if "." in vt_symbol else "",
            interval="1m",
            start=start,
            end=end
        )
        result = []
        for b in bars:
            result.append(Bar(
                dt=b.datetime, o=b.open_price, h=b.high_price,
                l=b.low_price, c=b.close_price, v=b.volume
            ))
        logger.info(f"[DataLoader] 从 vnpy 加载 {len(result)} 根 K 线: {vt_symbol}")
        return result
    except Exception as e:
        logger.error(f"[DataLoader] vnpy 加载失败: {e}")
        return []


def save_bars_to_csv(bars: List[Bar], filepath: str):
    """保存 Bars 到 CSV"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([
                b.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                b.open, b.high, b.low, b.close, b.volume
            ])
    logger.info(f"[DataLoader] 保存 {len(bars)} 根 K 线: {filepath}")
