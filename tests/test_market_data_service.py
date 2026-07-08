import unittest
import pandas as pd

from data.market_data_service import MarketDataService


class DummyBroker:
    def __init__(self):
        self.calls = []

    def get_ohlcv(self, instrument_key, interval, days=30, use_db_fallback=True):
        self.calls.append((instrument_key, interval, days, use_db_fallback))
        return pd.DataFrame()


class DummyDataEngine:
    def get_candles_with_live_bar(self, *args, **kwargs):
        return pd.DataFrame()

    def save_candles(self, *args, **kwargs):
        return None


class MarketDataServiceTests(unittest.TestCase):
    def test_get_ohlcv_accepts_use_db_fallback_kwarg(self):
        broker = DummyBroker()
        service = MarketDataService(broker=broker, data_engine=DummyDataEngine(), rate_per_sec=1.0)

        service.get_ohlcv("TEST", "15minute", days=90, use_db_fallback=True)

        self.assertEqual(broker.calls[0][0], "TEST")
        self.assertEqual(broker.calls[0][1], "15minute")
        self.assertEqual(broker.calls[0][2], 90)
        self.assertTrue(broker.calls[0][3])


if __name__ == "__main__":
    unittest.main()
