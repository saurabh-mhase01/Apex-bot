import sys
import threading
import time
import types
import unittest
from unittest import mock

from data.Angle_broker_v2 import AngelOneBroker


class _BlockingSocket:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def connect(self):
        time.sleep(0.2)

    def close_connection(self):
        pass


class StartLiveFeedTests(unittest.TestCase):
    def test_start_live_feed_returns_without_waiting_for_connect(self):
        fake_module = types.ModuleType("SmartApi.smartWebSocketV2")
        fake_module.SmartWebSocketV2 = _BlockingSocket
        with mock.patch.dict(sys.modules, {"SmartApi.smartWebSocketV2": fake_module}):
            broker = AngelOneBroker.__new__(AngelOneBroker)
            broker.auth_token = "auth"
            broker.feed_token = "feed"
            broker.api_key = "api"
            broker.client_id = "client"
            broker._normalize_live_message = lambda message: message
            broker.api_logger = type("Logger", (), {"log_call": lambda *args, **kwargs: None})()

            started = time.time()
            ws = broker.start_live_feed([{"exchangeType": 2, "tokens": ["1"]}])
            elapsed = time.time() - started

            self.assertIsNotNone(ws)
            self.assertLess(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
