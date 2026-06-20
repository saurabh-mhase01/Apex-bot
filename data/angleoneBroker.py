"""
ANGEL ONE — Production SmartAPI Broker Adapter (v2)

Features:
- Safe authentication + session handling
- Retry-safe API wrapper
- Persistent instrument cache
- WebSocket auto-reconnect
- Strict token validation
- Clean order system (paper + live)
- OHLCV + option chain + positions
"""

import logging
import uuid
import time
import pickle
import requests
import random
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Tuple

import pandas as pd
import pyotp

from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

logger = logging.getLogger("ANGELONE")
CACHE_FILE = Path("./angel_instrument_cache.pkl")

INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

INTERVAL_MAP = {
    "1minute": "ONE_MINUTE",
    "3minute": "THREE_MINUTE",
    "5minute": "FIVE_MINUTE",
    "15minute": "FIFTEEN_MINUTE",
    "30minute": "THIRTY_MINUTE",
    "1hour": "ONE_HOUR",
    "1day": "ONE_DAY",
}


# ─────────────────────────────────────────────────────────────
# BROKER
# ─────────────────────────────────────────────────────────────

class AngelOneBroker:

    def __init__(self,paper_trading: bool = True):

        self.api_key       = "JGMUJJ4n"
        self.client_id     = "S62103272"
        self.password      = "5763"
        self.totp_secret   = "OJOAT3LK5KW5M3U4LXJSHHZSDA"

        self.paper_trading = paper_trading

        self.smart_api = None
        self.auth_token = None
        self.refresh_token = None
        self.feed_token = None

        self._session_date = None
        self._order_counter = 1000

        self._instrument_cache = {}
        self._instrument_date = None

        self._authenticate()
        self._load_instrument_cache()

    # ─────────────────────────────────────────────
    # AUTH
    # ─────────────────────────────────────────────

    def _authenticate(self):
        try:
            totp = pyotp.TOTP(self.totp_secret).now()

            self.smart_api = SmartConnect(self.api_key)
            data = self.smart_api.generateSession(
                self.client_id,
                self.password,
                totp
            )

            if not data or not data.get("status"):
                raise Exception(f"Auth failed: {data}")

            self.auth_token = data["data"]["jwtToken"]
            self.refresh_token = data["data"]["refreshToken"]
            self.feed_token = self.smart_api.getfeedToken()

            self._session_date = date.today()

            logger.info("Angel One authenticated")

            return True

        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False

    def _ensure_session(self):
        if self._session_date != date.today():
            ok = self._authenticate()
            if not ok:
                raise RuntimeError("Session expired & re-login failed")

    # ─────────────────────────────────────────────
    # RETRY WRAPPER
    # ─────────────────────────────────────────────

    def _safe_call(self, fn, *args, retries=3, **kwargs):
        for i in range(retries):
            try:
                self._ensure_session()
                resp = fn(*args, **kwargs)
                if resp and resp.get("status"):
                    return resp
            except Exception as e:
                logger.warning(f"Retry {i+1}: {e}")
                time.sleep(0.3 + random.random())

        return None

    # ─────────────────────────────────────────────
    # INSTRUMENT CACHE
    # ─────────────────────────────────────────────

    def _load_instrument_cache(self):

        if CACHE_FILE.exists():
            try:
                data = pickle.load(open(CACHE_FILE, "rb"))
                if data["date"] == date.today():
                    self._instrument_cache = data["cache"]
                    self._instrument_date = data["date"]
                    return
            except Exception:
                pass

        self._download_cache()

    def _download_cache(self):
        logger.info("Downloading instrument master...")

        resp = requests.get(INSTRUMENT_URL, timeout=30)
        data = resp.json()

        cache = {}
        for inst in data:
            key1 = f"{inst.get('exch_seg')}:{inst.get('symbol')}"
            cache[key1] = inst
            cache[inst.get("symbol")] = inst

        self._instrument_cache = cache
        self._instrument_date = date.today()

        pickle.dump({
            "date": date.today(),
            "cache": cache
        }, open(CACHE_FILE, "wb"))

    def _get_token(self, symbol: str, exchange="NFO"):
        self._load_instrument_cache()

        key = f"{exchange}:{symbol}"
        inst = self._instrument_cache.get(key) or self._instrument_cache.get(symbol)

        if not inst:
            raise ValueError(f"Instrument not found: {symbol}")

        token = inst.get("token")
        if not token:
            raise ValueError(f"Token missing: {symbol}")

        return str(token)

    # ─────────────────────────────────────────────
    # INSTRUMENT RESOLVE
    # ─────────────────────────────────────────────

    def _resolve_instrument(self, key: str):

        mapping = {
            "NIFTY": ("NSE", "Nifty 50", "26000"),
            "BANKNIFTY": ("NSE", "Nifty Bank", "26009"),
            "FINNIFTY": ("NSE", "FINNIFTY", "26037"),
            "VIX": ("NSE", "India VIX", "13"),
        }

        if key in mapping:
            return mapping[key]

        token = self._get_token(key, "NFO")
        return "NFO", key, token

    # ─────────────────────────────────────────────
    # MARKET DATA
    # ─────────────────────────────────────────────

    def get_ltp(self, key: str):

        exchange, symbol, token = self._resolve_instrument(key)

        resp = self._safe_call(self.smart_api.ltpData, exchange, symbol, token)
        if not resp:
            return None

        return float(resp["data"]["ltp"])

    def get_ohlcv(self, key: str, interval="15minute", days=10):

        if interval not in INTERVAL_MAP:
            raise ValueError("Invalid interval")

        exchange, symbol, token = self._resolve_instrument(key)

        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": INTERVAL_MAP[interval],
            "fromdate": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "todate": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        resp = self._safe_call(self.smart_api.getCandleData, params)

        if not resp:
            return pd.DataFrame()

        df = pd.DataFrame(resp["data"], columns=[
            "time", "open", "high", "low", "close", "volume"
        ])

        df["time"] = pd.to_datetime(df["time"])
        return df.set_index("time")

    # ─────────────────────────────────────────────
    # ORDER SYSTEM
    # ─────────────────────────────────────────────

    def place_order(self, key, qty, side="BUY", order_type="MARKET"):

        if self.paper_trading:
            self._order_counter += 1
            return {"order_id": f"PAPER_{self._order_counter}"}

        exchange, symbol, token = self._resolve_instrument(key)

        params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(qty),
            "price": "0"
        }

        resp = self._safe_call(self.smart_api.placeOrder, params)

        if not resp:
            return {"status": "FAILED"}

        return {"status": "SUCCESS", "order_id": resp["data"]["orderid"]}

    # ─────────────────────────────────────────────
    # POSITION / FUNDS
    # ─────────────────────────────────────────────

    def get_positions(self):
        resp = self._safe_call(self.smart_api.position)
        return resp["data"] if resp else []

    def get_funds(self):
        resp = self._safe_call(self.smart_api.rmsLimit)
        return resp["data"] if resp else {}

    # ─────────────────────────────────────────────
    # WEB SOCKET (AUTO RECONNECT)
    # ─────────────────────────────────────────────

    def start_live_feed(self, tokens, callback):

        def run():
            while True:
                try:
                    ws = SmartWebSocketV2(
                        self.auth_token,
                        self.api_key,
                        self.client_id,
                        self.feed_token
                    )

                    def on_data(wsapp, msg):
                        callback(msg)

                    def on_open(wsapp):
                        ws.subscribe(str(uuid.uuid4()), 3, tokens)

                    ws.on_open = on_open
                    ws.on_data = on_data

                    ws.connect()

                except Exception as e:
                    logger.error(f"WS error: {e}")

                time.sleep(3)

        import threading
        threading.Thread(target=run, daemon=True).start()

    # ─────────────────────────────────────────────
    # CLEANUP
    # ─────────────────────────────────────────────

    def logout(self):
        try:
            self.smart_api.terminateSession(self.client_id)
        except Exception:
            pass