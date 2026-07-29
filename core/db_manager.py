"""
core/db_manager.py - v2.8.5 修正版
修正点：
  1. save_diagnosis 增加参数自适应：调用方若把 dict 传到 score/verdict 位置，自动纠正
  2. stock_diagnosis 建表增加 details_json 列（兼容旧代码写入）
  3. save_diagnosis 同时写入 details_json（JSON 序列化后的完整 details）
  4. regime_records 建表增加 features_json 列（与 regime_trainer 的 features 键对齐）
  5. save_regime 同时写入 features_json
  6. 导入 json 模块（原文件缺失）
"""
import os
import json
import sqlite3
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

log = logging.getLogger("DBManager")


class DBManager:
    """
    统一数据库访问层
    所有表结构集中管理，便于维护和迁移
    """

    SCHEMA = {
        # ============ K线数据（vnpy 标准表） ============
        "dbbardata": """
            CREATE TABLE IF NOT EXISTS "dbbardata" (
                "id" INTEGER NOT NULL PRIMARY KEY,
                "symbol" VARCHAR(255) NOT NULL,
                "exchange" VARCHAR(255) NOT NULL,
                "datetime" DATETIME NOT NULL,
                "interval" VARCHAR(255) NOT NULL,
                "volume" REAL NOT NULL,
                "turnover" REAL NOT NULL,
                "open_interest" REAL NOT NULL,
                "open_price" REAL NOT NULL,
                "high_price" REAL NOT NULL,
                "low_price" REAL NOT NULL,
                "close_price" REAL NOT NULL
            )""",
        "dbbardata_index": """
            CREATE UNIQUE INDEX IF NOT EXISTS "dbbardata_symbol_exchange_interval_datetime"
            ON "dbbardata" ("symbol", "exchange", "interval", "datetime")
        """,
        "dbbaroverview": """
            CREATE TABLE IF NOT EXISTS "dbbaroverview" (
                "id" INTEGER NOT NULL PRIMARY KEY,
                "symbol" VARCHAR(255) NOT NULL,
                "exchange" VARCHAR(255) NOT NULL,
                "interval" VARCHAR(255) NOT NULL,
                "count" INTEGER NOT NULL,
                "start" DATETIME NOT NULL,
                "end" DATETIME NOT NULL
            )""",

        # ============ Tick 数据 ============
        "dbtickdata": """
            CREATE TABLE IF NOT EXISTS "dbtickdata" (
                "id" INTEGER NOT NULL PRIMARY KEY,
                "symbol" VARCHAR(255) NOT NULL,
                "exchange" VARCHAR(255) NOT NULL,
                "datetime" DATETIME NOT NULL,
                "name" VARCHAR(255) NOT NULL DEFAULT '',
                "volume" REAL NOT NULL DEFAULT 0,
                "turnover" REAL NOT NULL DEFAULT 0,
                "open_interest" REAL NOT NULL DEFAULT 0,
                "last_price" REAL NOT NULL DEFAULT 0,
                "last_volume" REAL NOT NULL DEFAULT 0,
                "limit_up" REAL NOT NULL DEFAULT 0,
                "limit_down" REAL NOT NULL DEFAULT 0,
                "open_price" REAL NOT NULL DEFAULT 0,
                "high_price" REAL NOT NULL DEFAULT 0,
                "low_price" REAL NOT NULL DEFAULT 0,
                "pre_close" REAL NOT NULL DEFAULT 0,
                "bid_price_1" REAL NOT NULL DEFAULT 0,
                "bid_price_2" REAL,
                "bid_price_3" REAL,
                "bid_price_4" REAL,
                "bid_price_5" REAL,
                "ask_price_1" REAL NOT NULL DEFAULT 0,
                "ask_price_2" REAL,
                "ask_price_3" REAL,
                "ask_price_4" REAL,
                "ask_price_5" REAL,
                "bid_volume_1" REAL NOT NULL DEFAULT 0,
                "bid_volume_2" REAL,
                "bid_volume_3" REAL,
                "bid_volume_4" REAL,
                "bid_volume_5" REAL,
                "ask_volume_1" REAL NOT NULL DEFAULT 0,
                "ask_volume_2" REAL,
                "ask_volume_3" REAL,
                "ask_volume_4" REAL,
                "ask_volume_5" REAL,
                "localtime" DATETIME
            )""",
        "dbtickdata_index": """
            CREATE UNIQUE INDEX IF NOT EXISTS "dbtickdata_symbol_exchange_datetime"
            ON "dbtickdata" ("symbol", "exchange", "datetime")
        """,

        # ============ 选股池 ============
        "ai_stock_pool": """
            CREATE TABLE IF NOT EXISTS ai_stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                market TEXT DEFAULT 'US',
                score REAL DEFAULT 50.0,
                reason TEXT DEFAULT '',
                indicators_json TEXT,
                expires_at TEXT,
                status TEXT DEFAULT 'selected',
                source TEXT DEFAULT 'selector',
                selected_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, selected_at)
            )""",

        # ============ 诊股结果（★ 增加 vt_symbol + details_json 列） ============
        "stock_diagnosis": """
            CREATE TABLE IF NOT EXISTS stock_diagnosis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                vt_symbol TEXT DEFAULT '',
                score REAL DEFAULT 50.0,
                trend_score REAL DEFAULT 50.0,
                momentum_score REAL DEFAULT 50.0,
                volume_score REAL DEFAULT 50.0,
                volatility REAL DEFAULT 0.02,
                ma_alignment REAL DEFAULT 50.0,
                rsi REAL DEFAULT 50.0,
                macd_signal REAL DEFAULT 0.0,
                verdict TEXT DEFAULT '未知',
                details_json TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol)
            )""",

        # ============ Regime 状态（★ 增加 features_json 列） ============
        "regime_records": """
            CREATE TABLE IF NOT EXISTS regime_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                exchange TEXT DEFAULT 'US',
                regime TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                prob_trend REAL DEFAULT 0.0,
                prob_range REAL DEFAULT 0.0,
                prob_volatile REAL DEFAULT 0.0,
                features TEXT DEFAULT '{}',
                features_json TEXT DEFAULT '{}',
                timestamp TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, timestamp)
            )""",

        # ============ ★ 策略配置（核心表） ============
        "strategy_config": """
            CREATE TABLE IF NOT EXISTS strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                class_name TEXT NOT NULL,
                vt_symbol TEXT NOT NULL,
                market TEXT DEFAULT 'US',
                params_json TEXT DEFAULT '{}',
                version INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                source TEXT DEFAULT 'pipeline',
                modifier TEXT DEFAULT 'system',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )""",

        # ============ 操作日志 ============
        "strategy_audit_log": """
            CREATE TABLE IF NOT EXISTS strategy_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                strategy_name TEXT,
                detail TEXT,
                operator TEXT DEFAULT 'system',
                timestamp TEXT DEFAULT (datetime('now'))
            )""",

        # ============ 策略回测绩效 ============
        "strategy_performance": """
            CREATE TABLE IF NOT EXISTS strategy_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                vt_symbol TEXT NOT NULL,
                regime TEXT,
                sharpe REAL DEFAULT 0.0,
                total_return REAL DEFAULT 0.0,
                max_drawdown REAL DEFAULT 0.0,
                win_rate REAL DEFAULT 0.0,
                period_days INTEGER DEFAULT 30,
                recorded_at TEXT DEFAULT (datetime('now'))
            )""",

        # ============ Prelive 门禁结果 ============
        "prelive_results": """
            CREATE TABLE IF NOT EXISTS prelive_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                version INTEGER DEFAULT 0,
                modifier TEXT DEFAULT 'system',
                passed INTEGER DEFAULT 0,
                total_return REAL DEFAULT 0.0,
                sharpe_ratio REAL DEFAULT 0.0,
                max_drawdown REAL DEFAULT 0.0,
                total_trade_count INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )""",

        # ============ 参数优化结果 ============
        "param_optimization_results": """
            CREATE TABLE IF NOT EXISTS param_optimization_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                regime TEXT DEFAULT 'all',
                params_json TEXT DEFAULT '{}',
                performance_json TEXT DEFAULT '{}',
                version INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, strategy_name)
            )""",
    }

    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode='WAL'")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_all_tables()
        log.info(f"[DB INIT] 数据库路径: {db_path}")
        self._verify_tick_table()
        self._verify_stock_diagnosis_columns()
        self._verify_regime_columns()
        log.info(f"✅ 数据库表创建/验证完成 ({db_path})")

    def _init_all_tables(self):
        """创建所有表"""
        for name, ddl in self.SCHEMA.items():
            try:
                self.conn.execute(ddl)
            except Exception as e:
                log.warning(f"[DB] 建表 {name} 异常: {e}")
        self.conn.commit()

    def _verify_tick_table(self):
        """验证 tick_data 表结构（兼容旧表）"""
        try:
            cur = self.conn.execute("PRAGMA table_info(tick_data)")
            columns = [r[1] for r in cur.fetchall()]
            log.info(f"[DB INIT] tick_data 列: {columns}")

            required = {
                "name": "TEXT", "open_price": "REAL",
                "high_price": "REAL", "low_price": "REAL",
                "pre_close": "REAL", "source": "TEXT",
                "saved_at_utc": "TEXT",
            }
            for col, col_type in required.items():
                if col not in columns:
                    try:
                        self.conn.execute(f"ALTER TABLE tick_data ADD COLUMN {col} {col_type}")
                        log.info(f"[DB INIT] +列 tick_data.{col}")
                    except Exception:
                        pass
            self.conn.commit()
        except Exception as e:
            log.warning(f"[DB INIT] tick_data 检查失败: {e}")

    def _verify_stock_diagnosis_columns(self):
        """确保 stock_diagnosis 表有 details_json 列"""
        try:
            cur = self.conn.execute("PRAGMA table_info(stock_diagnosis)")
            columns = [r[1] for r in cur.fetchall()]
            if "details_json" not in columns:
                self.conn.execute("ALTER TABLE stock_diagnosis ADD COLUMN details_json TEXT DEFAULT '{}'")
                self.conn.commit()
                log.info("[DB INIT] +列 stock_diagnosis.details_json")
        except Exception as e:
            log.warning(f"[DB INIT] stock_diagnosis 检查失败: {e}")

    def _verify_regime_columns(self):
        """确保 regime_records 表有 features_json 列"""
        try:
            cur = self.conn.execute("PRAGMA table_info(regime_records)")
            columns = [r[1] for r in cur.fetchall()]
            if "features_json" not in columns:
                self.conn.execute("ALTER TABLE regime_records ADD COLUMN features_json TEXT DEFAULT '{}'")
                self.conn.commit()
                log.info("[DB INIT] +列 regime_records.features_json")
            if "exchange" not in columns:
                self.conn.execute("ALTER TABLE regime_records ADD COLUMN exchange TEXT DEFAULT 'US'")
                self.conn.commit()
                log.info("[DB INIT] +列 regime_records.exchange")
        except Exception as e:
            log.warning(f"[DB INIT] regime_records 检查失败: {e}")

    # ==================== 选股池 ====================

    def add_to_pool(self, items: List[dict]) -> int:
        return self.save_stock_pool(items)

    def save_stock_pool(self, items: List[dict]) -> int:
        count = 0
        for item in items:
            try:
                symbol = item.get("symbol",
                         item.get("stock_code",
                         item.get("vt_symbol", "")))
                self.conn.execute(
                    """INSERT OR REPLACE INTO ai_stock_pool
                       (symbol, market, score, reason, indicators_json,
                        expires_at, status, source, selected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        symbol,
                        item.get("market", "US"),
                        item.get("score", 50.0),
                        item.get("reason", ""),
                        json.dumps(item.get("indicators",
                                     item.get("extra", {})),
                                     ensure_ascii=False),
                        item.get("expires_at", ""),
                        item.get("status", "selected"),
                        item.get("source", "selector"),
                    ),
                )
                count += 1
            except Exception as e:
                log.warning(f"[DB] stock_pool 写入失败: {e}")
        self.conn.commit()
        log.info(f"[DBManager] ✅ 选股池写入 {count} 条记录")
        return count

    def get_stock_pool(self, limit: int = 50) -> List[dict]:
        try:
            cur = self.conn.execute(
                "SELECT symbol, market, score FROM ai_stock_pool "
                "ORDER BY selected_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ==================== ★ 策略 CRUD（核心） ====================

    def save_strategy(self, name: str, class_name: str, vt_symbol: str,
                       market: str = "US", params: Optional[dict] = None,
                       source: str = "pipeline", modifier: str = "system") -> bool:
        params = params or {}
        params_json = json.dumps(params, ensure_ascii=False)
        try:
            cur = self.conn.execute(
                "SELECT version FROM strategy_config WHERE name=?", (name,)
            )
            row = cur.fetchone()
            if row:
                new_version = (row[0] or 0) + 1
                self.conn.execute(
                    """UPDATE strategy_config SET
                       class_name=?, vt_symbol=?, market=?, params_json=?,
                       version=?, active=1, source=?, modifier=?,
                       updated_at=datetime('now')
                       WHERE name=?""",
                    (class_name, vt_symbol, market, params_json,
                     new_version, source, modifier, name),
                )
                log.info(f"[DBManager] ✅ 策略更新: {name} v{new_version}")
            else:
                self.conn.execute(
                    """INSERT INTO strategy_config
                       (name, class_name, vt_symbol, market, params_json,
                        version, active, source, modifier)
                       VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)""",
                    (name, class_name, vt_symbol, market, params_json,
                     source, modifier),
                )
                log.info(f"[DBManager] ✅ 策略保存: {name} v1")

            self.conn.commit()
            self._audit_log("save", name, f"class={class_name} symbol={vt_symbol}")
            return True
        except Exception as e:
            log.error(f"[DBManager] ❌ 策略保存失败 {name}: {e}")
            return False

    def disable_strategy(self, name: str, operator: str = "system") -> bool:
        try:
            self.conn.execute(
                "UPDATE strategy_config SET active=0, updated_at=datetime('now') WHERE name=?",
                (name,)
            )
            self.conn.commit()
            self._audit_log("disable", name, operator=operator)
            log.info(f"[DBManager] 🗑️ 策略已禁用: {name} (by {operator})")
            return True
        except Exception as e:
            log.error(f"[DBManager] ❌ 禁用策略失败 {name}: {e}")
            return False

    def update_strategy_params(self, name: str, new_params: dict) -> bool:
        try:
            cur = self.conn.execute(
                "SELECT params_json, version FROM strategy_config WHERE name=?",
                (name,)
            )
            row = cur.fetchone()
            if not row:
                log.warning(f"[DBManager] {name} 不存在，无法更新参数")
                return False

            old_params = json.loads(row[0] or "{}")
            old_params.update(new_params)
            new_version = (row[1] or 0) + 1

            self.conn.execute(
                """UPDATE strategy_config SET
                   params_json=?, version=?, updated_at=datetime('now')
                   WHERE name=?""",
                (json.dumps(old_params, ensure_ascii=False), new_version, name),
            )
            self.conn.commit()
            self._audit_log("update_params", name, f"v{new_version}")
            log.info(f"[DBManager] ✅ {name} 参数更新 → v{new_version}")
            return True
        except Exception as e:
            log.error(f"[DBManager] ❌ 更新参数失败 {name}: {e}")
            return False

    def get_all_strategies(self, enabled_only: bool = True) -> List[dict]:
        """
        获取所有策略配置
        返回字段与 strategy_engine.py 中 _load_from_db() 对齐：
        strategy_name, class_name, vt_symbol, market, params, version, active
        """
        try:
            sql = ("SELECT name, class_name, vt_symbol, market, params_json, version, active "
                   "FROM strategy_config")
            if enabled_only:
                sql += " WHERE active=1"
            sql += " ORDER BY name"
            cur = self.conn.execute(sql)
            return [
                {
                    "strategy_name": r[0],
                    "class_name": r[1],
                    "vt_symbol": r[2],
                    "market": r[3] or "US",
                    "params": json.loads(r[4] or "{}"),
                    "version": r[5] or 1,
                    "active": bool(r[6]),
                }
                for r in cur.fetchall()
            ]
        except Exception as e:
            log.error(f"[DBManager] 获取策略列表失败: {e}")
            return []

    def get_strategy(self, name: str) -> Optional[dict]:
        try:
            cur = self.conn.execute(
                "SELECT name, class_name, vt_symbol, market, params_json, version, active "
                "FROM strategy_config WHERE name=?", (name,)
            )
            r = cur.fetchone()
            if r:
                return {
                    "strategy_name": r[0],
                    "class_name": r[1],
                    "vt_symbol": r[2],
                    "market": r[3] or "US",
                    "params": json.loads(r[4] or "{}"),
                    "version": r[5] or 1,
                    "active": bool(r[6]),
                }
            return None
        except Exception:
            return None

    def detect_changed_strategies(self, deployed: Dict[str, dict]) -> dict:
        """
        对比数据库与内存中的策略，返回变化：
        {"added": [...], "removed": [...], "updated": [...]}
        """
        changes = {"added": [], "removed": [], "updated": []}
        try:
            db_list = self.get_all_strategies(enabled_only=True)
            db_names = set()
            for s in db_list:
                name = s["strategy_name"]
                db_names.add(name)
                if name not in deployed:
                    changes["added"].append(s)
                else:
                    deployed_info = deployed.get(name, {})
                    if deployed_info.get("params") != s["params"]:
                        changes["updated"].append(s)

            for name in deployed:
                if name not in db_names:
                    changes["removed"].append(name)
            return changes
        except Exception as e:
            log.error(f"[DBManager] detect_changed 失败: {e}")
            return changes

    # ==================== 部署日志相关 ====================

    def log_deploy(self, name: str, version: int, action: str,
                   operator: str = "system", status: str = "success",
                   msg: str = "") -> bool:
        """记录部署日志"""
        try:
            detail = f"action={action}, version={version}, status={status}"
            if msg:
                detail += f", msg={msg}"
            self._audit_log(action, name, detail, operator)
            return True
        except Exception:
            return False

    def mark_deployed(self, name: str, version: int, operator: str = "system") -> bool:
        """标记策略已部署（写入审计日志）"""
        return self.log_deploy(name, version, "deploy", operator, "success")

    def get_param_history(self, vt_symbol: str, class_name: str, limit: int = 20) -> List[dict]:
        """获取参数历史版本"""
        try:
            cur = self.conn.execute(
                """SELECT name, version, params_json, updated_at
                   FROM strategy_config
                   WHERE vt_symbol=? AND class_name=?
                   ORDER BY version DESC LIMIT ?""",
                (vt_symbol, class_name, limit)
            )
            return [
                {
                    "name": r[0],
                    "version": r[1],
                    "params": json.loads(r[2] or "{}"),
                    "updated_at": r[3],
                }
                for r in cur.fetchall()
            ]
        except Exception:
            return []

    def get_param_version(self, vt_symbol: str, class_name: str, version: int) -> Optional[dict]:
        """获取指定版本的参数"""
        try:
            cur = self.conn.execute(
                """SELECT name, params_json
                   FROM strategy_config
                   WHERE vt_symbol=? AND class_name=? AND version=?""",
                (vt_symbol, class_name, version)
            )
            row = cur.fetchone()
            if row:
                return {"name": row[0], "params": json.loads(row[1] or "{}")}
            return None
        except Exception:
            return None

    def log_event(self, timestamp: str, level: str, msg: str, strategy_name: str = "") -> bool:
        """写入事件日志"""
        try:
            self.conn.execute(
                """INSERT INTO strategy_audit_log (action, strategy_name, detail, operator, timestamp)
                   VALUES ('event', ?, ?, 'system', ?)""",
                (strategy_name, f"[{level}] {msg}", timestamp)
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def get_active_strategies(self) -> List[dict]:
        """获取所有活跃策略"""
        return self.get_all_strategies(enabled_only=True)

    # ==================== 获取最新参数 ====================

    def get_latest_params(self, vt_symbol: str, class_name: str) -> dict:
        """
        获取最新参数（AI参数建议）
        优先从 param_optimization_results 表获取，否则从 strategy_config 表获取
        """
        try:
            cur = self.conn.execute(
                "SELECT params_json FROM param_optimization_results "
                "WHERE symbol=? AND strategy_name=? "
                "ORDER BY version DESC LIMIT 1",
                (vt_symbol, class_name)
            )
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])

            cur = self.conn.execute(
                "SELECT params_json FROM strategy_config "
                "WHERE vt_symbol=? AND class_name=? AND active=1 "
                "ORDER BY version DESC LIMIT 1",
                (vt_symbol, class_name)
            )
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return {}
        except Exception as e:
            log.warning(f"[DBManager] get_latest_params 失败: {e}")
            return {}

    # ==================== Regime ====================

    def save_regime(self, symbol: str, regime: str,
                     confidence: float = 0.0, features: Optional[dict] = None) -> bool:
        """
        保存 regime 结果
        features 同时写入 features（TEXT，JSON序列化）和 features_json（TEXT，JSON序列化）
        确保 regime_trainer 的 save() 方法无论写哪个列都能成功
        """
        try:
            features = features or {}
            features_json_str = json.dumps(features, ensure_ascii=False)
            prob_trend = features.get("prob_trend", 0.0)
            prob_range = features.get("prob_range", 0.0)
            prob_volatile = features.get("prob_volatile", 0.0)

            self.conn.execute(
                """INSERT OR REPLACE INTO regime_records
                   (symbol, exchange, regime, confidence,
                    prob_trend, prob_range, prob_volatile,
                    features, features_json, timestamp)
                   VALUES (?, 'US', ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (symbol, regime, confidence, prob_trend, prob_range, prob_volatile,
                 features_json_str, features_json_str),
            )
            self.conn.commit()
            return True
        except Exception as e:
            log.warning(f"[DBManager] regime 保存失败 {symbol}: {e}")
            return False

    def get_latest_regime(self, symbol: str) -> Optional[dict]:
        try:
            cur = self.conn.execute(
                "SELECT regime, confidence, timestamp FROM regime_records "
                "WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,)
            )
            row = cur.fetchone()
            if row:
                return {"regime": row[0], "confidence": row[1], "timestamp": row[2]}
            return None
        except Exception:
            return None

    # ==================== ★ 诊股（核心修正）====================

    def save_diagnosis(self, symbol: str, score=50.0,
                        verdict: str = "未知", details: Optional[dict] = None,
                        vt_symbol: str = "") -> bool:
        """
        保存诊股结果（参数自适应，兼容所有调用方式）
        
        错误根因：调用方（ai/stock_diagnosis.py）可能把 details dict 传到了 score 位置，
        导致 SQLite 收到 dict 而非 float → "Error binding parameter N: type 'dict' is not supported"
        
        修复：检测参数类型，自动纠正；details 同时写入各拆列 + details_json 列。
        """
        try:
            # ---- 参数自适应纠正 ----
            # 情况1：score 位置收到了 dict → 说明调用方把 details 传到了第2个参数
            if isinstance(score, dict):
                details = score
                score = 50.0
            # 情况2：verdict 位置收到了 dict → 说明调用方把 details 传到了第3个参数
            if isinstance(verdict, dict):
                details = verdict
                verdict = "未知"
            # 确保 details 是 dict
            if details is not None and not isinstance(details, dict):
                try:
                    details = json.loads(details) if isinstance(details, str) else {}
                except (json.JSONDecodeError, TypeError):
                    details = {}
            d = details or {}
            # vt_symbol 兜底
            if not vt_symbol:
                vt_symbol = symbol

            # ---- 写入数据库 ----
            self.conn.execute(
                """INSERT OR REPLACE INTO stock_diagnosis
                   (symbol, vt_symbol, score, trend_score, momentum_score, volume_score,
                    volatility, ma_alignment, rsi, macd_signal, verdict, details_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (symbol,
                 vt_symbol,
                 float(score) if not isinstance(score, dict) else 50.0,
                 float(d.get("trend_score", 50.0)),
                 float(d.get("momentum_score", 50.0)),
                 float(d.get("volume_score", 50.0)),
                 float(d.get("volatility", 0.02)),
                 float(d.get("ma_alignment", 50.0)),
                 float(d.get("rsi", 50.0)),
                 float(d.get("macd_signal", 0.0)),
                 str(verdict),
                 json.dumps(d, ensure_ascii=False)),
            )
            self.conn.commit()
            return True
        except Exception as e:
            log.warning(f"[DBManager] diagnosis 保存失败 {symbol}: {e}")
            return False

    # ==================== Prelive 门禁结果 ====================

    def save_prelive_result(self, vt_symbol: str, strategy_class: str,
                             version: int = 0, modifier: str = "system",
                             passed: bool = False,
                             total_return: float = 0.0,
                             sharpe_ratio: float = 0.0,
                             max_drawdown: float = 0.0,
                             total_trade_count: int = 0,
                             reason: str = "") -> bool:
        try:
            self.conn.execute(
                """INSERT INTO prelive_results
                   (vt_symbol, strategy_class, version, modifier,
                    passed, total_return, sharpe_ratio, max_drawdown,
                    total_trade_count, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (vt_symbol, strategy_class, version, modifier,
                 1 if passed else 0,
                 total_return, sharpe_ratio, max_drawdown,
                 total_trade_count, reason),
            )
            self.conn.commit()
            return True
        except Exception as e:
            log.warning(f"[DBManager] prelive_result 保存失败: {e}")
            return False

    # ==================== 策略绩效 ====================

    def save_performance(self, strategy_name: str, vt_symbol: str,
                         sharpe: float = 0.0, total_return: float = 0.0,
                         max_dd: float = 0.0, win_rate: float = 0.0,
                         regime: str = "", period_days: int = 30):
        try:
            self.conn.execute(
                """INSERT INTO strategy_performance
                   (strategy_name, vt_symbol, regime, sharpe, total_return,
                    max_drawdown, win_rate, period_days, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (strategy_name, vt_symbol, regime, sharpe, total_return,
                 max_dd, win_rate, period_days),
            )
            self.conn.commit()
        except Exception as e:
            log.warning(f"[DBManager] 绩效保存失败: {e}")

    def get_best_params(self, vt_symbol: str, class_name: str) -> dict:
        try:
            cur = self.conn.execute(
                """SELECT params_json FROM strategy_config
                   WHERE vt_symbol=? AND class_name=? AND active=1
                   ORDER BY version DESC LIMIT 1""",
                (vt_symbol, class_name),
            )
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return {}
        except Exception:
            return {}

    # ==================== 参数优化结果 ====================

    def save_optimization_result(self, symbol: str, strategy_name: str,
                                  params: dict, performance: dict,
                                  regime: str = "all", version: int = 1) -> bool:
        try:
            self.conn.execute(
                """INSERT OR REPLACE INTO param_optimization_results
                   (symbol, strategy_name, regime, params_json, performance_json, version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                (symbol, strategy_name, regime,
                 json.dumps(params, ensure_ascii=False),
                 json.dumps(performance, ensure_ascii=False),
                 version),
            )
            self.conn.commit()
            return True
        except Exception as e:
            log.warning(f"[DBManager] optimization 保存失败: {e}")
            return False

    def get_optimization_result(self, symbol: str, strategy_name: str) -> dict:
        try:
            cur = self.conn.execute(
                """SELECT params_json FROM param_optimization_results
                   WHERE symbol=? AND strategy_name=?
                   ORDER BY version DESC LIMIT 1""",
                (symbol, strategy_name),
            )
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return {}
        except Exception:
            return {}

    # ==================== 审计日志 ====================

    def _audit_log(self, action: str, strategy_name: Optional[str] = None,
                    detail: str = "", operator: str = "system"):
        try:
            self.conn.execute(
                """INSERT INTO strategy_audit_log (action, strategy_name, detail, operator)
                   VALUES (?, ?, ?, ?)""",
                (action, strategy_name, detail, operator),
            )
            self.conn.commit()
        except Exception:
            pass

    def get_audit_log(self, limit: int = 50) -> List[dict]:
        try:
            cur = self.conn.execute(
                "SELECT action, strategy_name, detail, operator, timestamp "
                "FROM strategy_audit_log ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ==================== 工具方法 ====================

    def execute(self, sql: str, params: tuple = ()):
        self.conn.execute(sql, params)
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()
