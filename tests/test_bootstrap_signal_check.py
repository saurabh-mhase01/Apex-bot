import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import OptionsBot


def test_bootstrap_runs_signal_check_after_regime():
    bot = OptionsBot.__new__(OptionsBot)
    bot.engine = MagicMock()
    bot.engine.active_regime = "TRENDING_BULL"

    bot._bootstrap_and_run_once()

    bot.engine.pre_market_analysis.assert_called_once_with()
    bot.engine.compute_daily_regime.assert_called_once_with()
    bot.engine.check_signals.assert_called_once_with()
