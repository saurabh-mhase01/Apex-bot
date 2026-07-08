import sys
import types

from data.Angle_broker_v2 import AngelOneBroker


class FakeSocket:
    def __init__(self):
        self.on_data = None
        self.on_error = None
        self.on_close = None
        self.on_open = None
        self.connected = False

    def connect(self):
        self.connected = True


class FakeSmartSocketModule(types.SimpleNamespace):
    pass


def test_start_live_feed_overrides_internal_close_handler(monkeypatch):
    fake_socket = FakeSocket()

    class FakeSmartWebSocketV2:
        def __init__(self, *args, **kwargs):
            self._socket = fake_socket

        def connect(self):
            self._socket.connected = True

    fake_module = types.ModuleType("SmartApi.smartWebSocketV2")
    fake_module.SmartWebSocketV2 = FakeSmartWebSocketV2
    monkeypatch.setitem(sys.modules, "SmartApi.smartWebSocketV2", fake_module)

    broker = AngelOneBroker.__new__(AngelOneBroker)
    broker.auth_token = "token"
    broker.feed_token = "feed"
    broker.api_key = "api"
    broker.client_id = "client"
    broker.api_logger = types.SimpleNamespace(log_call=lambda *args, **kwargs: None)

    ws = broker.start_live_feed([{"tokens": ["1"]}], callback=None)

    assert ws is not None
    assert callable(ws._on_close)
    assert callable(ws._on_error)

    # The library calls the internal callback with extra positional args.
    ws._on_close(ws, "reason", "detail")
    ws._on_error(ws, "boom", "detail")
