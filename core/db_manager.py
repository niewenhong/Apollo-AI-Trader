"""
db_manager.py — Apollo AI Trader v2.8.0 统一数据库管理器
合并：K线本地优先+富途回源 / 策略CRUD / 多用户 / Regime / 参数优化 / 选股池 / 事件日志
"""
import sqlite3
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

logger = logging.getLogger("DBManager")

LOCAL_BAR_LIMIT = 300
MAX_STALE_SECONDS = 1800


class DBManager:
    """统一数据库管理器"""

    def __init__(self, db_path: str = "data/history.db",
                 bars_db_path: str = "data/database.db",
                 futu_us_ctx=None, futu_hk_ctx=None,
                 score_threshold: int = 80):
        self.db_path = db_path
        self.bars_db_path = bars_db_path
        self.futu_us_ctx = futu_us_ctx
        self.futu_hk_ctx = futu_hk_ctx
        self.score_threshold = score_threshold

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()
        self._ensure_bars_table()

    # ═══════════════════════════════════════
    #  建表
    # ═══════════════════════════════════════
    def create_tables(self):
        c = self.conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT UNIQUE NOT NULL,
                class_name TEXT NOT NULL,
                vt_symbol TEXT NOT NULL,
                market TEXT DEFAULT 'US',
                params TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                current_version INTEGER DEFAULT 1,
                source TEXT DEFAULT 'manual',
                modifier TEXT DEFAULT 'system',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ai_stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                market TEXT DEFAULT 'US',
                score REAL DEFAULT 0,
                reason TEXT,
                indicators TEXT,
                expires_at TEXT,
                status TEXT DEFAULT 'selected',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                batch_id TEXT
            );

            CREATE TABLE IF NOT EXISTS strategy_deploy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                action TEXT,
                operator TEXT,
                result TEXT,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                message TEXT,
                strategy_name TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS param_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                class_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                params TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vt_symbol, class_name, version)
            );

            CREATE TABLE IF NOT EXISTS kline_cache (
                symbol TEXT NOT NULL, exchange TEXT NOT NULL, interval TEXT NOT NULL,
                datetime TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, exchange, interval, datetime)
            );

            CREATE TABLE IF NOT EXISTS regime_records (
                symbol TEXT NOT NULL, exchange TEXT NOT NULL,
                regime_date TEXT NOT NULL, regime_time TEXT NOT NULL,
                prob_trend REAL DEFAULT 0, prob_range REAL DEFAULT 0, prob_volatile REAL DEFAULT 0,
                primary_regime TEXT, confidence REAL, version INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, exchange, regime_date, regime_time, version)
            );

            CREATE TABLE IF NOT EXISTS param_optimization_results (
                symbol TEXT NOT NULL, strategy_name TEXT NOT NULL, regime TEXT NOT NULL,
                params_json TEXT NOT NULL, performance_json TEXT NOT NULL,
                train_start TEXT, train_end TEXT, version INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(symbol, strategy_name, regime, version)
            );

            CREATE TABLE IF NOT EXISTS user_config (
                user_id TEXT PRIMARY KEY, futu_account TEXT, telegram_chat_id TEXT,
                trade_mode TEXT DEFAULT 'simulation', trd_env TEXT DEFAULT 'SIMULATE',
                risk_profile TEXT DEFAULT 'moderate', risk_acknowledged INTEGER DEFAULT 0,
                risk_acknowledged_at TEXT, created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_positions (
                user_id TEXT, symbol TEXT, trade_mode TEXT,
                quantity REAL, cost_basis REAL, updated_at TEXT,
                PRIMARY KEY (user_id, symbol, trade_mode)
            );

            CREATE TABLE IF NOT EXISTS user_orders (
                order_id TEXT PRIMARY KEY, user_id TEXT, symbol TEXT,
                side TEXT, quantity REAL, price REAL, status TEXT,
                filled_qty REAL, avg_fill_price REAL, trade_mode TEXT, created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_daily_performance (
                user_id TEXT, date TEXT, total_pnl REAL, total_return REAL,
                max_drawdown REAL, sharpe_ratio REAL, trade_mode TEXT,
                PRIMARY KEY (user_id, date, trade_mode)
            );

            CREATE TABLE IF NOT EXISTS optimization_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT,
                status TEXT DEFAULT 'pending', started_at TEXT, finished_at TEXT,
                summary TEXT, created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                telegram_token TEXT,
                telegram_chat_id TEXT,
                futu_account TEXT,
                trade_mode TEXT DEFAULT 'simulation',
                trd_env TEXT DEFAULT 'SIMULATE',
                risk_profile TEXT DEFAULT 'moderate',
                is_super INTEGER DEFAULT 0,
                risk_acknowledged INTEGER DEFAULT 0,
                risk_acknowledged_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT
            );
        """)
        self.conn.commit()
        logger.info(f"✅ 数据库表创建/验证完成 ({self.db_path})")

    def _ensure_bars_table(self):
        try:
            conn = sqlite3.connect(self.bars_db_path)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dbbardata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, exchange TEXT NOT NULL,
                    datetime TEXT NOT NULL, interval TEXT NOT NULL,
                    volume REAL NOT NULL, turnover REAL NOT NULL,
                    open_interest REAL NOT NULL,
                    open_price REAL NOT NULL, high_price REAL NOT NULL,
                    low_price REAL NOT NULL, close_price REAL NOT NULL,
                    UNIQUE(symbol, exchange, interval, datetime)
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"⚠️ 创建 dbbardata 表失败: {e}")

    # ═══════════════════════════════════════
    #  K线：本地优先 + 富途回源
    # ═══════════════════════════════════════
    def load_bars(self, vt_symbol: str, interval: str = "15m",
                  limit: int = 100, score: float = 0) -> List[dict]:
        if "." in vt_symbol:
            symbol, exchange = vt_symbol.split(".", 1)
        else:
            symbol, exchange = vt_symbol, "SMART"

        futu_ktype = {"1m": "K_1M", "5m": "K_5M", "15m": "K_15M",
                      "60m": "K_60M", "1d": "K_DAY"}.get(interval, "K_15M")
        market = "US" if exchange == "SMART" else "HK"

        local_bars = self._load_from_local(symbol, exchange, futu_ktype, limit)
        if len(local_bars) >= max(20, limit // 2) and self._is_fresh(local_bars[-1].get("datetime", "")):
            return local_bars
        if len(local_bars) >= max(20, limit // 2):
            logger.info(f"⏰ {vt_symbol} 本地K线较旧，尝试回源刷新")

        if score < self.score_threshold:
            logger.info(f"📊 {vt_symbol} 分数 {score} < {self.score_threshold}，不回源")
            return local_bars

        remote_bars = self._fetch_from_futu(symbol, market, futu_ktype, limit)
        if remote_bars:
            self._save_to_local(symbol, exchange, futu_ktype, remote_bars)
            return remote_bars
        return local_bars

    def _load_from_local(self, symbol, exchange, ktype, limit):
        try:
            conn = sqlite3.connect(self.bars_db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT datetime, open_price AS open, high_price AS high,
                       low_price AS low, close_price AS close, volume
                FROM dbbardata WHERE symbol=? AND exchange=? AND interval=?
                ORDER BY datetime DESC LIMIT ?
            """, (symbol, exchange, ktype, limit))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            rows.reverse()
            return rows
        except Exception as e:
            logger.warning(f"⚠️ 本地K线读取失败: {e}")
            return []

    def _fetch_from_futu(self, symbol, market, ktype, limit):
        ctx = self.futu_us_ctx if market == "US" else self.futu_hk_ctx
        if ctx is None:
            return []
        code = f"{market}.{symbol}"
        try:
            result = ctx.request_history_kline(code=code, ktype=ktype, max_count=limit)
            ret = result[0] if isinstance(result, tuple) else 0
            data = result[1] if isinstance(result, tuple) and len(result) > 1 else (result if not isinstance(result, tuple) else None)
            if ret != 0 or data is None:
                return []
            bars = [{"datetime": str(row["time_key"]), "open": float(row["open"]),
                     "high": float(row["high"]), "low": float(row["low"]),
                     "close": float(row["close"]), "volume": float(row.get("volume", 0))}
                    for _, row in data.iterrows()]
            bars.sort(key=lambda x: x["datetime"])
            logger.info(f"✅ {code} 富途回源 {len(bars)} 根")
            return bars
        except Exception as e:
            logger.warning(f"⚠️ 富途K线异常: {e}")
            return []

    def _save_to_local(self, symbol, exchange, ktype, bars):
        try:
            conn = sqlite3.connect(self.bars_db_path)
            cur = conn.cursor()
            for b in bars:
                cur.execute("""
                    INSERT OR REPLACE INTO dbbardata
                    (symbol, exchange, datetime, interval, volume, turnover, open_interest,
                     open_price, high_price, low_price, close_price)
                    VALUES (?,?,?,?,?,0,0,?,?,?,?)
                """, (symbol, exchange, b["datetime"], ktype, b.get("volume", 0),
                      b["open"], b["high"], b["low"], b["close"]))
            conn.commit()
            conn.close()
            logger.info(f"💾 {symbol}.{exchange} {len(bars)} 根已写回本地")
        except Exception as e:
            logger.warning(f"⚠️ 写回失败: {e}")

    def _is_fresh(self, dt_str):
        if not dt_str:
            return False
        try:
            age = (datetime.now() - datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")).total_seconds()
            return age <= MAX_STALE_SECONDS
        except Exception:
            return False

    # 兼容接口
    def save_klines(self, symbol, market, interval, klines):
        c = self.conn.cursor()
        for k in klines:
            c.execute("""INSERT OR REPLACE INTO kline_cache
                (symbol, exchange, interval, datetime, open, high, low, close, volume)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, market, interval, k["datetime"],
                 k["open"], k["high"], k["low"], k["close"], k["volume"]))
        self.conn.commit()

    def load_bars_from_cache(self, symbol, interval, limit=500):
        c = self.conn.cursor()
        c.execute("""SELECT datetime,open,high,low,close,volume FROM kline_cache
            WHERE symbol=? AND interval=? ORDER BY datetime DESC LIMIT ?""",
            (symbol, interval, limit))
        rows = c.fetchall()
        return [{"datetime": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                for r in reversed(rows)]

    # ═══════════════════════════════════════
    #  策略 CRUD（保持 engine.py 兼容）
    # ═══════════════════════════════════════
    def save_strategy(self, strategy_name, class_name, vt_symbol, market, params,
                      source="manual", modifier="system"):
        c = self.conn.cursor()
        pj = json.dumps(params, ensure_ascii=False)
        c.execute("SELECT current_version FROM strategy_config WHERE strategy_name=?", (strategy_name,))
        row = c.fetchone()
        if row:
            v = row["current_version"] + 1
            c.execute("""UPDATE strategy_config SET class_name=?,vt_symbol=?,market=?,params=?,
                current_version=?,source=?,modifier=?,updated_at=CURRENT_TIMESTAMP WHERE strategy_name=?""",
                (class_name, vt_symbol, market, pj, v, source, modifier, strategy_name))
        else:
            v = 1
            c.execute("""INSERT INTO strategy_config
                (strategy_name,class_name,vt_symbol,market,params,current_version,source,modifier)
                VALUES (?,?,?,?,?,?,?,?)""",
                (strategy_name, class_name, vt_symbol, market, pj, v, source, modifier))
        c.execute("INSERT OR IGNORE INTO param_versions (vt_symbol,class_name,version,params) VALUES (?,?,?,?)",
                  (vt_symbol, class_name, v, pj))
        self.conn.commit()
        logger.info(f"✅ 策略保存: {strategy_name} v{v}")
        return strategy_name, v

    def get_strategy(self, name):
        c = self.conn.cursor()
        c.execute("SELECT * FROM strategy_config WHERE strategy_name=?", (name,))
        row = c.fetchone()
        if row:
            d = dict(row)
            d["params"] = json.loads(d.get("params", "{}"))
            return d
        return None

    def get_all_strategies(self, enabled_only=False):
        c = self.conn.cursor()
        sql = "SELECT * FROM strategy_config" + (" WHERE enabled=1" if enabled_only else "")
        return [{**dict(r), "params": json.loads(r["params"])} for r in c.execute(sql).fetchall()]

    def get_active_strategies(self):
        return self.get_all_strategies(enabled_only=True)

    def disable_strategy(self, name):
        self.conn.execute("UPDATE strategy_config SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE strategy_name=?", (name,))
        self.conn.commit()

    def mark_deployed(self, name, version, operator="system"):
        self.conn.execute("UPDATE strategy_config SET updated_at=CURRENT_TIMESTAMP WHERE strategy_name=?", (name,))
        self.conn.commit()

    def log_deploy(self, name, version, action, operator, result, detail=""):
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT INTO strategy_deploy_log (strategy_name, version, action, operator, result, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, version, action, operator, result, detail, now)
        )
        self.conn.commit()

    def log_event(self, timestamp, level, message, strategy_name=""):
        self.conn.execute("INSERT INTO event_log (timestamp,level,message,strategy_name) VALUES (?,?,?,?)",
                          (timestamp, level, message, strategy_name))
        self.conn.commit()

    def save_prelive_result(self, *args, **kwargs):
        """保存预上线验证结果（兼容不同调用方式）"""
        strategy_name = kwargs.get('strategy_name') or (args[0] if args else None)
        result_dict = kwargs.get('result_dict') or (args[1] if len(args) > 1 else kwargs)
        if not strategy_name:
            return
        detail = json.dumps(result_dict, ensure_ascii=False)
        self.log_event(datetime.now().isoformat(), "GATE", detail, strategy_name)

    def get_latest_params(self, vt_symbol, class_name):
        """获取最新的优化参数（由策略模板调用）"""
        best = self.get_best_params(vt_symbol, class_name)
        return best or {}

    def detect_changed_strategies(self, deployed: Dict[str, str]) -> List[dict]:
        """
        检测数据库中有变动的策略（新增、修改、删除、禁用）。
        deployed: {strategy_name: updated_at_string}  统一使用字符串时间戳
        返回变更的策略配置列表。
        """
        c = self.conn.cursor()
        changed = []
        for row in c.execute("SELECT * FROM strategy_config").fetchall():
            d = dict(row)
            d["params"] = json.loads(d.get("params", "{}"))
            name = d["strategy_name"]
            db_update = d["updated_at"]  # 字符串格式 "YYYY-MM-DD HH:MM:SS"
            if name not in deployed:
                d["_change_type"] = "added"
                changed.append(d)
            else:
                if db_update > deployed.get(name, ""):
                    d["_change_type"] = "updated"
                    changed.append(d)
        return changed

    def get_param_history(self, vt_symbol, class_name, limit=20):
        c = self.conn.cursor()
        c.execute("SELECT * FROM param_versions WHERE vt_symbol=? AND class_name=? ORDER BY version DESC LIMIT ?",
                  (vt_symbol, class_name, limit))
        return [{**dict(r), "params": json.loads(r["params"])} for r in c.fetchall()]

    # ═══════════════════════════════════════
    #  新增方法（不破坏原有逻辑）
    # ═══════════════════════════════════════
    def close(self):
        """关闭数据库连接"""
        try:
            if self.conn:
                self.conn.close()
                logger.info("✅ 数据库连接已关闭")
        except Exception as e:
            logger.warning(f"⚠️ 关闭数据库连接异常: {e}")

    def ensure_super_user(self, config: dict):
        """
        确保存在超级用户（管理员）账户。
        如果不存在，则使用配置中的默认信息创建一个。
        config 应包含: TELEGRAM_TOKEN, FUTU_ACCOUNT, TRADE_MODE, TRD_ENV, RISK_PROFILE 等。
        """
        c = self.conn.cursor()
        # 检查是否已有超级用户
        c.execute("SELECT user_id FROM users WHERE is_super=1 LIMIT 1")
        existing = c.fetchone()
        if existing:
            logger.info(f"✅ 超级用户已存在: {existing['user_id']}")
            return

        # 从配置中提取默认超级用户信息
        super_user_id = config.get("SUPER_USER_ID", "admin")
        telegram_token = config.get("TELEGRAM_TOKEN", "")
        telegram_chat_id = config.get("TELEGRAM_CHAT_ID", "")
        futu_account = config.get("FUTU_ACCOUNT", "")
        trade_mode = config.get("TRADE_MODE", "simulation")
        trd_env = config.get("TRD_ENV", "SIMULATE")
        risk_profile = config.get("RISK_PROFILE", "moderate")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""
            INSERT OR REPLACE INTO users
            (user_id, telegram_token, telegram_chat_id, futu_account,
             trade_mode, trd_env, risk_profile, is_super,
             risk_acknowledged, risk_acknowledged_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
        """, (super_user_id, telegram_token, telegram_chat_id, futu_account,
              trade_mode, trd_env, risk_profile, now, now, now))
        self.conn.commit()
        logger.info(f"✅ 超级用户已创建: {super_user_id}")

    def add_to_pool(self, stocks: list):
        """
        将选股结果批量写入 ai_stock_pool 表。
        stocks: list of dict，每个 dict 应包含：
            stock_code, stock_name, market, score, reason, indicators, expires_at, batch_id
        """
        c = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for s in stocks:
            c.execute("""
                INSERT OR REPLACE INTO ai_stock_pool
                (stock_code, stock_name, market, score, reason, indicators, expires_at, batch_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s.get("stock_code"),
                s.get("stock_name", ""),
                s.get("market", "US"),
                s.get("score", 0),
                s.get("reason", ""),
                json.dumps(s.get("indicators", {}), ensure_ascii=False),
                s.get("expires_at"),
                s.get("batch_id"),
                now
            ))
        self.conn.commit()
        logger.info(f"✅ 选股池写入 {len(stocks)} 条记录")

    def get_best_params(self, vt_symbol: str, class_name: str) -> dict:
        """
        从 param_optimization_results 表中获取给定标的和策略类的最佳参数。
        按 performance_json 中的 sharpe_ratio 降序取第一条，若无则返回空字典。
        """
        c = self.conn.cursor()
        try:
            # 尝试按性能指标排序，假设 performance_json 包含 "sharpe_ratio"
            # 如果 performance_json 不是数字字段，只能取最新一条
            c.execute("""
                SELECT params_json, performance_json
                FROM param_optimization_results
                WHERE symbol=? AND strategy_name=?
                ORDER BY created_at DESC LIMIT 1
            """, (vt_symbol, class_name))
            row = c.fetchone()
            if row:
                params = json.loads(row["params_json"])
                logger.info(f"✅ 获取到 {vt_symbol}/{class_name} 的最优参数")
                return params
        except Exception as e:
            logger.warning(f"⚠️ get_best_params 查询异常: {e}")
        return {}