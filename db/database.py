"""
Database — SQLite via SQLAlchemy
Tables: trades, signals, strategy_performance, regime_log, settings, backtest_results
"""
import json
import logging
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("DATABASE")

class Database:
    def __init__(self, db_path: str = "data/bot.db"):
        logger.info(f"[DB] Initializing database at {db_path}")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self._init_schema()
        logger.info(f"[DB] ✅ Database initialized successfully")

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        logger.info(f"[DB] Creating/verifying database schema...")
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                instrument TEXT,
                strike INTEGER,
                option_type TEXT,   -- CE or PE
                expiry TEXT,
                action TEXT,        -- BUY / SELL
                qty INTEGER,
                entry_price REAL,
                exit_price REAL,
                entry_time TEXT,
                exit_time TEXT,
                sl_price REAL,
                target1_price REAL,
                target2_price REAL,
                pnl REAL,
                pnl_pct REAL,
                status TEXT,        -- OPEN / CLOSED / SL_HIT / TARGET_HIT / EXPIRED
                strategies_voted TEXT,  -- JSON
                confidence_score REAL,
                regime TEXT,
                market_conditions TEXT, -- JSON
                upstox_order_id TEXT,
                paper_trade INTEGER DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                instrument TEXT,
                signal_type TEXT,   -- BUY_CE / BUY_PE / NO_TRADE
                score REAL,
                strategies TEXT,    -- JSON
                regime TEXT,
                confidence REAL,
                acted_on INTEGER DEFAULT 0,
                trade_id TEXT
            );

            CREATE TABLE IF NOT EXISTS strategy_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT,
                date TEXT,
                signals_given INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                win_rate REAL,
                avg_return REAL,
                weight REAL
            );

            CREATE TABLE IF NOT EXISTS regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                instrument TEXT,
                regime TEXT,
                confidence REAL,
                adx REAL,
                rsi REAL,
                vix REAL,
                pcr REAL,
                ema_cross TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                gross_pnl REAL DEFAULT 0,
                net_pnl REAL DEFAULT 0,
                trades_taken INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                opening_capital REAL,
                closing_capital REAL,
                max_drawdown REAL DEFAULT 0,
                best_trade REAL DEFAULT 0,
                worst_trade REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                period_start TEXT,
                period_end TEXT,
                total_trades INTEGER,
                win_rate REAL,
                avg_return REAL,
                max_drawdown REAL,
                sharpe_ratio REAL,
                total_return REAL,
                strategy_breakdown TEXT,  -- JSON
                config_snapshot TEXT      -- JSON
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(entry_time);
            CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp);
            """)

    # ── Trades ─────────────────────────────────────────────────
    def insert_trade(self, trade: dict) -> int:
        logger.info(f"[DB] Inserting trade: {trade.get('trade_id')} | {trade.get('qty')}x {trade.get('instrument')} {trade.get('strike')}{trade.get('option_type')} @ ₹{trade.get('entry_price'):.2f}")
        with self._conn() as conn:
            cols = ", ".join(trade.keys())
            placeholders = ", ".join(["?"] * len(trade))
            vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in trade.values()]
            conn.execute(f"INSERT OR REPLACE INTO trades ({cols}) VALUES ({placeholders})", vals)
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info(f"[DB] ✅ Trade inserted with row_id={row_id}")
        return row_id

    def update_trade(self, trade_id: str, updates: dict):
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in updates.values()]
        vals.append(trade_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE trades SET {sets} WHERE trade_id=?", vals)

    def get_open_trades(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
            return [dict(r) for r in rows]

    def get_trades(self, limit=100, offset=0, status=None, instrument=None) -> List[Dict]:
        q = "SELECT * FROM trades WHERE 1=1"
        params = []
        if status:
            q += " AND status=?"; params.append(status)
        if instrument:
            q += " AND instrument=?"; params.append(instrument)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    def get_trade_by_id(self, trade_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades WHERE trade_id=?", [trade_id]).fetchone()
            return dict(row) if row else None

    # ── Signals ────────────────────────────────────────────────
    def insert_signal(self, signal: dict):
        logger.info(f"[DB] Inserting signal: {signal.get('signal_type')} for {signal.get('instrument')} | Score={signal.get('score'):.0%}")
        with self._conn() as conn:
            cols = ", ".join(signal.keys())
            placeholders = ", ".join(["?"] * len(signal))
            vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in signal.values()]
            conn.execute(f"INSERT INTO signals ({cols}) VALUES ({placeholders})", vals)
        logger.info(f"[DB] ✅ Signal saved")

    def get_signals(self, limit=50) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", [limit]).fetchall()
            return [dict(r) for r in rows]

    # ── Daily P&L ──────────────────────────────────────────────
    def upsert_daily_pnl(self, data: dict):
        with self._conn() as conn:
            cols = ", ".join(data.keys())
            ph = ", ".join(["?"] * len(data))
            update = ", ".join(f"{k}=excluded.{k}" for k in data if k != "date")
            vals = list(data.values())
            conn.execute(
                f"INSERT INTO daily_pnl ({cols}) VALUES ({ph}) ON CONFLICT(date) DO UPDATE SET {update}",
                vals
            )

    def get_daily_pnl(self, days=30) -> List[Dict]:
        logger.info(f"[DB] Fetching daily P&L for last {days} days...")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", [days]
            ).fetchall()
            pnl_list = [dict(r) for r in rows]
        logger.info(f"[DB] ✅ Retrieved {len(pnl_list)} daily P&L records")
        return pnl_list

    # ── Strategy Performance ───────────────────────────────────
    def get_strategy_performance(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT strategy_name,
                       SUM(signals_given) as total_signals,
                       SUM(correct) as total_correct,
                       AVG(win_rate) as avg_win_rate,
                       AVG(avg_return) as avg_return,
                       MAX(weight) as current_weight
                FROM strategy_performance
                GROUP BY strategy_name
            """).fetchall()
            return [dict(r) for r in rows]

    def log_strategy_performance(self, data: dict):
        with self._conn() as conn:
            cols = ", ".join(data.keys())
            ph = ", ".join(["?"] * len(data))
            conn.execute(f"INSERT INTO strategy_performance ({cols}) VALUES ({ph})", list(data.values()))

    # ── Regime Log ─────────────────────────────────────────────
    def log_regime(self, data: dict):
        regime_type = data.get('regime')
        confidence = data.get('confidence', 0)
        logger.info(f"[DB] Logging regime: {regime_type} (confidence={confidence:.0%})")
        with self._conn() as conn:
            cols = ", ".join(data.keys())
            ph = ", ".join(["?"] * len(data))
            conn.execute(f"INSERT INTO regime_log ({cols}) VALUES ({ph})", list(data.values()))
        logger.info(f"[DB] ✅ Regime logged")

    def get_regime_history(self, limit=100) -> List[Dict]:
        logger.info(f"[DB] Fetching last {limit} regime records...")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM regime_log ORDER BY timestamp DESC LIMIT ?", [limit]
            ).fetchall()
            history = [dict(r) for r in rows]
        logger.info(f"[DB] ✅ Retrieved {len(history)} regime records")
        return history

    # ── Settings ───────────────────────────────────────────────
    def set_setting(self, key: str, value):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings(key, value, updated_at) VALUES(?,?,datetime('now'))",
                [key, json.dumps(value)]
            )

    def get_setting(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM bot_settings WHERE key=?", [key]).fetchone()
            if row:
                return json.loads(row[0])
            return default

    # ── Backtest ───────────────────────────────────────────────
    def save_backtest(self, result: dict):
        with self._conn() as conn:
            cols = ", ".join(result.keys())
            ph = ", ".join(["?"] * len(result))
            vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in result.values()]
            conn.execute(f"INSERT INTO backtest_results ({cols}) VALUES ({ph})", vals)

    def get_backtests(self, limit=10) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_results ORDER BY run_date DESC LIMIT ?", [limit]
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Stats ──────────────────────────────────────────────────
    def get_summary_stats(self) -> Dict:
        with self._conn() as conn:
            r = conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    MAX(pnl) as best_trade,
                    MIN(pnl) as worst_trade,
                    AVG(CASE WHEN pnl > 0 THEN pnl_pct END) as avg_win_pct,
                    AVG(CASE WHEN pnl <= 0 THEN pnl_pct END) as avg_loss_pct
                FROM trades WHERE status != 'OPEN'
            """).fetchone()
            stats = dict(r) if r else {}
            if stats.get("total_trades") and stats["total_trades"] > 0:
                stats["win_rate"] = round((stats["wins"] or 0) / stats["total_trades"] * 100, 1)
            else:
                stats["win_rate"] = 0
            return stats
        
    def insert_backtest_result(self, result: dict):
        if "error" in result or "total_trades" not in result:
            logger.warning(f"[DB] Skipping backtest_result insert — incomplete result: {result}")
            return
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO backtest_results
                (period_start, period_end, run_date, total_trades, win_rate,
                avg_return, max_drawdown, sharpe_ratio, total_return, equity_curve)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [
                result["period_start"], result["period_end"], str(datetime.now()),
                result["total_trades"], result["win_rate"], result.get("avg_win", 0),
                result["max_drawdown_pct"], result["sharpe_ratio"],
                result["total_return_pct"], json.dumps(result["equity_curve"]),
            ])