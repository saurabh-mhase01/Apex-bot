from datetime import time as dtime

from core.scheduler import build_clock_times


def test_build_clock_times_includes_11_50_for_five_minute_signals():
    times = build_clock_times(dtime(9, 15), dtime(15, 25), 5)
    assert "11:50" in times


def test_build_clock_times_includes_11_45_for_fifteen_minute_regime():
    times = build_clock_times(dtime(9, 15), dtime(15, 25), 15)
    assert "11:45" in times
