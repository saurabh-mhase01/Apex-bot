from datetime import datetime, timedelta

from data.Angle_broker_v2 import AngelOneBroker
from data.data_engine import DataEngine
from db.database import Database


def test_store_and_query_candles_incrementally(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    engine = DataEngine(db=db)

    ts1 = datetime(2024, 1, 1, 9, 30)
    ts2 = ts1 + timedelta(minutes=5)

    engine.save_candles(
        "NIFTY",
        "5minute",
        [
            {"timestamp": ts1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
            {"timestamp": ts2, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 1200},
        ],
    )

    df = engine.get_candles("NIFTY", "5minute", limit=10)
    assert len(df) == 2
    assert df.index[0] == ts1
    assert engine.get_last_candle_timestamp("NIFTY", "5minute") == ts2


def test_store_and_query_market_snapshots(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    engine = DataEngine(db=db)

    engine.save_market_snapshot("NIFTY", "vix", {"value": 15.2, "source": "live"})
    engine.save_market_snapshot("NIFTY", "vix", {"value": 15.6, "source": "live"})

    snapshot = engine.get_latest_market_snapshot("NIFTY", "vix")
    assert snapshot["value"] == 15.6
    assert snapshot["source"] == "live"


def test_normalize_live_quote_message():
    broker = AngelOneBroker.__new__(AngelOneBroker)
    message = {
        "token": "26000",
        "ltp": 19850.5,
        "open_interest": 125000,
        "bid": 19849.0,
        "ask": 19851.0,
    }

    normalized = broker._normalize_live_message(message)
    assert normalized["token"] == "26000"
    assert normalized["ltp"] == 19850.5
    assert normalized["oi"] == 125000
    assert normalized["bid"] == 19849.0
    assert normalized["ask"] == 19851.0


def test_get_candles_with_live_bar(tmp_path):
    db_path = tmp_path / "test_bot.db"
    db = Database(str(db_path))
    engine = DataEngine(db=db)

    ts1 = datetime(2024, 1, 1, 9, 30)
    ts2 = ts1 + timedelta(minutes=5)

    engine.save_candles(
        "NIFTY",
        "5minute",
        [{"timestamp": ts1, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}],
    )
    engine.update_live_bar("NIFTY", "5minute", 101.0, volume=10.0, timestamp=ts2)

    df = engine.get_candles_with_live_bar("NIFTY", "5minute", limit=10)
    assert len(df) == 2
    assert df.iloc[-1]["close"] == 101.0
