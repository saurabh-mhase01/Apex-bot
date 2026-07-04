import logging
import math
import time
import json
import sqlite3
import threading
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import pandas as pd
import requests

logger = logging.getLogger("ANGELONE")

try:
    from SmartApi import SmartConnect
    import pyotp
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    logger.warning("SmartApi not installed. Run: pip install smartapi-python pyotp logzero")


# ════════════════════════════════════════════════════════════════════════════
# INDEX TOKENS
# ════════════════════════════════════════════════════════════════════════════

INDEX_TOKENS = {
    "NIFTY":     {"exchange": "NSE", "symbol": "Nifty 50",          "ltp_token": "26000",    "hist_token": "99926000"},
    "BANKNIFTY": {"exchange": "NSE", "symbol": "Nifty Bank",        "ltp_token": "26009",    "hist_token": "99926009"},
    "FINNIFTY":  {"exchange": "NSE", "symbol": "Nifty Fin Service", "ltp_token": "26037",    "hist_token": "99926037"},
    "INDIAVIX":  {"exchange": "NSE", "symbol": "India VIX",         "ltp_token": "99926017", "hist_token": "99926017"},
}

KEY_TO_UNDERLYING = {
    "NSE_INDEX|Nifty 50":          "NIFTY",
    "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
    "NSE_INDEX|Nifty Fin Service": "FINNIFTY",
    "NSE_INDEX|India VIX":         "INDIAVIX",
    "NIFTY":     "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY":  "FINNIFTY",
}

STRIKE_STEP = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
    "FINNIFTY":  50,
}

# NOTE: lot sizes are NO LONGER read from a hardcoded fallback table.
# get_lot_size() now returns None on a cache miss instead of guessing —
# a wrong lot size directly changes real position size / capital at risk.
# This constant is kept only for reference/logging, it is not used to size trades.
LOT_SIZE_REFERENCE_ONLY = {
    "NIFTY":     65,
    "BANKNIFTY": 30,
    "FINNIFTY":  60,
}

ANGEL_BASE     = "https://apiconnect.angelone.in"
INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

EXCHANGE_NSE = "NSE"
EXCHANGE_NFO = "NFO"
PRODUCT_INTRADAY     = "INTRADAY"
ORDER_MARKET         = "MARKET"
ORDER_LIMIT          = "LIMIT"
ORDER_STOPLOSS_LIMIT = "STOPLOSS_LIMIT"
VARIETY_NORMAL   = "NORMAL"
VARIETY_STOPLOSS = "STOPLOSS"
DURATION_DAY = "DAY"

INTERVAL_MAP = {
    "1minute":   "ONE_MINUTE",    "3minute":  "THREE_MINUTE",
    "5minute":   "FIVE_MINUTE",   "10minute": "TEN_MINUTE",
    "15minute":  "FIFTEEN_MINUTE","30minute": "THIRTY_MINUTE",
    "1hour":     "ONE_HOUR",      "1day":     "ONE_DAY",
}

_INSTRUMENT_CACHE: Dict[str, Dict] = {}
_INSTRUMENT_CACHE_DATE: Optional[date] = None


# ════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, min_interval_sec: float = 1.2):
        self.min_interval = min_interval_sec
        self._last_call: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, bucket: str = "default"):
        with self._lock:
            now     = time.time()
            last    = self._last_call.get(bucket, 0)
            elapsed = now - last
            if elapsed < self.min_interval:
                sleep_for = self.min_interval - elapsed
                logger.debug(f"⏳ Rate limit: sleeping {sleep_for:.2f}s before [{bucket}]")
                time.sleep(sleep_for)
            self._last_call[bucket] = time.time()


def with_backoff(fn, *args, max_retries: int = 4, base_delay: float = 2.0, **kwargs):
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            err_str  = str(e).lower()
            is_rate  = "exceeding access rate" in err_str or "403" in err_str or "access denied" in err_str
            if is_rate and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"🔁 Rate limited (attempt {attempt+1}/{max_retries}) — backing off {delay:.1f}s")
                time.sleep(delay)
                continue
            raise
    raise last_exc


# ════════════════════════════════════════════════════════════════════════════
# API CALL LOGGER
# ════════════════════════════════════════════════════════════════════════════

class ApiCallLogger:
    def __init__(self, db_path: str = "data/bot.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_table()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, endpoint TEXT,
                    request_params TEXT, response_status TEXT,
                    response_data TEXT, candle_count INTEGER,
                    http_status INTEGER, error_message TEXT, latency_ms INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_key TEXT, symboltoken TEXT,
                    interval TEXT, timestamp TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    fetched_at TEXT,
                    UNIQUE(instrument_key, interval, timestamp)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_apilog_ts   ON api_call_log(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_key ON candles(instrument_key, interval)")

    def log_call(self, endpoint: str, params: dict, response: dict,
                 http_status: int = 200, latency_ms: int = 0, error: str = None):
        candle_count = 0
        if response and isinstance(response.get("data"), list):
            candle_count = len(response["data"])
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO api_call_log
                    (timestamp, endpoint, request_params, response_status,
                     response_data, candle_count, http_status, error_message, latency_ms)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, [
                    datetime.now().isoformat(), endpoint,
                    json.dumps(params, default=str),
                    str(response.get("status")) if response else "EXCEPTION",
                    json.dumps(response, default=str)[:5000] if response else None,
                    candle_count, http_status, error, latency_ms,
                ])
        except Exception as e:
            logger.error(f"Failed to log API call: {e}")

    def save_candles(self, instrument_key: str, symboltoken: str,
                     interval: str, candles: List[list]) -> int:
        if not candles:
            return 0
        saved = 0
        try:
            with self._conn() as conn:
                for c in candles:
                    try:
                        conn.execute("""
                            INSERT OR REPLACE INTO candles
                            (instrument_key, symboltoken, interval, timestamp,
                             open, high, low, close, volume, fetched_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, [
                            instrument_key, symboltoken, interval, c[0],
                            float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]),
                            datetime.now().isoformat()
                        ])
                        saved += 1
                    except Exception as e:
                        logger.error(f"Candle save error: {e}")
            return saved
        except Exception as e:
            logger.error(f"Bulk candle save failed: {e}")
            return saved

    def get_recent_failures(self, limit: int = 20) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT timestamp, endpoint, request_params, candle_count,
                       http_status, error_message
                FROM api_call_log
                WHERE candle_count = 0 OR http_status != 200
                ORDER BY id DESC LIMIT ?
            """, [limit]).fetchall()
            return [dict(r) for r in rows]

    def get_cached_candles(self, instrument_key: str, interval: str,
                           days: int = 30) -> pd.DataFrame:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT timestamp, open, high, low, close, volume
                FROM candles
                WHERE instrument_key=? AND interval=? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, [instrument_key, interval, cutoff]).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        return df


# ════════════════════════════════════════════════════════════════════════════
# LIVE GREEKS CACHE
# ════════════════════════════════════════════════════════════════════════════

class GreeksCache:
    def __init__(self):
        self._data: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def update(self, token: str, greeks: Dict):
        with self._lock:
            self._data[token] = {**self._data.get(token, {}), **greeks}

    def get(self, token: str) -> Optional[Dict]:
        with self._lock:
            return self._data.get(token)

    def clear(self):
        with self._lock:
            self._data.clear()


_greeks_cache = GreeksCache()


class GreeksFeedSubscriber:
    """
    Angel One SmartAPI WebSocket feed in OPTION_GREEK mode.
    Reference: https://smartapi.angelbroking.com/docs/WebSocket2
    """

    def __init__(self, feed_token: str, api_key: str, client_id: str,
                 cache: GreeksCache):
        self.feed_token = feed_token
        self.api_key    = api_key
        self.client_id  = client_id
        self.cache      = cache
        self._ws        = None
        self._subscribed_tokens: List[str] = []

    def _on_data(self, wsapp, message):
        try:
            if not isinstance(message, dict):
                return
            token = str(message.get("token", ""))
            if not token:
                return
            greeks = {}
            if message.get("delta")  is not None: greeks["delta"] = float(message["delta"])
            if message.get("theta")  is not None: greeks["theta"] = float(message["theta"])
            if message.get("gamma")  is not None: greeks["gamma"] = float(message["gamma"])
            if message.get("vega")   is not None: greeks["vega"]  = float(message["vega"])
            if message.get("implied_volatility") is not None:
                greeks["iv"] = float(message["implied_volatility"])
            if message.get("ltp") is not None:
                greeks["ltp"] = float(message["ltp"])
            if message.get("open_interest") is not None:
                greeks["oi"] = int(message["open_interest"])
            if greeks:
                self.cache.update(token, greeks)
        except Exception as e:
            logger.debug(f"[WS] Greeks parse error: {e}")

    def subscribe(self, tokens: List[str]):
        self._subscribed_tokens = tokens
        logger.info(f"[WS] Greeks feed: {len(tokens)} tokens queued for subscription")
        # ── Uncomment to activate WebSocket Greeks feed ──────────────────────
        # from SmartApi.SmartWebSocketV2 import SmartWebSocketV2
        # self._ws = SmartWebSocketV2(
        #     auth_token=self.feed_token, api_key=self.api_key,
        #     client_code=self.client_id, feed_token=self.feed_token,
        # )
        # token_list = [{"exchangeType": 2, "tokens": tokens}]
        # self._ws.subscribe("greeks_sub", mode=4, token_list=token_list)
        # self._ws.on_data = self._on_data
        # threading.Thread(target=self._ws.connect, daemon=True).start()
        # ─────────────────────────────────────────────────────────────────────

    def stop(self):
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
# MAIN BROKER CLASS
# ════════════════════════════════════════════════════════════════════════════

class AngelOneBroker:
    """
    Angel One SmartAPI adapter.

    IMPORTANT — credentials:
    This class previously had api_key/client_id/password/totp_secret hardcoded
    as literals. That is a serious security problem if this file is ever
    committed to source control or shared (as happened in this chat) — anyone
    with those values can log into your live trading account. They have now
    been moved to read from Config/environment variables. ROTATE those
    credentials (change your Angel One password and regenerate the TOTP
    secret / API key) since the old ones were pasted in plaintext.
    """

    def __init__(self, db_path: str = "data/bot.db",
                 api_key: Optional[str] = None,
                 client_id: Optional[str] = None,
                 password: Optional[str] = None,
                 totp_secret: Optional[str] = None):
        import os
        self.api_key       = api_key       or os.getenv("ANGELONE_API_KEY", "")
        self.client_id     = client_id     or os.getenv("ANGELONE_CLIENT_ID", "")
        self.password      = password      or os.getenv("ANGELONE_PASSWORD", "")
        self.totp_secret   = totp_secret   or os.getenv("ANGELONE_TOTP_SECRET", "")
        self.paper_trading = True

        if not all([self.api_key, self.client_id, self.password, self.totp_secret]):
            logger.error(
                "[BROKER_INIT] Missing one or more Angel One credentials "
                "(api_key/client_id/password/totp_secret). Set them via env vars "
                "ANGELONE_API_KEY / ANGELONE_CLIENT_ID / ANGELONE_PASSWORD / ANGELONE_TOTP_SECRET."
            )

        self.smart_api      = None
        self.auth_token     = None
        self.refresh_token  = None
        self.feed_token     = None
        self._order_counter = 2000
        self._session_date  = None

        self.rate_limiter = RateLimiter(min_interval_sec=1.2)
        self.api_logger   = ApiCallLogger(db_path)

        self.greeks_feed: Optional[GreeksFeedSubscriber] = None

        if not SMARTAPI_AVAILABLE:
            logger.error("Install SmartApi: pip install smartapi-python pyotp logzero")
            return

        self._authenticate()
        self._load_instrument_cache()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        logger.info(f"[AUTH] INPUT: client_id={self.client_id}")
        try:
            totp = pyotp.TOTP(self.totp_secret).now()
            self.smart_api = SmartConnect(self.api_key)
            data = self.smart_api.generateSession(self.client_id, self.password, totp)
            if not data or data.get("status") is False:
                logger.error(f"[AUTH] OUTPUT: FAILED — {data}")
                return False
            self.auth_token    = data["data"]["jwtToken"]
            self.refresh_token = data["data"]["refreshToken"]
            self.feed_token    = self.smart_api.getfeedToken()
            self._session_date = date.today()
            logger.info(f"[AUTH] OUTPUT: ✅ authenticated | Client: {self.client_id}")
            return True
        except Exception as e:
            logger.error(f"[AUTH] EXCEPTION: {e}")
            return False

    def _ensure_session(self):
        if self._session_date != date.today():
            logger.info("[AUTH] Session expired — re-authenticating...")
            self._authenticate()

    # ── Instrument Cache ──────────────────────────────────────────────────────

    def _load_instrument_cache(self):
        global _INSTRUMENT_CACHE, _INSTRUMENT_CACHE_DATE
        if _INSTRUMENT_CACHE_DATE == date.today() and _INSTRUMENT_CACHE:
            return
        try:
            logger.info("[INSTRUMENT_CACHE] Loading Angel One instrument master...")
            resp        = requests.get(INSTRUMENT_URL, timeout=30)
            instruments = resp.json()
            _INSTRUMENT_CACHE = {}
            for inst in instruments:
                key = f"{inst.get('exch_seg', '')}:{inst.get('symbol', '')}"
                _INSTRUMENT_CACHE[key] = inst
                _INSTRUMENT_CACHE[inst.get("symbol", "")] = inst
            _INSTRUMENT_CACHE_DATE = date.today()
            logger.info(f"[INSTRUMENT_CACHE] OUTPUT: loaded {len(instruments):,} instruments")
        except Exception as e:
            logger.error(f"[INSTRUMENT_CACHE] ERROR: {e}")

    def _get_token(self, trading_symbol: str, exchange: str = "NFO") -> Optional[str]:
        self._load_instrument_cache()
        key  = f"{exchange}:{trading_symbol}"
        inst = _INSTRUMENT_CACHE.get(key) or _INSTRUMENT_CACHE.get(trading_symbol)
        token = str(inst.get("token", "")) if inst else None
        logger.debug(f"[GET_TOKEN] INPUT: symbol={trading_symbol}, exchange={exchange} → OUTPUT: token={token}")
        return token

    def _get_inst(self, trading_symbol: str, exchange: str = "NFO") -> Optional[Dict]:
        self._load_instrument_cache()
        key = f"{exchange}:{trading_symbol}"
        return _INSTRUMENT_CACHE.get(key) or _INSTRUMENT_CACHE.get(trading_symbol)

    def _search_option_token(self, underlying: str, strike: int,
                             option_type: str, expiry_str: str) -> Tuple[Optional[str], Optional[str]]:
        logger.debug(f"[SEARCH_OPT_TOKEN] INPUT: underlying={underlying}, strike={strike}, "
                     f"option_type={option_type}, expiry={expiry_str}")
        try:
            dt     = datetime.strptime(expiry_str, "%Y-%m-%d")
            mon    = dt.strftime("%b").upper()
            yy     = dt.strftime("%y")
            dd     = dt.strftime("%d")
            symbol = f"{underlying}{dd}{mon}{yy}{strike}{option_type}"
            token  = self._get_token(symbol, "NFO")
            logger.debug(f"[SEARCH_OPT_TOKEN] OUTPUT: symbol={symbol}, token={token}")
            if token:
                return symbol, token
            return None, None
        except Exception as e:
            logger.error(f"[SEARCH_OPT_TOKEN] ERROR: {e}")
            return None, None

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        logger.info(f"[GET_LTP] INPUT: instrument_key={instrument_key}")
        self._ensure_session()
        exchange, symbol, token = self._resolve_for_ltp(instrument_key)
        if not token:
            logger.error(f"[GET_LTP] OUTPUT: None (no LTP token resolved for {instrument_key})")
            return None
        self.rate_limiter.wait("ltp")
        t0 = time.time()
        try:
            resp    = with_backoff(self.smart_api.ltpData, exchange, symbol, token)
            latency = int((time.time() - t0) * 1000)
            self.api_logger.log_call("ltpData",
                {"exchange": exchange, "symbol": symbol, "token": token},
                resp, latency_ms=latency)
            if resp and resp.get("status"):
                ltp = float(resp["data"].get("ltp", 0)) or None
                logger.info(f"[GET_LTP] OUTPUT: {instrument_key} = {ltp}")
                return ltp
            logger.warning(f"[GET_LTP] OUTPUT: None (status=False for {instrument_key}: {resp})")
            return None
        except Exception as e:
            logger.error(f"[GET_LTP] ERROR [{instrument_key}]: {e}")
            return None

    def get_ohlcv(self, instrument_key: str, interval: str = "30minute",
              days: int = 30, use_db_fallback: bool = True) -> pd.DataFrame:
        logger.info(f"[GET_OHLCV] INPUT: instrument_key={instrument_key}, interval={interval}, "
                    f"days={days}, use_db_fallback={use_db_fallback}")

        self._ensure_session()

        exchange, symboltoken = self._resolve_for_historical(instrument_key)
        if not symboltoken:
            logger.error(f"[GET_OHLCV] No historical token for {instrument_key}")
            if use_db_fallback:
                cached = self.api_logger.get_cached_candles(instrument_key, interval, days)
                logger.warning(f"[GET_OHLCV] OUTPUT: DB fallback returned {len(cached)} candles")
                return cached
            logger.warning("[GET_OHLCV] OUTPUT: empty DataFrame (no token, no fallback)")
            return pd.DataFrame()

        angel_interval = INTERVAL_MAP.get(interval, "THIRTY_MINUTE")

        def _fetch_window(days_back: int):
            from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M")
            to_date   = datetime.now().strftime("%Y-%m-%d %H:%M")
            params = {
                "exchange": exchange, "symboltoken": symboltoken,
                "interval": angel_interval, "fromdate": from_date, "todate": to_date,
            }
            logger.info(f"[GET_OHLCV] Candle request → {params}")
            self.rate_limiter.wait("historical")
            t0 = time.time()
            resp = with_backoff(self.smart_api.getCandleData, params)
            latency = int((time.time() - t0) * 1000)
            self.api_logger.log_call("getCandleData", params, resp, latency_ms=latency)
            return resp, params

        resp, params = _fetch_window(days)
        candles = (resp or {}).get("data", []) if resp else []

        if len(candles) < 20:
            logger.warning(f"[GET_OHLCV] Only {len(candles)} candles received — expanding window to {days*2} days...")
            resp, params = _fetch_window(days * 2)
            candles = (resp or {}).get("data", []) if resp else []

        if not resp or not resp.get("status") or not candles:
            logger.error(f"[GET_OHLCV] Candle FAILED or empty response: {resp}")
            if use_db_fallback:
                cached = self.api_logger.get_cached_candles(instrument_key, interval, days)
                if not cached.empty:
                    logger.warning(f"[GET_OHLCV] OUTPUT: DB fallback returned {len(cached)} candles")
                    return cached
            logger.warning("[GET_OHLCV] OUTPUT: empty DataFrame")
            return pd.DataFrame()

        if len(candles) < 20:
            logger.error(
                f"[GET_OHLCV] OUTPUT: empty DataFrame — insufficient candles ({len(candles)}) "
                f"for {instrument_key} ({interval}) even after expanding window"
            )
            return pd.DataFrame()

        saved = self.api_logger.save_candles(instrument_key, symboltoken, interval, candles)
        logger.info(f"[GET_OHLCV] Got {len(candles)} candles, saved {saved} to DB")

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df = df.sort_index()
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)

        logger.info(f"[GET_OHLCV] OUTPUT: {len(df)} candles for {instrument_key} "
                    f"[{df.index.min()} → {df.index.max()}]")
        return df

    def get_india_vix(self) -> Optional[float]:
        logger.info("[GET_INDIA_VIX] INPUT: (none)")
        vix = self.get_ltp("NSE_INDEX|India VIX")
        logger.info(f"[GET_INDIA_VIX] OUTPUT: {vix}")
        return vix

    def get_funds(self) -> Dict:
        self._ensure_session()
        self.rate_limiter.wait("funds")
        try:
            resp = with_backoff(self.smart_api.rmsLimit)
            if resp and resp.get("status"):
                out = resp.get("data", {})
                logger.info(f"[GET_FUNDS] OUTPUT: {out}")
                return out
        except Exception as e:
            logger.error(f"[GET_FUNDS] ERROR: {e}")
        return {}

    def get_positions(self) -> List[Dict]:
        self._ensure_session()
        self.rate_limiter.wait("positions")
        try:
            resp = with_backoff(self.smart_api.position)
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception as e:
            logger.error(f"[GET_POSITIONS] ERROR: {e}")
        return []

    def get_order_book(self) -> List[Dict]:
        self._ensure_session()
        self.rate_limiter.wait("orderbook")
        try:
            resp = with_backoff(self.smart_api.orderBook)
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception:
            return []

    def get_profile(self) -> Dict:
        self._ensure_session()
        try:
            resp = self.smart_api.getProfile(self.refresh_token)
            if resp and resp.get("status"):
                return resp.get("data", {})
        except Exception:
            pass
        return {}

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(self, instrument_key: str, qty: int,
                    order_type: str = "MARKET", price: float = 0,
                    transaction_type: str = "BUY") -> Dict:
        logger.info(f"[PLACE_ORDER] INPUT: instrument={instrument_key}, qty={qty}, "
                    f"order_type={order_type}, price={price}, transaction_type={transaction_type}, "
                    f"paper_trading={self.paper_trading}")
        if self.paper_trading:
            out = self._paper_order(instrument_key, qty, transaction_type, price)
            logger.info(f"[PLACE_ORDER] OUTPUT: {out}")
            return out
        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_for_ltp(instrument_key)
            if not token:
                token = self._get_token(instrument_key, "NFO")
                exchange, symbol = "NFO", instrument_key
            if not token:
                logger.error(f"[PLACE_ORDER] OUTPUT: token not found for {instrument_key}")
                return {"error": f"Token not found for {instrument_key}"}
            order_params = {
                "variety":         VARIETY_NORMAL,
                "tradingsymbol":   symbol,
                "symboltoken":     token,
                "transactiontype": transaction_type,
                "exchange":        exchange,
                "ordertype":       ORDER_MARKET if order_type == "MARKET" else ORDER_LIMIT,
                "producttype":     PRODUCT_INTRADAY,
                "duration":        DURATION_DAY,
                "price":           str(price) if order_type == "LIMIT" else "0",
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(qty),
            }
            self.rate_limiter.wait("orders")
            resp = with_backoff(self.smart_api.placeOrder, order_params)
            self.api_logger.log_call("placeOrder", order_params,
                resp if isinstance(resp, dict) else {"raw": resp})
            if resp and isinstance(resp, dict) and resp.get("status"):
                oid = resp.get("data", {}).get("orderid", "")
                out = {"order_id": oid, "status": "complete"}
                logger.info(f"[PLACE_ORDER] OUTPUT: {out}")
                return out
            logger.error(f"[PLACE_ORDER] OUTPUT: error response {resp}")
            return {"error": str(resp)}
        except Exception as e:
            logger.error(f"[PLACE_ORDER] EXCEPTION: {e}")
            return {"error": str(e)}

    def place_sl_order(self, instrument_key: str, qty: int,
                       trigger_price: float, price: float) -> Dict:
        logger.info(f"[PLACE_SL_ORDER] INPUT: instrument={instrument_key}, qty={qty}, "
                    f"trigger_price={trigger_price}, price={price}")
        if self.paper_trading:
            out = {"order_id": f"PAPER_SL_{self._order_counter}"}
            logger.info(f"[PLACE_SL_ORDER] OUTPUT: {out}")
            return out
        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_for_ltp(instrument_key)
            if not token:
                token = self._get_token(instrument_key, "NFO")
                exchange, symbol = "NFO", instrument_key
            order_params = {
                "variety":         VARIETY_STOPLOSS,
                "tradingsymbol":   symbol,
                "symboltoken":     token,
                "transactiontype": "SELL",
                "exchange":        exchange,
                "ordertype":       ORDER_STOPLOSS_LIMIT,
                "producttype":     PRODUCT_INTRADAY,
                "duration":        DURATION_DAY,
                "price":           str(price),
                "triggerprice":    str(trigger_price),
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(qty),
            }
            self.rate_limiter.wait("orders")
            resp = with_backoff(self.smart_api.placeOrder, order_params)
            if resp and resp.get("status"):
                out = {"order_id": resp["data"].get("orderid", "")}
                logger.info(f"[PLACE_SL_ORDER] OUTPUT: {out}")
                return out
            logger.error(f"[PLACE_SL_ORDER] OUTPUT: error {resp}")
            return {"error": str(resp)}
        except Exception as e:
            logger.error(f"[PLACE_SL_ORDER] EXCEPTION: {e}")
            return {"error": str(e)}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        logger.info(f"[CANCEL_ORDER] INPUT: order_id={order_id}, variety={variety}")
        if self.paper_trading:
            return True
        self._ensure_session()
        try:
            resp = with_backoff(self.smart_api.cancelOrder, order_id, variety)
            ok = bool(resp and resp.get("status"))
            logger.info(f"[CANCEL_ORDER] OUTPUT: {ok}")
            return ok
        except Exception as e:
            logger.error(f"[CANCEL_ORDER] ERROR: {e}")
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def get_atm_strike(self, instrument_key: str) -> Optional[int]:
        logger.info(f"[GET_ATM_STRIKE] INPUT: instrument_key={instrument_key}")
        underlying = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
        step       = STRIKE_STEP.get(underlying, 50)
        ltp        = self.get_ltp(instrument_key)
        if not ltp:
            logger.error(f"[GET_ATM_STRIKE] OUTPUT: None (no live LTP for {instrument_key})")
            return None
        atm = round(ltp / step) * step
        logger.info(f"[GET_ATM_STRIKE] OUTPUT: {atm} (ltp={ltp}, step={step})")
        return atm

    def get_lot_size(self, instrument_key: str) -> Optional[int]:
        """
        Reads lot size from the live instrument cache ONLY. Returns None on a
        cache miss instead of guessing via a hardcoded table — callers
        (BotEngine._execute_trade / RiskGuard.position_size) MUST treat None
        as "abort this trade", since a wrong lot size directly changes real
        position size and capital at risk.
        """
        logger.info(f"[GET_LOT_SIZE] INPUT: instrument_key={instrument_key}")
        underlying = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
        self._load_instrument_cache()

        for _key, inst in _INSTRUMENT_CACHE.items():
            if inst.get("exch_seg") != "NFO":
                continue
            if inst.get("instrumenttype") not in ("OPTIDX", "OPTSTK"):
                continue
            if underlying not in inst.get("symbol", ""):
                continue
            try:
                lotsize = int(inst.get("lotsize", 0))
                if lotsize > 0:
                    logger.info(f"[GET_LOT_SIZE] OUTPUT: {lotsize} (from live API cache, underlying={underlying})")
                    return lotsize
            except (ValueError, TypeError):
                continue

        logger.error(
            f"[GET_LOT_SIZE] OUTPUT: None — cache miss for {underlying}. "
            f"NOT using a hardcoded fallback (reference only: {LOT_SIZE_REFERENCE_ONLY.get(underlying)}). "
            f"Caller must abort this trade."
        )
        return None

    def get_strike_step(self, instrument_key: str) -> int:
        underlying = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
        step = STRIKE_STEP.get(underlying, 50)
        logger.debug(f"[GET_STRIKE_STEP] INPUT: {instrument_key} → OUTPUT: {step}")
        return step

    def find_option_instrument(self, underlying_key: str, strike: int,
                               option_type: str, expiry: str) -> Optional[str]:
        logger.info(f"[FIND_OPTION_INSTRUMENT] INPUT: underlying_key={underlying_key}, "
                    f"strike={strike}, option_type={option_type}, expiry={expiry}")
        underlying = KEY_TO_UNDERLYING.get(underlying_key, underlying_key)
        symbol, _  = self._search_option_token(underlying, strike, option_type, expiry)
        logger.info(f"[FIND_OPTION_INSTRUMENT] OUTPUT: {symbol}")
        return symbol

    def get_option_expiries(self, instrument_key: str) -> List[str]:
        logger.info(f"[GET_OPTION_EXPIRIES] INPUT: instrument_key={instrument_key}")
        try:
            underlying = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
            expiry_set = set()
            for _key, inst in _INSTRUMENT_CACHE.items():
                if inst.get("exch_seg") != "NFO":
                    continue
                if inst.get("instrumenttype") not in ("OPTIDX", "OPTSTK"):
                    continue
                if underlying not in inst.get("symbol", ""):
                    continue
                exp_raw = inst.get("expiry", "")
                if not exp_raw:
                    continue
                try:
                    dt = datetime.strptime(exp_raw.upper(), "%d%b%Y")
                    expiry_set.add(dt.strftime("%Y-%m-%d"))
                except ValueError:
                    pass
            expiries = sorted(expiry_set)
            logger.info(f"[GET_OPTION_EXPIRIES] OUTPUT: {len(expiries)} expiries, next={expiries[0] if expiries else None}")
            return expiries
        except Exception as e:
            logger.error(f"[GET_OPTION_EXPIRIES] ERROR: {e}")
            return []

    # ── Token resolvers ───────────────────────────────────────────────────────

    def _resolve_for_ltp(self, key: str) -> Tuple[str, str, Optional[str]]:
        underlying = KEY_TO_UNDERLYING.get(key)
        if underlying and underlying in INDEX_TOKENS:
            info = INDEX_TOKENS[underlying]
            return info["exchange"], info["symbol"], info["ltp_token"]
        token = self._get_token(key, "NFO")
        return "NFO", key, token

    def _resolve_for_historical(self, key: str) -> Tuple[str, Optional[str]]:
        underlying = KEY_TO_UNDERLYING.get(key)
        if underlying and underlying in INDEX_TOKENS:
            info = INDEX_TOKENS[underlying]
            return info["exchange"], info["hist_token"]
        token = self._get_token(key, "NFO")
        return "NFO", token

    def _paper_order(self, symbol: str, qty: int, tx_type: str, price: float) -> Dict:
        self._order_counter += 1
        oid = f"PAPER_AO_{self._order_counter}"
        logger.info(f"📝 PAPER ORDER: {tx_type} {qty} {symbol} @ ₹{price} → {oid}")
        return {"order_id": oid, "status": "complete"}

    def _format_expiry_for_cache(self, expiry_yyyy_mm_dd: str) -> str:
        try:
            dt = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d")
            return dt.strftime("%d%b%Y").upper()
        except Exception as e:
            logger.warning(f"[FORMAT_EXPIRY] Failed to parse '{expiry_yyyy_mm_dd}': {e}")
            return expiry_yyyy_mm_dd

    def logout(self):
        try:
            self.smart_api.terminateSession(self.client_id)
            logger.info("[LOGOUT] session terminated")
        except Exception:
            pass

    # ── Option Chain ──────────────────────────────────────────────────────────

    def get_option_chain(self, instrument_key: str, expiry: str) -> Tuple[list, int]:
        logger.info(f"[GET_OPTION_CHAIN] INPUT: instrument_key={instrument_key}, expiry={expiry}")

        self._ensure_session()
        self._load_instrument_cache()

        underlying = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
        step       = STRIKE_STEP.get(underlying, 50)

        try:
            dt = datetime.strptime(expiry, "%Y-%m-%d")
            expiry_formats = [
                dt.strftime("%d%b%y").upper(),
                dt.strftime("%d%b%Y").upper(),
            ]
        except Exception:
            expiry_formats = [expiry]

        logger.info(f"[GET_OPTION_CHAIN] underlying={underlying} expiry_formats={expiry_formats}")

        chain_meta:    list = []
        seen_keys:     set  = set()
        matched_format: Optional[str] = None

        for _key, inst in _INSTRUMENT_CACHE.items():
            sym = inst.get("symbol", "")
            if inst.get("exch_seg") != "NFO":
                continue
            if inst.get("instrumenttype") not in ("OPTIDX", "OPTSTK"):
                continue
            if not (sym.endswith("CE") or sym.endswith("PE")):
                continue
            if underlying not in sym:
                continue
            sym_upper = sym.upper()
            if not any(fmt in sym_upper for fmt in expiry_formats):
                continue
            if sym in seen_keys:
                continue
            seen_keys.add(sym)
            if matched_format is None:
                for fmt in expiry_formats:
                    if fmt in sym_upper:
                        matched_format = fmt
                        break
            try:
                strike = int(float(inst.get("strike", 0))) // 100
            except Exception:
                continue
            if strike <= 0:
                continue
            chain_meta.append({
                "strike_price":   strike,
                "option_type":    "CE" if sym.endswith("CE") else "PE",
                "trading_symbol": sym,
                "token":          str(inst.get("token", "")),
                "ltp":            0.0,
                "oi":             0,
            })

        if not chain_meta:
            logger.error(f"[GET_OPTION_CHAIN] OUTPUT: 0 contracts for {underlying} expiry={expiry}")
            return [], 0

        logger.info(f"[GET_OPTION_CHAIN] Found {len(chain_meta)} contracts ({matched_format})")

        ltp        = self.get_ltp(instrument_key)
        if not ltp:
            logger.error(f"[GET_OPTION_CHAIN] OUTPUT: no live underlying LTP for {instrument_key} — cannot determine ATM, chain unusable")
            return [], 0
        atm_strike = round(ltp / step) * step

        all_strikes = sorted(set(c["strike_price"] for c in chain_meta))
        atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm_strike))
        lo          = max(0, atm_idx - 10)
        hi          = min(len(all_strikes) - 1, atm_idx + 10)
        live_strikes = set(all_strikes[lo: hi + 1])

        live_entries = [e for e in chain_meta if e["strike_price"] in live_strikes]
        tokens_batch = [e["token"] for e in live_entries if e["token"]]

        logger.info(f"[GET_OPTION_CHAIN] ATM={atm_strike}, batch-fetching {len(tokens_batch)} contracts")

        token_to_ltp: dict = {}
        token_to_oi:  dict = {}

        if tokens_batch:
            self.rate_limiter.wait("ltp")
            try:
                for chunk_start in range(0, len(tokens_batch), 50):
                    chunk = tokens_batch[chunk_start: chunk_start + 50]
                    resp  = self.smart_api.getMarketData(
                        mode="FULL", exchangeTokens={"NFO": chunk}
                    )
                    if resp and resp.get("status") and resp.get("data"):
                        for item in resp["data"].get("fetched", []):
                            tk = str(item.get("symbolToken", ""))
                            token_to_ltp[tk] = float(item.get("ltp", 0))
                            oi = (
                                item.get("opnInterest")
                                or item.get("openInterest")
                                or item.get("open_interest")
                                or 0
                            )
                            token_to_oi[tk] = int(oi)
                    if chunk_start > 0:
                        self.rate_limiter.wait("ltp")

                oi_nonzero = sum(1 for v in token_to_oi.values() if v > 0)
                logger.info(
                    f"[GET_OPTION_CHAIN] getMarketData: {len(token_to_ltp)} LTPs, "
                    f"{oi_nonzero}/{len(token_to_oi)} with OI > 0"
                )

            except Exception as e:
                logger.warning(f"[GET_OPTION_CHAIN] getMarketData failed ({e}) — falling back to individual ltpData")
                narrow = set(all_strikes[max(0, atm_idx - 5):
                                         min(len(all_strikes), atm_idx + 6)])
                for entry in chain_meta:
                    if entry["strike_price"] not in narrow:
                        continue
                    token = entry["token"]
                    if not token or token in token_to_ltp:
                        continue
                    self.rate_limiter.wait("ltp")
                    try:
                        r = self.smart_api.ltpData("NFO", entry["trading_symbol"], token)
                        if r and r.get("status") and r.get("data"):
                            token_to_ltp[token] = float(r["data"].get("ltp", 0))
                    except Exception:
                        pass

        for entry in chain_meta:
            tk = entry["token"]
            entry["ltp"] = token_to_ltp.get(tk, 0.0)
            entry["oi"]  = token_to_oi.get(tk, 0)

        if self.greeks_feed and tokens_batch:
            self.greeks_feed.subscribe(tokens_batch)

        chain_meta.sort(key=lambda x: (x["strike_price"], x["option_type"]))
        ltp_n = sum(1 for c in chain_meta if c["ltp"] > 0)
        oi_n  = sum(1 for c in chain_meta if c["oi"]  > 0)
        logger.info(f"[GET_OPTION_CHAIN] OUTPUT: {len(chain_meta)} contracts, {ltp_n} with LTP, {oi_n} with OI, atm={atm_strike}")
        return chain_meta, atm_strike

    def get_pcr(self, chain: list) -> Optional[float]:
        """
        Returns None (not a fake 1.0) when there is no OI and no LTP data to
        derive a ratio from, so callers know PCR is unavailable rather than
        silently treating it as neutral.
        """
        logger.debug(f"[GET_PCR] INPUT: chain_len={len(chain) if chain else 0}")
        if not chain:
            logger.warning("[GET_PCR] OUTPUT: None (empty chain)")
            return None
        pe_oi = sum(c.get("oi", 0) for c in chain if c.get("option_type") == "PE")
        ce_oi = sum(c.get("oi", 0) for c in chain if c.get("option_type") == "CE")
        if ce_oi > 0 and pe_oi > 0:
            pcr = round(pe_oi / ce_oi, 2)
            logger.debug(f"[GET_PCR] OUTPUT: {pcr} (from OI)")
            return pcr
        pe_ltp = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "PE")
        ce_ltp = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "CE")
        if ce_ltp > 0:
            pcr = round(pe_ltp / ce_ltp, 2)
            logger.debug(f"[GET_PCR] OUTPUT: {pcr} (LTP-proxy fallback, OI unavailable)")
            return pcr
        logger.warning("[GET_PCR] OUTPUT: None (no OI and no LTP data)")
        return None

    def compute_max_pain(self, chain: list) -> Optional[int]:
        """Returns None when total OI = 0 — max pain is meaningless without real OI."""
        logger.debug(f"[MAX_PAIN] INPUT: chain_len={len(chain) if chain else 0}")
        try:
            if not chain:
                return None
            strikes: dict = {}
            for item in chain:
                s = item.get("strike_price")
                if not s:
                    continue
                if s not in strikes:
                    strikes[s] = {"ce_oi": 0, "pe_oi": 0}
                oi = item.get("oi", 0) or 0
                if item.get("option_type") == "CE":
                    strikes[s]["ce_oi"] += oi
                else:
                    strikes[s]["pe_oi"] += oi

            if not strikes:
                return None

            total_oi = sum(v["ce_oi"] + v["pe_oi"] for v in strikes.values())
            if total_oi == 0:
                logger.debug("[MAX_PAIN] OUTPUT: None (all OI=0)")
                return None

            pain: dict = {}
            for test_s in strikes:
                total = sum(
                    strikes[s]["ce_oi"] * max(0, test_s - s) +
                    strikes[s]["pe_oi"] * max(0, s - test_s)
                    for s in strikes
                )
                pain[test_s] = total
            result = min(pain, key=pain.get)
            logger.debug(f"[MAX_PAIN] OUTPUT: {result}")
            return result
        except Exception as e:
            logger.error(f"[MAX_PAIN] ERROR: {e}")
            return None


# ════════════════════════════════════════════════════════════════════════════
# build_strategy_context()
# ════════════════════════════════════════════════════════════════════════════

def build_strategy_context(
    chain: list,
    vix: float,
    atm_strike: int,
    prev_chain_snapshot: dict,
    days_to_expiry: int,
    prev_vix: Optional[float] = None,
    underlying_ltp: Optional[float] = None,
    greeks_cache: Optional[GreeksCache] = None,
) -> dict:
    """
    Build the strategy context dict from live chain data.

    days_to_expiry is now REQUIRED (no default). It used to default to 1,
    which meant a caller that forgot to pass it would silently get theta
    computed as if every trade were 1 day from expiry (theta_pct capped
    at 0.30) — this could block or unblock GreeksStrategy for the wrong
    reason without any error being raised. Now a missing DTE is a hard
    TypeError at the call site instead of a silent wrong value.

    vix and atm_strike are also required — both must come from a live
    broker read (get_india_vix() / get_option_chain()) done just before
    calling this function.
    """
    logger.info(
        f"[BUILD_CONTEXT] INPUT: chain_len={len(chain) if chain else 0}, vix={vix}, "
        f"atm_strike={atm_strike}, days_to_expiry={days_to_expiry}, prev_vix={prev_vix}, "
        f"underlying_ltp={underlying_ltp}, prev_snapshot_strikes={len(prev_chain_snapshot) if prev_chain_snapshot else 0}"
    )

    vix_prev   = prev_vix if prev_vix is not None else vix
    vix_change = round((vix - vix_prev) / vix_prev * 100, 2) if vix_prev > 0 else 0.0

    theta_pct = round(min(0.30, 1.0 / (days_to_expiry + 1)), 3)

    VIX_52W_LOW  = 10.5
    VIX_52W_HIGH = 24.0
    iv_percentile = round(
        max(0.0, min(100.0, (vix - VIX_52W_LOW) / (VIX_52W_HIGH - VIX_52W_LOW) * 100)), 1
    )

    context = {
        "vix":               vix,
        "vix_prev":          vix_prev,
        "vix_change_pct":    vix_change,
        "underlying_ltp":    underlying_ltp,

        "iv_percentile":     iv_percentile,
        "atm_delta":         0.50,           # updated below once real LTPs are read
        "theta_pct":         theta_pct,
        "skew":              0.0,

        "pcr":               None,           # None until real OI/LTP proves otherwise
        "max_pain":          None,
        "total_ce_oi":       0,
        "total_pe_oi":       0,

        "days_to_expiry":    days_to_expiry,

        "chain_snapshot_now":  {},
        "chain_snapshot_prev": prev_chain_snapshot or {},
        "oi_change":           {"ce_added": 0, "pe_added": 0, "ce_shed": 0, "pe_shed": 0},

        "atm_ce_ltp": 0.0,
        "atm_pe_ltp": 0.0,
        "atm_ce_oi":  0,
        "atm_pe_oi":  0,
    }

    if not chain:
        logger.warning("[BUILD_CONTEXT] OUTPUT: empty chain — returning minimal context, pcr=None")
        return context

    ce: Dict[int, Dict] = {c["strike_price"]: c for c in chain if c["option_type"] == "CE"}
    pe: Dict[int, Dict] = {c["strike_price"]: c for c in chain if c["option_type"] == "PE"}
    all_strikes = sorted(set(ce.keys()) | set(pe.keys()))

    nearest_atm = min(all_strikes, key=lambda s: abs(s - atm_strike)) if all_strikes else atm_strike

    atm_ce_ltp = ce.get(nearest_atm, {}).get("ltp", 0.0)
    atm_pe_ltp = pe.get(nearest_atm, {}).get("ltp", 0.0)
    atm_ce_oi  = ce.get(nearest_atm, {}).get("oi",  0)
    atm_pe_oi  = pe.get(nearest_atm, {}).get("oi",  0)

    context["atm_ce_ltp"] = atm_ce_ltp
    context["atm_pe_ltp"] = atm_pe_ltp
    context["atm_ce_oi"]  = atm_ce_oi
    context["atm_pe_oi"]  = atm_pe_oi

    logger.info(
        f"[BUILD_CONTEXT] ATM={nearest_atm} CE_LTP={atm_ce_ltp} PE_LTP={atm_pe_ltp} "
        f"CE_OI={atm_ce_oi} PE_OI={atm_pe_oi}"
    )

    if atm_ce_ltp + atm_pe_ltp > 0:
        context["atm_delta"] = round(atm_ce_ltp / (atm_ce_ltp + atm_pe_ltp), 3)
    else:
        logger.warning("[BUILD_CONTEXT] ATM CE+PE LTP is 0 — atm_delta stays at neutral 0.50 placeholder")

    if greeks_cache:
        atm_ce_token = ce.get(nearest_atm, {}).get("token")
        atm_pe_token = pe.get(nearest_atm, {}).get("token")
        if atm_ce_token:
            g = greeks_cache.get(atm_ce_token)
            if g and g.get("delta") is not None:
                context["atm_delta"] = round(abs(float(g["delta"])), 3)
                context["atm_ce_iv"] = g.get("iv")
                context["atm_ce_theta"] = g.get("theta")
                context["atm_ce_gamma"] = g.get("gamma")
                context["atm_ce_vega"]  = g.get("vega")
                logger.info(f"[BUILD_CONTEXT] real CE delta={context['atm_delta']:.3f}")
        if atm_pe_token:
            g = greeks_cache.get(atm_pe_token)
            if g and g.get("theta") is not None:
                context["theta_pct"] = round(abs(float(g["theta"])) / max(atm_pe_ltp, 1), 3)
                context["atm_pe_iv"]    = g.get("iv")
                context["atm_pe_theta"] = g.get("theta")
                context["atm_pe_gamma"] = g.get("gamma")
                context["atm_pe_vega"]  = g.get("vega")
                logger.info(f"[BUILD_CONTEXT] real PE theta={g['theta']:.2f} → theta_pct={context['theta_pct']:.3f}")

    sorted_ce_strikes = sorted(ce.keys())
    strike_step = (sorted_ce_strikes[1] - sorted_ce_strikes[0]) if len(sorted_ce_strikes) >= 2 else 50

    for multiplier in [4, 6, 8]:
        otm_dist    = strike_step * multiplier
        otm_ce_ltp  = ce.get(nearest_atm + otm_dist, {}).get("ltp", 0)
        otm_pe_ltp  = pe.get(nearest_atm - otm_dist, {}).get("ltp", 0)
        if otm_ce_ltp > 0 and otm_pe_ltp > 0:
            context["skew"] = round(otm_pe_ltp - otm_ce_ltp, 2)
            logger.info(f"[BUILD_CONTEXT] skew={context['skew']:+.2f} (OTM±{otm_dist})")
            break

    snapshot_now: dict = {}
    for entry in chain:
        s = entry["strike_price"]
        if s not in snapshot_now:
            snapshot_now[s] = {"ce_oi": 0, "pe_oi": 0, "ce_ltp": 0.0, "pe_ltp": 0.0}
        if entry["option_type"] == "CE":
            snapshot_now[s]["ce_oi"]  = entry.get("oi",  0)
            snapshot_now[s]["ce_ltp"] = entry.get("ltp", 0.0)
        else:
            snapshot_now[s]["pe_oi"]  = entry.get("oi",  0)
            snapshot_now[s]["pe_ltp"] = entry.get("ltp", 0.0)
    context["chain_snapshot_now"] = snapshot_now

    oi_change = {"ce_added": 0, "pe_added": 0, "ce_shed": 0, "pe_shed": 0}
    if prev_chain_snapshot:
        for strike, now in snapshot_now.items():
            prev    = prev_chain_snapshot.get(strike, {})
            ce_diff = now["ce_oi"] - prev.get("ce_oi", 0)
            pe_diff = now["pe_oi"] - prev.get("pe_oi", 0)
            if ce_diff > 0: oi_change["ce_added"] += ce_diff
            else:           oi_change["ce_shed"]  += abs(ce_diff)
            if pe_diff > 0: oi_change["pe_added"] += pe_diff
            else:           oi_change["pe_shed"]  += abs(pe_diff)
    context["oi_change"] = oi_change

    total_pe_oi = sum(s["pe_oi"] for s in snapshot_now.values())
    total_ce_oi = sum(s["ce_oi"] for s in snapshot_now.values())
    context["total_pe_oi"] = total_pe_oi
    context["total_ce_oi"] = total_ce_oi

    if total_pe_oi > 0 and total_ce_oi > 0:
        context["pcr"] = round(total_pe_oi / total_ce_oi, 2)
        pcr_source = "OI"
    else:
        pe_ltp_sum = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "PE")
        ce_ltp_sum = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "CE")
        if ce_ltp_sum > 0:
            context["pcr"] = round(pe_ltp_sum / ce_ltp_sum, 2)
            pcr_source = "LTP-proxy"
        else:
            context["pcr"] = None
            pcr_source = "unavailable"

    logger.info(f"[BUILD_CONTEXT] PCR={context['pcr']} ({pcr_source}) CE_OI={total_ce_oi} PE_OI={total_pe_oi}")

    total_oi = total_pe_oi + total_ce_oi
    if total_oi > 0:
        pain: dict = {}
        for test_s in snapshot_now:
            p = sum(
                snapshot_now[s]["ce_oi"] * max(0, test_s - s) +
                snapshot_now[s]["pe_oi"] * max(0, s - test_s)
                for s in snapshot_now
            )
            pain[test_s] = p
        if pain:
            context["max_pain"] = min(pain, key=pain.get)

    logger.info(
        f"[BUILD_CONTEXT] OUTPUT: VIX={vix:.2f} IVP={iv_percentile:.0f}pct ATM={nearest_atm} "
        f"Delta={context['atm_delta']:.3f} Theta={context['theta_pct']:.3f} Skew={context['skew']:+.1f} "
        f"PCR={context['pcr']} MaxPain={context['max_pain']} DTE={days_to_expiry}"
    )

    return context