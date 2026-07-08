import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import data.Angle_broker_v2 as angle_broker


def test_get_option_expiries_handles_symbol_matching(monkeypatch):
    monkeypatch.setattr(
        angle_broker,
        "_INSTRUMENT_CACHE",
        {
            "nifty": {
                "exch_seg": "NFO",
                "instrumenttype": "OPTIDX",
                "symbol": "NIFTY07JUL2415000CE",
                "expiry": "07JUL2024",
            },
            "banknifty": {
                "exch_seg": "NFO",
                "instrumenttype": "OPTIDX",
                "symbol": "BANKNIFTY07JUL2415000CE",
                "expiry": "07JUL2024",
            },
        },
    )

    broker = angle_broker.AngelOneBroker.__new__(angle_broker.AngelOneBroker)
    expiries = broker.get_option_expiries("NIFTY")

    assert expiries == ["2024-07-07"]
