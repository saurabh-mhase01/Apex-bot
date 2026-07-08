import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from collections import defaultdict

from pandas import Timestamp

import pandas as pd

from db.database import Database

logger = logging.getLogger("DATA_ENGINE")


class DataEngine:
    """Persistent data ingestion layer for candles and market snapshots."""

    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()
        self._ensure_schema()
        self._live_bars: Dict[tuple, Dict[str, Any]] = {}
        self._live_bar_lock = None

    def _ensure_schema(self) -> None:
        with self.db._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_data_candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    source TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(instrument, interval, timestamp)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_data_candles_instr_interval ON market_data_candles(instrument, interval, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_snapshots_instr_type ON market_snapshots(instrument, snapshot_type, created_at)"
            )

    def save_candles(self, instrument: str, interval: str, candles: List[Dict[str, Any]]) -> int:
        if not candles:
            return 0

        rows = []
        for candle in candles:
            ts = candle.get("timestamp")
            if isinstance(ts, datetime):
                ts_value = ts.isoformat()
            else:
                ts_value = str(ts)
            rows.append(
                (
                    instrument,
                    interval,
                    ts_value,
                    float(candle.get("open", 0.0)),
                    float(candle.get("high", 0.0)),
                    float(candle.get("low", 0.0)),
                    float(candle.get("close", 0.0)),
                    float(candle.get("volume", 0.0)),
                    candle.get("source", "live"),
                )
            )

        with self.db._conn() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO market_data_candles
                (instrument, interval, timestamp, open, high, low, close, volume, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_candles(self, instrument: str, interval: str, limit: Optional[int] = None) -> pd.DataFrame:
        query = "SELECT timestamp, open, high, low, close, volume FROM market_data_candles WHERE instrument=? AND interval=?"
        params: List[Any] = [instrument, interval]
        if limit is not None:
            query += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
        else:
            query += " ORDER BY timestamp ASC"

        with self.db._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df = df.astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"})
        return df

    def get_candles_with_live_bar(self, instrument: str, interval: str, limit: Optional[int] = None, timestamp: Optional[datetime] = None) -> pd.DataFrame:
        df = self.get_candles(instrument, interval, limit=limit)
        if df.empty:
            return df

        bar = self.get_live_bar(instrument, interval, timestamp=timestamp)
        if bar is None:
            return df

        ts = bar["timestamp"]
        if ts in df.index:
            return df

        new_row = pd.DataFrame(
            [{"open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"], "volume": bar["volume"]}],
            index=[pd.Timestamp(ts)],
        )
        return pd.concat([df, new_row]).sort_index()

    def get_last_candle_timestamp(self, instrument: str, interval: str) -> Optional[datetime]:
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT timestamp FROM market_data_candles WHERE instrument=? AND interval=? ORDER BY timestamp DESC LIMIT 1",
                [instrument, interval],
            ).fetchone()
        if not row:
            return None
        return pd.to_datetime(row[0]).to_pydatetime()

    def ingest_from_broker(self, broker, instrument: str, interval: str, from_dt: Optional[datetime] = None,
                           to_dt: Optional[datetime] = None) -> pd.DataFrame:
        last_ts = self.get_last_candle_timestamp(instrument, interval)
        if from_dt is None:
            if last_ts is None:
                from_dt = datetime.now() - timedelta(days=30)
            else:
                from_dt = last_ts
        if to_dt is None:
            to_dt = datetime.now()

        df = broker.get_ohlcv_range(instrument, interval=interval, from_date=from_dt, to_date=to_dt)
        if not df.empty:
            self.save_candles(
                instrument,
                interval,
                [
                    {
                        "timestamp": idx,
                        "open": row.get("open", 0.0),
                        "high": row.get("high", 0.0),
                        "low": row.get("low", 0.0),
                        "close": row.get("close", 0.0),
                        "volume": row.get("volume", 0.0),
                        "source": "broker",
                    }
                    for idx, row in df.iterrows()
                ],
            )
        return df

    def update_live_bar(self, instrument: str, interval: str, price: float, volume: float = 0.0, timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        if timestamp is None:
            timestamp = datetime.now()
        bucket = (instrument, interval, timestamp.replace(second=0, microsecond=0))
        bar = self._live_bars.get(bucket)
        if bar is None:
            bar = {
                "instrument": instrument,
                "interval": interval,
                "timestamp": timestamp.replace(second=0, microsecond=0),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "source": "live",
            }
            self._live_bars[bucket] = bar
            return bar

        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["close"] = price
        bar["volume"] += volume
        return bar

    def flush_live_bar(self, instrument: str, interval: str, timestamp: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        if timestamp is None:
            timestamp = datetime.now()
        bucket = (instrument, interval, timestamp.replace(second=0, microsecond=0))
        bar = self._live_bars.pop(bucket, None)
        if not bar:
            return None
        self.save_candles(instrument, interval, [bar])
        return bar

    def get_live_bar(self, instrument: str, interval: str, timestamp: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        if timestamp is None:
            timestamp = datetime.now()
            bucket = (instrument, interval, timestamp.replace(second=0, microsecond=0))
            bar = self._live_bars.get(bucket)
            if bar is not None:
                return bar

            matching = [
                entry for (bar_instrument, bar_interval, _), entry in self._live_bars.items()
                if bar_instrument == instrument and bar_interval == interval
            ]
            if matching:
                return max(matching, key=lambda item: item.get("timestamp", datetime.min))
            return None

        bucket = (instrument, interval, timestamp.replace(second=0, microsecond=0))
        return self._live_bars.get(bucket)

    def maybe_flush_expired_bars(self, instrument: str, interval: str, timestamp: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if timestamp is None:
            timestamp = datetime.now()
        current_bucket = timestamp.replace(second=0, microsecond=0)
        flushed = []
        for (bar_instrument, bar_interval, bar_time), bar in list(self._live_bars.items()):
            if bar_instrument != instrument or bar_interval != interval:
                continue
            if bar_time < current_bucket:
                flushed.append(self.flush_live_bar(instrument, interval, bar_time))
        return [item for item in flushed if item is not None]

    def save_market_snapshot(self, instrument: str, snapshot_type: str, payload: Dict[str, Any]) -> None:
        with self.db._conn() as conn:
            conn.execute(
                "INSERT INTO market_snapshots (instrument, snapshot_type, payload) VALUES (?, ?, ?)",
                [instrument, snapshot_type, json.dumps(payload)],
            )

    def get_latest_market_snapshot(self, instrument: str, snapshot_type: str) -> Optional[Dict[str, Any]]:
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM market_snapshots WHERE instrument=? AND snapshot_type=? ORDER BY created_at DESC LIMIT 1",
                [instrument, snapshot_type],
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def get_market_snapshots(self, instrument: str, snapshot_type: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT payload, created_at FROM market_snapshots WHERE instrument=? AND snapshot_type=? ORDER BY created_at DESC LIMIT ?",
                [instrument, snapshot_type, limit],
            ).fetchall()
        return [json.loads(row[0]) | {"created_at": row[1]} for row in rows]
