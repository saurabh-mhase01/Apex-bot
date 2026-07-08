import threading

from main import OptionsBot


def test_start_bootstrap_runs_in_background_thread():
    bot = OptionsBot.__new__(OptionsBot)
    bot._shutdown_requested = False
    called = []

    def fake_bootstrap():
        called.append("bootstrapped")

    bot._bootstrap = fake_bootstrap
    thread = bot._start_bootstrap()
    thread.join(timeout=1)

    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert called == ["bootstrapped"]
