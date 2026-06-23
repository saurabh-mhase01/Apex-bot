"""
AI Options Buyer Bot — Main Entry Point
Nifty & Bank Nifty | Upstox API | Self-Learning
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time as dtime, timezone, timedelta
import schedule
import time
from pathlib import Path

from core.bot_engine import BotEngine
from core.config import Config
from db.database import Database
from alerts.telegram_alert import TelegramAlerter
from api.dashboard_api import start_api_server

import os
os.environ["TZ"] = "Asia/Kolkata"
time.tzset()

# ─── Logging Setup with IST ───────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG level for comprehensive logging
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ]
)



logger = logging.getLogger("MAIN")

# Suppress verbose library logs — keep only bot logs
logging.getLogger("urllib3").setLevel(logging.DEBUG)
logging.getLogger("requests").setLevel(logging.DEBUG)
logging.getLogger("asyncio").setLevel(logging.DEBUG)
logging.getLogger("schedule").setLevel(logging.DEBUG)

# Enable DEBUG for our modules
for module in ["MAIN", "ENGINE", "GROWW", "TELEGRAM"]:
    logging.getLogger(module).setLevel(logging.DEBUG)

class OptionsBot:
    def __init__(self):
        self.config = Config.load()
        self.db = Database(self.config.db_path)
        self.alerter = TelegramAlerter(self.config.telegram_token, self.config.telegram_chat_id)
        self.engine = BotEngine(self.config, self.db, self.alerter)
        self.running = False

    def setup_schedule(self):
        # Pre-market
        schedule.every().day.at("08:50").do(self.engine.pre_market_analysis)
        schedule.every().day.at("09:00").do(self.engine.compute_daily_regime)

        # Market hours — signal checks every 5 minutes
        for h in range(9, 15):
            for m in range(0, 60, 5):
                t = f"{h:02d}:{m:02d}"
                if t >= "09:15" and t <= "15:25":
                    schedule.every().day.at(t).do(self.engine.check_signals)

        # Intraday management
        schedule.every().day.at("13:00").do(self.engine.mid_day_review)
        schedule.every().day.at("15:00").do(self.engine.pre_close_review)
        schedule.every().day.at("15:25").do(self.engine.force_exit_all)

        # Post-market
        schedule.every().day.at("15:35").do(self.engine.post_market_log)
        schedule.every().sunday.at("21:00").do(self.engine.weekly_backtest_and_retrain)

        logger.info("✅ Schedule configured")

    def run(self):
        self.running = True
        logger.info("🚀 AI Options Bot starting...")
        self.alerter.send("🤖 Bot started and monitoring markets")

        # Start FastAPI dashboard in background thread
        import threading
        api_thread = threading.Thread(
            target=start_api_server,
            kwargs={
                "engine": self.engine,
                "db": self.db,
                "host": "0.0.0.0",
                "port": 8000
            },
            daemon=True
        )
        api_thread.start()

        now = datetime.now().time()
        market_open  = dtime(9, 15)
        market_close = dtime(15, 30)
        
        if market_open <= now <= market_close:
            logger.info("🌅 Starting mid-session — bootstrapping regime and VIX...")
    
            def _bootstrap():
                try:
                    self.engine.pre_market_analysis()   # seeds VIX in DB
                    self.engine.compute_daily_regime()  # sets active_regime / confidence
                    logger.info("✅ Bootstrap complete")
                except Exception as e:
                    logger.error(f"Bootstrap error: {e}")
    
            bootstrap_thread = threading.Thread(target=_bootstrap, daemon=True)
            bootstrap_thread.start()
        self.setup_schedule()

        def shutdown(sig, frame):
            logger.info("Shutdown signal received")
            self.engine.force_exit_all()
            self.running = False
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while self.running:
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    bot = OptionsBot()
    bot.run()
