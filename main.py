# """
# AI Options Buyer Bot — Main Entry Point
# Nifty & Bank Nifty | Angel One API | Self-Learning
# """

# import asyncio
# import logging
# import signal
# import sys
# import threading
# from datetime import datetime, time as dtime, timezone, timedelta
# import schedule
# import time
# from pathlib import Path

# from core.bot_engine import BotEngine
# from core.config import Config
# from core.regime_classifier import NO_DATA_REGIME
# from db.database import Database
# from alerts.telegram_alert import TelegramAlerter
# from api.dashboard_api import start_api_server

# import os
# os.environ["TZ"] = "Asia/Kolkata"
# time.tzset()

# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
#     datefmt="%Y-%m-%d %H:%M:%S",
#     handlers=[
#         logging.StreamHandler(sys.stdout),
#         logging.FileHandler("logs/bot.log"),
#     ]
# )

# logger = logging.getLogger("MAIN")

# # Enable DEBUG for our modules
# for module in ["MAIN", "ENGINE", "ANGELONE", "REGIME", "RISK", "STRATEGY", "DATABASE", "CONFIG", "TELEGRAM"]:
#     logging.getLogger(module).setLevel(logging.DEBUG)

# class OptionsBot:
#     def __init__(self):
#         self.config = Config.load()
#         self.db = Database(self.config.db_path)
#         self.alerter = TelegramAlerter(self.config.telegram_token, self.config.telegram_chat_id)
#         self.engine = BotEngine(self.config, self.db, self.alerter)
#         self.running = False

#     def _start_background_ingestion(self):
#         def _on_live_message(message):
#             try:
#                 token = message.get("token")
#                 if not token:
#                     return
#                 payload = {
#                     "token": token,
#                     "ltp": message.get("ltp"),
#                     "oi": message.get("oi"),
#                     "bid": message.get("bid"),
#                     "ask": message.get("ask"),
#                     "source": "ws",
#                 }
#                 self.engine.data_engine.save_market_snapshot("LIVE", f"quote:{token}", payload)
#                 if payload.get("ltp") is not None:
#                     self.engine.data_engine.update_live_bar("NIFTY", "5minute", float(payload["ltp"]), volume=0.0)
#                     self.engine.data_engine.maybe_flush_expired_bars("NIFTY", "5minute", datetime.now())
#             except Exception as exc:
#                 logger.warning(f"[MAIN] Live-feed persistence failed: {exc}")

#         def _loop():
#             while self.running:
#                 try:
#                     for instrument_key in self.config.instruments:
#                         self.engine.data_engine.ingest_from_broker(
#                             self.engine.broker,
#                             instrument_key,
#                             "5minute",
#                             from_dt=None,
#                             to_dt=datetime.now(),
#                         )
#                         self.engine.data_engine.ingest_from_broker(
#                             self.engine.broker,
#                             instrument_key,
#                             "15minute",
#                             from_dt=None,
#                             to_dt=datetime.now(),
#                         )
#                         self.engine.data_engine.ingest_from_broker(
#                             self.engine.broker,
#                             instrument_key,
#                             "30minute",
#                             from_dt=None,
#                             to_dt=datetime.now(),
#                         )
#                         try:
#                             vix = self.engine.broker.get_india_vix()
#                             if vix is not None:
#                                 self.engine.data_engine.save_market_snapshot(instrument_key, "vix", {"value": vix, "source": "live"})
#                         except Exception as exc:
#                             logger.warning(f"[MAIN] Background VIX ingest failed: {exc}")
#                     time.sleep(60)
#                 except Exception as exc:
#                     logger.error(f"[MAIN] Background ingestion loop error: {exc}", exc_info=True)
#                     time.sleep(30)

#         ingestion_thread = threading.Thread(target=_loop, daemon=True, name="background-ingestion")
#         ingestion_thread.start()

#         try:
#             self.engine.broker.start_live_feed(
#                 [{"exchangeType": 2, "tokens": ["26000", "26009", "26037"]}],
#                 callback=_on_live_message,
#             )
#         except Exception as exc:
#             logger.warning(f"[MAIN] Could not start websocket feed: {exc}")

#     def _bootstrap_and_run_once(self):
#         try:
#             self.engine.pre_market_analysis()
#             self.engine.compute_daily_regime()
#             if self.engine.active_regime == NO_DATA_REGIME:
#                 logger.warning(
#                     "[MAIN] Bootstrap finished but regime is still "
#                     f"{NO_DATA_REGIME} — check ANGELONE_* credentials and "
#                     "broker connectivity in the logs above. check_signals() "
#                     "will keep skipping until a real regime is computed."
#                 )
#             else:
#                 self.engine.check_signals()
#                 logger.info(f"[MAIN] Bootstrap complete — regime={self.engine.active_regime}")
#         except Exception as e:
#             logger.error(f"[MAIN] Bootstrap error: {e}", exc_info=True)

#     def setup_schedule(self):
#         schedule.every().day.at("08:50").do(self.engine.pre_market_analysis)
#         schedule.every().day.at("09:00").do(self.engine.compute_daily_regime)

#         # BUG FIX: compute_daily_regime() was previously scheduled ONCE per day
#         # (09:00 only). Between 09:00 and the next day, active_regime/regime_confidence
#         # never updated except on a process restart — so an intraday reversal (like
#         # the 07-Jul session: bullish morning, late-session selling flipping it
#         # bearish) never reached the strategy engine. It stayed pinned to whatever
#         # was computed at market open. Now re-run every 15 minutes through the
#         # session so the regime — and its confidence — stay current.
#         for h in range(9, 15):
#             for m in range(0, 60, 15):
#                 t = f"{h:02d}:{m:02d}"
#                 if "09:15" <= t <= "15:25":
#                     schedule.every().day.at(t).do(self.engine.compute_daily_regime)

#         for h in range(9, 15):
#             for m in range(0, 60, 5):
#                 t = f"{h:02d}:{m:02d}"
#                 if t >= "09:15" and t <= "15:25":
#                     schedule.every().day.at(t).do(self.engine.check_signals)

#         schedule.every().day.at("13:00").do(self.engine.mid_day_review)
#         schedule.every().day.at("15:00").do(self.engine.pre_close_review)
#         schedule.every().day.at("15:25").do(self.engine.force_exit_all)

#         schedule.every().day.at("15:35").do(self.engine.post_market_log)
#         schedule.every().sunday.at("21:00").do(self.engine.weekly_backtest_and_retrain)

#         logger.info("[MAIN]-> SCHEDULE configured (regime refresh every 15min, signals every 5min)")

#     def run(self):
#         self.running = True
#         logger.info("[MAIN] AI Options Bot starting...")
#         self.alerter.send("🤖 Bot started and monitoring markets")

#         import threading
#         api_thread = threading.Thread(
#             target=start_api_server,
#             kwargs={
#                 "engine": self.engine,
#                 "db": self.db,
#                 "host": "0.0.0.0",
#                 "port": 8000
#             },
#             daemon=True
#         )
#         api_thread.start()

#         now = datetime.now().time()
#         market_open  = dtime(9, 15)
#         market_close = dtime(15, 30)

#         if market_open <= now <= market_close:
#             logger.info("[MAIN] Starting mid-session — bootstrapping regime and VIX...")
#             bootstrap_thread = threading.Thread(target=self._bootstrap_and_run_once, daemon=True)
#             bootstrap_thread.start()
#         self.setup_schedule()
#         self._start_background_ingestion()

#         def shutdown(sig, frame):
#             logger.info("[MAIN] Shutdown signal received")
#             self.engine.force_exit_all()
#             self.running = False
#             sys.exit(0)

#         signal.signal(signal.SIGINT, shutdown)
#         signal.signal(signal.SIGTERM, shutdown)

#         while self.running:
#             schedule.run_pending()
#             time.sleep(1)


# if __name__ == "__main__":
#     Path("logs").mkdir(exist_ok=True)
#     bot = OptionsBot()
#     bot.run()

"""
AI Options Buyer Bot — Main Entry Point (single-loop architecture)

BUG FIX (architecture): previously ran THREE independent, uncoordinated
polling loops that each called the broker directly — a 60s background
ingestion thread, check_signals() via `schedule` every 5min, and
compute_daily_regime() via `schedule` every 15min. With no shared cache and
no global rate limit between them, they collided and caused AB1021
"Access denied because of exceeding access rate" errors.

Now there is ONE thread, ONE loop. Every broker call goes through
MarketDataService (shared hot-value cache + global token bucket). Each
periodic action tracks its own "next due" time and fires only when due —
no separate threads, no possibility of two paths hitting the broker for the
same data at the same moment.
"""
import logging
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

from core.bot_engine import BotEngine
from core.config import Config
from core.regime_classifier import NO_DATA_REGIME
from core.scheduler import build_clock_times
from data.market_data_service import MarketDataService
from data.data_engine import DataEngine
from data.Angle_broker_v2 import AngelOneBroker, INDEX_TOKENS
from db.database import Database
from alerts.telegram_alert import TelegramAlerter
from api.dashboard_api import start_api_server

import os
os.environ["TZ"] = "Asia/Kolkata"
time.tzset()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/bot.log")],
)
logger = logging.getLogger("MAIN")

for module in ["MAIN", "ENGINE", "ANGELONE", "MARKET_DATA_SERVICE", "REGIME", "RISK", "STRATEGY", "DATABASE", "CONFIG"]:
    logging.getLogger(module).setLevel(logging.DEBUG)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


# class Scheduled:
#     """A single periodic action that runs on the actual market clock."""
#     def __init__(self, name: str, times: list[str], fn, active_hours_only: bool = True):
#         self.name = name
#         self.times = times
#         self.fn = fn
#         self.active_hours_only = active_hours_only
#         self._last_run_date: str | None = None

#     def maybe_run(self, now_dt: datetime, market_open: bool):
#         if self.active_hours_only and not market_open:
#             return

#         current_day = now_dt.strftime("%Y-%m-%d")
#         current_time = now_dt.strftime("%H:%M")
#         if current_day == self._last_run_date and current_time not in self.times:
#             return
#         if current_time not in self.times:
#             self._last_run_date = None
#             return
#         if current_day == self._last_run_date:
#             return

#         self._last_run_date = current_day
#         try:
#             self.fn()
#         except Exception as e:
#             logger.error(f"[SCHEDULE] {self.name} raised: {e}", exc_info=True)

class Scheduled:
    """A single periodic action that runs on the actual market clock."""
    def __init__(self, name: str, times: list[str], fn, active_hours_only: bool = True):
        self.name = name
        self.times = times
        self.fn = fn
        self.active_hours_only = active_hours_only
        self._last_fired_slot: str | None = None  # "YYYY-MM-DD HH:MM" of last successful run

    def maybe_run(self, now_dt: datetime, market_open: bool):
        if self.active_hours_only and not market_open:
            return

        current_time = now_dt.strftime("%H:%M")
        if current_time not in self.times:
            return

        # BUG FIX: old version tracked only the DATE of the last run, so
        # `if current_day == self._last_run_date: return` blocked every
        # later time-of-day match for the rest of the session, not just
        # duplicate calls within the same minute. Confirmed in logs:
        # check_signals fired once at 10:20, then never again. Now keyed
        # on the exact (date, HH:MM) slot, so re-firing is only blocked
        # within the same minute across the ~5s poll loop.
        slot = now_dt.strftime("%Y-%m-%d %H:%M")
        if slot == self._last_fired_slot:
            return

        self._last_fired_slot = slot
        try:
            self.fn()
        except Exception as e:
            logger.error(f"[SCHEDULE] {self.name} raised: {e}", exc_info=True)


class OptionsBot:
    def __init__(self):
        self.config = Config.load()
        self.db = Database(self.config.db_path)
        self.alerter = TelegramAlerter(self.config.telegram_token, self.config.telegram_chat_id)
        self.data_engine = DataEngine(db=self.db)

        broker = AngelOneBroker(
            db_path=self.config.db_path,
            api_key=self.config.angleone_api_key or None,
            client_id=self.config.angleone_client_id or None,
            password=self.config.angleone_password or None,
            totp_secret=self.config.angleone_totp_secret or None,
        )
        # rate_per_sec: verify against your Angel One plan's documented limit
        # before running live — start conservative and raise only if you
        # confirm headroom in api_call_log.
        self.market_data = MarketDataService(broker, self.data_engine, rate_per_sec=3.0)
        self.engine = BotEngine(self.config, self.db, self.alerter, market_data=self.market_data)

        self.running = False
        self._shutdown_requested = False
        self._bootstrap_thread: threading.Thread | None = None
        self._daily_done = set()  # (job_name, date_str) → already ran today

        regime_times = build_clock_times(dtime(9, 15), dtime(15, 25), 15)
        signal_times = build_clock_times(dtime(9, 15), dtime(15, 25), 5)
        backfill_times = build_clock_times(dtime(9, 15), dtime(15, 25), 5)

        self._jobs = [
            Scheduled("backfill_candles", backfill_times, self._backfill_all, active_hours_only=True),
            Scheduled("compute_regime", regime_times, self.engine.compute_daily_regime),
            Scheduled("check_signals", signal_times, self.engine.check_signals),
            Scheduled("mid_day_review", ["13:00"], self._maybe_mid_day_review, active_hours_only=False),
            Scheduled("pre_close_review", ["15:00"], self._maybe_pre_close_review, active_hours_only=False),
            Scheduled("post_market_log", ["15:35"], self._maybe_post_market_log, active_hours_only=False),
        ]

    def _backfill_all(self):
        for instrument_key in self.config.instruments:
            for interval in ("5minute", "15minute", "30minute"):
                self.market_data.backfill(instrument_key, interval)

    def _once_daily_window(self, job_name: str, start: dtime, end: dtime, fn):
        now = datetime.now().time()
        key = (job_name, today_str())
        if start <= now <= end and key not in self._daily_done:
            fn()
            self._daily_done.add(key)

    def _maybe_mid_day_review(self):
        self._once_daily_window("mid_day", dtime(13, 0), dtime(13, 5), self.engine.mid_day_review)

    def _maybe_pre_close_review(self):
        self._once_daily_window("pre_close", dtime(15, 0), dtime(15, 5), self.engine.pre_close_review)

    def _maybe_post_market_log(self):
        self._once_daily_window("post_market", dtime(15, 35), dtime(15, 40), self.engine.post_market_log)

    def _bootstrap(self):
        logger.info("[MAIN] Bootstrap thread started")
        if self._shutdown_requested:
            logger.info("[MAIN] Bootstrap skipped because shutdown was requested")
            return
        try:
            self.engine.pre_market_analysis()
            if self._shutdown_requested:
                logger.info("[MAIN] Bootstrap interrupted before regime evaluation")
                return
            self.engine.compute_daily_regime()
            if self._shutdown_requested:
                logger.info("[MAIN] Bootstrap interrupted after regime evaluation")
                return
            if self.engine.active_regime == NO_DATA_REGIME:
                logger.warning(f"[MAIN] Bootstrap finished but regime is still {NO_DATA_REGIME} — check credentials/connectivity above")
            else:
                self.engine.check_signals()
                logger.info(f"[MAIN] Bootstrap complete — regime={self.engine.active_regime}")
        except KeyboardInterrupt:
            logger.info("[MAIN] Bootstrap interrupted by Ctrl+C")
            self._request_shutdown("bootstrap-interrupt")
        except Exception as e:
            logger.error(f"[MAIN] Bootstrap error: {e}", exc_info=True)

    def _start_bootstrap(self):
        thread = threading.Thread(target=self._bootstrap, daemon=False, name="bootstrap")
        self._bootstrap_thread = thread
        thread.start()
        return thread

    def _request_shutdown(self, reason: str = "signal"):
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self.running = False
        logger.info(f"[MAIN] Shutdown requested ({reason})")
        try:
            self.engine.force_exit_all()
        except Exception:
            pass
        try:
            self.market_data.stop()
        except Exception:
            pass

    def _install_signal_handlers(self):
        def shutdown(sig, frame):
            self._request_shutdown("signal")

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

    def run(self):
        self.running = True
        self._shutdown_requested = False
        logger.info("[MAIN] AI Options Bot starting (single-loop architecture)...")
        self.alerter.send("🤖 Bot started and monitoring markets")

        api_thread = threading.Thread(
            target=start_api_server,
            kwargs={"engine": self.engine, "db": self.db, "host": "0.0.0.0", "port": 8000},
            daemon=True,
        )
        api_thread.start()

        self._install_signal_handlers()

        self.market_data.start(index_tokens=[
            INDEX_TOKENS["NIFTY"]["ltp_token"],
            INDEX_TOKENS["BANKNIFTY"]["ltp_token"],
            INDEX_TOKENS["INDIAVIX"]["ltp_token"],
        ])

        self._start_bootstrap()

        last_force_exit_date = None
        while self.running:
            now_dt = datetime.now()
            now_t = now_dt.time()
            market_open = dtime(9, 15) <= now_t <= dtime(15, 30)

            if now_t >= dtime(15, 25) and last_force_exit_date != now_dt.date():
                self.engine.force_exit_all()
                last_force_exit_date = now_dt.date()

            for job in self._jobs:
                job.maybe_run(now_dt, market_open)

            try:
                time.sleep(5)
            except KeyboardInterrupt:
                logger.info("[MAIN] KeyboardInterrupt received in main loop")
                self._request_shutdown("keyboard-interrupt")
                break

        if self._bootstrap_thread and self._bootstrap_thread.is_alive():
            logger.info("[MAIN] Waiting for bootstrap work to finish before exiting")
            self._bootstrap_thread.join(timeout=20)
            if self._bootstrap_thread.is_alive():
                logger.warning("[MAIN] Bootstrap thread still running after shutdown timeout")


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    bot = OptionsBot()
    bot.run()