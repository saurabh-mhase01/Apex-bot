"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   ANGEL ONE — SmartAPI Broker Adapter  (v2 — FIXED)                          ║
║   Fixes:                                                                     ║
║     1. WRONG INDEX TOKENS  → equity-segment 26000/26009 swapped for the      ║
║        correct historical-data index tokens (99926000 / 99926009 / ...)      ║
║     2. NO RATE LIMITING    → added token-bucket throttle + exponential       ║
║        backoff on 403 "Access denied because of exceeding access rate"       ║
║     3. NO PERSISTENCE      → every raw request/response is now written to    ║
║        SQLite (api_call_log table) BEFORE parsing, so nothing is lost        ║
║        even if the bot crashes or the candle list is empty                   ║
║     4. SILENT EMPTY DATA   → empty responses are now logged with full        ║
║        request context and surfaced as a distinct warning, not just          ║
║        "Insufficient data"                                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
import time
import json
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import pandas as pd
import requests

logger = logging.getLogger("ANGELONE")

try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    import pyotp
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    logger.warning("SmartApi not installed. Run: pip install smartapi-python pyotp logzero")


# ════════════════════════════════════════════════════════════════════════════
# FIX #1 — CORRECT INDEX TOKENS
# ════════════════════════════════════════════════════════════════════════════
# 26000 / 26009 are the EQUITY/CASH segment quote tokens (work fine for ltpData)
# but the HISTORICAL CANDLE API needs the dedicated index token below.
# Verified against Angel One's official scrip master + community reports of
# the exact "empty data" symptom you're hitting.
# ════════════════════════════════════════════════════════════════════════════

INDEX_TOKENS = {
    # name              : (exchange, symbol_for_ltp, ltp_token, historical_token)
    "NIFTY":        {"exchange": "NSE", "symbol": "Nifty 50",        "ltp_token": "26000", "hist_token": "99926000"},
    "BANKNIFTY":    {"exchange": "NSE", "symbol": "Nifty Bank",      "ltp_token": "26009", "hist_token": "99926009"},
    "FINNIFTY":     {"exchange": "NSE", "symbol": "Nifty Fin Service","ltp_token": "26037", "hist_token": "99926037"},
    "INDIAVIX":     {"exchange": "NSE", "symbol": "India VIX",       "ltp_token": "99926017", "hist_token": "99926017"},
}

KEY_TO_UNDERLYING = {
    "NSE_INDEX|Nifty 50":          "NIFTY",
    "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
    "NSE_INDEX|Nifty Fin Service": "FINNIFTY",
    "NSE_INDEX|India VIX":         "INDIAVIX",
    "NIFTY":     "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
}

ANGEL_BASE = "https://apiconnect.angelone.in"
INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

EXCHANGE_NSE = "NSE"
EXCHANGE_NFO = "NFO"

PRODUCT_INTRADAY = "INTRADAY"
PRODUCT_DELIVERY = "DELIVERY"

ORDER_MARKET = "MARKET"
ORDER_LIMIT  = "LIMIT"
ORDER_STOPLOSS_LIMIT = "STOPLOSS_LIMIT"

VARIETY_NORMAL   = "NORMAL"
VARIETY_STOPLOSS = "STOPLOSS"

DURATION_DAY = "DAY"

INTERVAL_MAP = {
    "1minute": "ONE_MINUTE", "3minute": "THREE_MINUTE", "5minute": "FIVE_MINUTE",
    "10minute": "TEN_MINUTE", "15minute": "FIFTEEN_MINUTE", "30minute": "THIRTY_MINUTE",
    "1hour": "ONE_HOUR", "1day": "ONE_DAY",
}

_INSTRUMENT_CACHE: Dict[str, Dict] = {}
_INSTRUMENT_CACHE_DATE: Optional[date] = None


# ════════════════════════════════════════════════════════════════════════════
# FIX #2 — RATE LIMITER  (token bucket, conservative)
# ════════════════════════════════════════════════════════════════════════════
# Angel One historical API limit: 3 req/sec, but bursty back-to-back calls
# across 2 instruments in the same scheduler tick trip "exceeding access rate"
# even under that. We throttle to 1 request per 1.2s per endpoint class, with
# exponential backoff + retry on 403.
# ════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, min_interval_sec: float = 1.2):
        self.min_interval = min_interval_sec
        self._last_call: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, bucket: str = "default"):
        with self._lock:
            now = time.time()
            last = self._last_call.get(bucket, 0)
            elapsed = now - last
            if elapsed < self.min_interval:
                sleep_for = self.min_interval - elapsed
                logger.debug(f"⏳ Rate limit: sleeping {sleep_for:.2f}s before [{bucket}]")
                time.sleep(sleep_for)
            self._last_call[bucket] = time.time()


def with_backoff(fn, *args, max_retries: int = 4, base_delay: float = 2.0, **kwargs):
    """Retry with exponential backoff on 403 / rate-limit errors."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            is_rate_limit = "exceeding access rate" in err_str or "403" in err_str or "access denied" in err_str
            if is_rate_limit and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"🔁 Rate limited (attempt {attempt+1}/{max_retries}) — backing off {delay:.1f}s")
                time.sleep(delay)
                continue
            raise
    raise last_exc


# ════════════════════════════════════════════════════════════════════════════
# FIX #3 — API CALL LOGGING TO DB  (so nothing is silently lost)
# ════════════════════════════════════════════════════════════════════════════

class ApiCallLogger:
    """Persists every raw request + response to SQLite, independent of
    whether parsing succeeds. This is what was missing — you had no record
    of *why* candles were empty because nothing was saved before the
    'Insufficient data' check threw the response away."""

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
                    timestamp TEXT,
                    endpoint TEXT,
                    request_params TEXT,
                    response_status TEXT,
                    response_data TEXT,
                    candle_count INTEGER,
                    http_status INTEGER,
                    error_message TEXT,
                    latency_ms INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_key TEXT,
                    symboltoken TEXT,
                    interval TEXT,
                    timestamp TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    fetched_at TEXT,
                    UNIQUE(instrument_key, interval, timestamp)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_apilog_ts ON api_call_log(timestamp)")
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
                    datetime.now().isoformat(),
                    endpoint,
                    json.dumps(params, default=str),
                    str(response.get("status")) if response else "EXCEPTION",
                    json.dumps(response, default=str)[:5000] if response else None,
                    candle_count,
                    http_status,
                    error,
                    latency_ms,
                ])
        except Exception as e:
            logger.error(f"Failed to log API call: {e}")

    def save_candles(self, instrument_key: str, symboltoken: str,
                      interval: str, candles: List[list]):
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
        """Quick debug helper: see the last N empty/failed calls."""
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
        """Fall back to last-known-good candles from DB if live fetch is empty."""
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
# MAIN BROKER CLASS
# ════════════════════════════════════════════════════════════════════════════

class AngelOneBroker:
    """
    Fixed Angel One SmartAPI adapter.
    Same public interface as before — bot_engine.py needs no changes.

    NEW in v2:
      - broker.api_logger.get_recent_failures()  → debug empty responses
      - broker.get_ohlcv(..., use_db_fallback=True) → uses cached candles
        if live API returns empty (keeps the bot trading on stale-but-real data
        instead of permanently stalling on "Insufficient data")
    """

    def __init__(self, db_path: str = "data/bot.db"):
        
        self.api_key       = "JGMUJJ4n"
        self.client_id     = "S62103272"
        self.password      = "5763"
        self.totp_secret   = "OJOAT3LK5KW5M3U4LXJSHHZSDA"
        self.paper_trading = True;

        self.smart_api      = None
        self.auth_token     = None
        self.refresh_token  = None
        self.feed_token     = None
        self._order_counter = 2000
        self._session_date  = None

        self.rate_limiter = RateLimiter(min_interval_sec=1.2)
        self.api_logger   = ApiCallLogger(db_path)

        if not SMARTAPI_AVAILABLE:
            logger.error("Install SmartApi: pip install smartapi-python pyotp logzero")
            return

        self._authenticate()
        self._load_instrument_cache()

    # ─────────────────────────────────────────────────────────────────────────
    # AUTH
    # ─────────────────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        try:
            totp = pyotp.TOTP(self.totp_secret).now()
            self.smart_api = SmartConnect(self.api_key)
            data = self.smart_api.generateSession(self.client_id, self.password, totp)

            if not data or data.get("status") is False:
                logger.error(f"Angel One auth failed: {data}")
                return False

            self.auth_token    = data["data"]["jwtToken"]
            self.refresh_token = data["data"]["refreshToken"]
            self.feed_token    = self.smart_api.getfeedToken()
            self._session_date = date.today()
            logger.info(f"✅ Angel One SmartAPI authenticated | Client: {self.client_id}")
            return True
        except Exception as e:
            logger.error(f"Angel One auth exception: {e}")
            return False

    def _ensure_session(self):
        if self._session_date != date.today():
            logger.info("Session expired — re-authenticating...")
            self._authenticate()

    # ─────────────────────────────────────────────────────────────────────────
    # INSTRUMENT CACHE
    # ─────────────────────────────────────────────────────────────────────────

    def _load_instrument_cache(self):
        global _INSTRUMENT_CACHE, _INSTRUMENT_CACHE_DATE
        if _INSTRUMENT_CACHE_DATE == date.today() and _INSTRUMENT_CACHE:
            return
        try:
            logger.info("📥 Loading Angel One instrument master...")
            resp = requests.get(INSTRUMENT_URL, timeout=30)
            instruments = resp.json()
            _INSTRUMENT_CACHE = {}
            for inst in instruments:
                key = f"{inst.get('exch_seg', '')}:{inst.get('symbol', '')}"
                _INSTRUMENT_CACHE[key] = inst
                _INSTRUMENT_CACHE[inst.get("symbol", "")] = inst
            _INSTRUMENT_CACHE_DATE = date.today()
            logger.info(f"✅ Instrument cache loaded: {len(instruments):,} instruments")
        except Exception as e:
            logger.error(f"Instrument cache load error: {e}")

    def _get_token(self, trading_symbol: str, exchange: str = "NFO") -> Optional[str]:
        self._load_instrument_cache()
        key = f"{exchange}:{trading_symbol}"
        inst = _INSTRUMENT_CACHE.get(key) or _INSTRUMENT_CACHE.get(trading_symbol)
        return str(inst.get("token", "")) if inst else None

    def _search_option_token(self, underlying: str, strike: int,
                              option_type: str, expiry_str: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            dt = datetime.strptime(expiry_str, "%Y-%m-%d")
            mon = dt.strftime("%b").upper()
            yy  = dt.strftime("%y")
            dd  = dt.strftime("%d")
            symbol = f"{underlying}{dd}{mon}{yy}{strike}{option_type}"
            token = self._get_token(symbol, "NFO")
            if token:
                return symbol, token
            return None, None
        except Exception as e:
            logger.error(f"Option search error: {e}")
            return None, None

    # ─────────────────────────────────────────────────────────────────────────
    # MARKET DATA  (FIXED token resolution + rate limiting + logging)
    # ─────────────────────────────────────────────────────────────────────────

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        self._ensure_session()
        exchange, symbol, token = self._resolve_for_ltp(instrument_key)
        if not token:
            logger.warning(f"⚠️ No LTP token resolved for {instrument_key}")
            return None

        self.rate_limiter.wait("ltp")
        t0 = time.time()
        try:
            resp = with_backoff(self.smart_api.ltpData, exchange, symbol, token)
            latency = int((time.time() - t0) * 1000)
            self.api_logger.log_call("ltpData",
                {"exchange": exchange, "symbol": symbol, "token": token},
                resp, latency_ms=latency)

            if resp and resp.get("status"):
                return float(resp["data"].get("ltp", 0)) or None

            logger.warning(f"LTP call returned status=False for {instrument_key}: {resp}")
            return None

        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            self.api_logger.log_call("ltpData",
                {"exchange": exchange, "symbol": symbol, "token": token},
                {}, latency_ms=latency, error=str(e))
            logger.error(f"LTP error [{instrument_key}]: {e}")
            return None

    def get_ohlcv(self, instrument_key: str, interval: str = "30minute",
                  days: int = 30, use_db_fallback: bool = True) -> pd.DataFrame:
        """
        Historical OHLCV candles — FIXED to use correct index historical tokens.

        Key fix: indices (Nifty/BankNifty/etc.) need a DIFFERENT token for the
        historical candle endpoint than for LTP. Using the LTP token (26000)
        here is exactly why you were getting `data: []` with status=True.
        """
        self._ensure_session()
        exchange, symboltoken = self._resolve_for_historical(instrument_key)

        if not symboltoken:
            logger.error(f"❌ No historical-data token resolved for {instrument_key} — cannot fetch candles")
            if use_db_fallback:
                return self.api_logger.get_cached_candles(instrument_key, interval, days)
            return pd.DataFrame()

        angel_interval = INTERVAL_MAP.get(interval, "THIRTY_MINUTE")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        to_date   = datetime.now().strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": angel_interval,
            "fromdate": from_date,
            "todate": to_date,
        }

        logger.info(f"[ANGELONE] Candle request → {params}")
        self.rate_limiter.wait("historical")

        t0 = time.time()
        try:
            resp = with_backoff(self.smart_api.getCandleData, params)
            latency = int((time.time() - t0) * 1000)

            self.api_logger.log_call("getCandleData", params, resp, latency_ms=latency)

            if not resp or not resp.get("status"):
                logger.error(f"[ANGELONE] Candle FAILED status=False: {resp}")
                if use_db_fallback:
                    cached = self.api_logger.get_cached_candles(instrument_key, interval, days)
                    if not cached.empty:
                        logger.warning(f"↩️ Falling back to {len(cached)} cached candles from DB")
                        return cached
                return pd.DataFrame()

            candles = resp.get("data", [])

            if not candles:
                logger.warning(
                    f"[ANGELONE] ⚠️ Candle response EMPTY (status=True but 0 rows). "
                    f"Params: exchange={exchange} token={symboltoken} interval={angel_interval} "
                    f"from={from_date} to={to_date}. "
                    f"Likely cause: wrong symboltoken for this instrument, or no trading "
                    f"in this window (holiday/weekend/pre-market)."
                )
                if use_db_fallback:
                    cached = self.api_logger.get_cached_candles(instrument_key, interval, days)
                    if not cached.empty:
                        logger.warning(f"↩️ Falling back to {len(cached)} cached candles from DB")
                        return cached
                return pd.DataFrame()

            # ── Success — persist + return ──────────────────────────────────
            saved = self.api_logger.save_candles(instrument_key, symboltoken, interval, candles)
            logger.info(f"[ANGELONE] ✅ Got {len(candles)} candles, saved {saved} to DB")

            df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            df = df.sort_index()
            df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
            return df

        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            self.api_logger.log_call("getCandleData", params, {}, latency_ms=latency, error=str(e))
            logger.error(f"[ANGELONE] OHLCV exception [{instrument_key}]: {e}")
            if use_db_fallback:
                cached = self.api_logger.get_cached_candles(instrument_key, interval, days)
                if not cached.empty:
                    logger.warning(f"↩️ Falling back to {len(cached)} cached candles from DB after exception")
                    return cached
            return pd.DataFrame()

    def get_india_vix(self) -> Optional[float]:
        return self.get_ltp("NSE_INDEX|India VIX")

    def get_funds(self) -> Dict:
        self._ensure_session()
        self.rate_limiter.wait("funds")
        try:
            resp = with_backoff(self.smart_api.rmsLimit)
            if resp and resp.get("status"):
                return resp.get("data", {})
        except Exception as e:
            logger.error(f"Funds error: {e}")
        return {}

    def get_positions(self) -> List[Dict]:
        self._ensure_session()
        self.rate_limiter.wait("positions")
        try:
            resp = with_backoff(self.smart_api.position)
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception as e:
            logger.error(f"Positions error: {e}")
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

    # ─────────────────────────────────────────────────────────────────────────
    # ORDERS
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, instrument_key: str, qty: int,
                    order_type: str = "MARKET", price: float = 0,
                    transaction_type: str = "BUY") -> Dict:
        if self.paper_trading:
            return self._paper_order(instrument_key, qty, transaction_type, price)

        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_for_ltp(instrument_key)
            if not token:
                token = self._get_token(instrument_key, "NFO")
                exchange, symbol = "NFO", instrument_key
            if not token:
                return {"error": f"Token not found for {instrument_key}"}

            product = PRODUCT_INTRADAY
            angel_order_type = ORDER_MARKET if order_type == "MARKET" else ORDER_LIMIT

            order_params = {
                "variety": VARIETY_NORMAL,
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": transaction_type,
                "exchange": exchange,
                "ordertype": angel_order_type,
                "producttype": product,
                "duration": DURATION_DAY,
                "price": str(price) if order_type == "LIMIT" else "0",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
            }

            self.rate_limiter.wait("orders")
            resp = with_backoff(self.smart_api.placeOrder, order_params)
            self.api_logger.log_call("placeOrder", order_params, resp if isinstance(resp, dict) else {"raw": resp})

            if resp and isinstance(resp, dict) and resp.get("status"):
                oid = resp.get("data", {}).get("orderid", "")
                logger.info(f"✅ Order placed: {transaction_type} {qty} {symbol} → {oid}")
                return {"order_id": oid, "status": "complete"}
            return {"error": str(resp)}

        except Exception as e:
            logger.error(f"Place order exception: {e}")
            return {"error": str(e)}

    def place_sl_order(self, instrument_key: str, qty: int,
                       trigger_price: float, price: float) -> Dict:
        if self.paper_trading:
            return {"order_id": f"PAPER_SL_{self._order_counter}"}
        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_for_ltp(instrument_key)
            if not token:
                token = self._get_token(instrument_key, "NFO")
                exchange, symbol = "NFO", instrument_key
            order_params = {
                "variety": VARIETY_STOPLOSS,
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": "SELL",
                "exchange": exchange,
                "ordertype": ORDER_STOPLOSS_LIMIT,
                "producttype": PRODUCT_INTRADAY,
                "duration": DURATION_DAY,
                "price": str(price),
                "triggerprice": str(trigger_price),
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
            }
            self.rate_limiter.wait("orders")
            resp = with_backoff(self.smart_api.placeOrder, order_params)
            if resp and resp.get("status"):
                return {"order_id": resp["data"].get("orderid", "")}
            return {"error": str(resp)}
        except Exception as e:
            logger.error(f"SL order error: {e}")
            return {"error": str(e)}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        if self.paper_trading:
            return True
        self._ensure_session()
        try:
            resp = with_backoff(self.smart_api.cancelOrder, order_id, variety)
            return bool(resp and resp.get("status"))
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def get_atm_strike(self, instrument_key: str, lot_size: int = 50) -> Optional[int]:
        ltp = self.get_ltp(instrument_key)
        return round(ltp / lot_size) * lot_size if ltp else None

    def find_option_instrument(self, underlying_key: str, strike: int,
                                option_type: str, expiry: str) -> Optional[str]:
        underlying = KEY_TO_UNDERLYING.get(underlying_key, underlying_key)
        symbol, _ = self._search_option_token(underlying, strike, option_type, expiry)
        return symbol
    
    # ── Get option expiries from instrument cache ─────────────────────────────
    def get_option_expiries(self, instrument_key: str) -> List[str]:
        try:
            from datetime import datetime
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
                    # Cache format: "23JUN2026"  or  "23Jun2026"
                    dt = datetime.strptime(exp_raw.upper(), "%d%b%Y")
                    expiry_set.add(dt.strftime("%Y-%m-%d"))
                except ValueError:
                    pass

            expiries = sorted(expiry_set)
            logger.info(f"[EXPIRY] {underlying}: {len(expiries)} expiries found. "
                        f"Next: {expiries[0] if expiries else 'none'}")
            return expiries

        except Exception as e:
            logger.error(f"Expiry fetch error: {e}")
            return []


    # ─────────────────────────────────────────────────────────────────────────
    # FIX #1 (core) — separate resolvers for LTP vs Historical endpoints
    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_for_ltp(self, key: str) -> Tuple[str, str, Optional[str]]:
        """Token to use with ltpData() — equity/cash-segment token."""
        underlying = KEY_TO_UNDERLYING.get(key)
        if underlying and underlying in INDEX_TOKENS:
            info = INDEX_TOKENS[underlying]
            return info["exchange"], info["symbol"], info["ltp_token"]
        # Option / non-index symbol
        token = self._get_token(key, "NFO")
        return "NFO", key, token

    def _resolve_for_historical(self, key: str) -> Tuple[str, Optional[str]]:
        """Token to use with getCandleData() — THE FIX: dedicated index token."""
        underlying = KEY_TO_UNDERLYING.get(key)
        if underlying and underlying in INDEX_TOKENS:
            info = INDEX_TOKENS[underlying]
            return info["exchange"], info["hist_token"]
        # Option / equity symbol — same token works for both LTP & historical
        token = self._get_token(key, "NFO")
        return "NFO", token

    def _paper_order(self, symbol: str, qty: int, tx_type: str, price: float) -> Dict:
        self._order_counter += 1
        oid = f"PAPER_AO_{self._order_counter}"
        logger.info(f"📝 PAPER ORDER [AngelOne]: {tx_type} {qty} {symbol} @ ₹{price} → {oid}")
        return {"order_id": oid, "status": "complete"}

    def logout(self):
        try:
            self.smart_api.terminateSession(self.client_id)
            logger.info("Angel One session terminated")
        except Exception:
            pass
    
    # ─────────────────────────────────────────────────────────────────────────────
    # METHOD 1 — get_option_()
    # Used by bot_engine._evaluate_instrument() at line 151
    # ─────────────────────────────────────────────────────────────────────────────
    
    def get_option_chain(self, instrument_key: str, expiry: str) -> list:
        """
        Build option chain for given underlying + expiry.
    
        Returns list of dicts:
        [
        {
            "strike_price": 24500,
            "option_type":  "CE",
            "trading_symbol": "NIFTY23JUN2624500CE",
            "token": "12345",
            "ltp": 142.5,
            "oi": 0,           # OI not available via REST; use WebSocket for live OI
            "iv": 0,
        },
        ...
        ]
    
        Strategy: build entirely from cached instrument master (fast, no extra
        API calls per strike). Then fetch LTP only for ATM ± 10 strikes via
        a single batch call to avoid rate-limit.
        """
        self._ensure_session()
        self._load_instrument_cache()
    
        underlying   = KEY_TO_UNDERLYING.get(instrument_key, instrument_key)
        expiry_angel = self._format_expiry_for_cache(expiry)   # "23JUN2026"
        logger.info(f"[OC] Building chain: underlying={underlying} expiry_angel={expiry_angel}")
        # ── Step 1: scan instrument cache for matching NFO options ────────────
        chain_meta = []
        seen_keys  = set()

        for _key, inst in _INSTRUMENT_CACHE.items():
            sym  = inst.get("symbol", "")
            exch = inst.get("exch_seg", "")
            itype = inst.get("instrumenttype", "")
    
            if exch != "NFO":
                continue
            if itype not in ("OPTIDX", "OPTSTK"):
                continue
            if underlying not in sym:
                continue
            if expiry_angel not in sym.upper():
                continue
            if not sym.endswith("CE") and not sym.endswith("PE"):
                continue

            dedup_key = sym
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            try:
                raw_strike = inst.get("strike", 0)
                # Angel One stores strike * 100 in the master file
                strike = int(float(raw_strike)) // 100   # Angel stores strike * 100
            except (ValueError, TypeError):
                continue
    
            if strike <= 0:
                continue

            opt_type = "CE" if sym.endswith("CE") else "PE"
            token    = str(inst.get("token", ""))
    
            chain_meta.append({
                "strike_price":   strike,
                "option_type":    opt_type,
                "trading_symbol": sym,
                "token":          token,
                "ltp":            0.0,
                "oi":             0,
                "iv":             0.0,
            })
    
        if not chain_meta:
            logger.warning(
                f"[OC] No option chain data found for {underlying} expiry={expiry} "
                f"(angel_expiry={expiry_angel}). Cache has {len(_INSTRUMENT_CACHE):,} instruments. "
                f"Check that expiry format is correct and NFO instruments are loaded."
                f"Possible cause: expiry not in instrument cache yet (loaded at startup). "
                f"Cache size: {len(_INSTRUMENT_CACHE):,} instruments."
            )
            return []
        logger.info(f"[OC] Found {len(chain_meta)} contracts in cache for {underlying} {expiry_angel}")

        # ── Step 2: find ATM strike from current LTP ──────────────────────────
        lot_size    = 50 if underlying == "NIFTY" else 25
        atm_strike  = self.get_atm_strike(instrument_key, lot_size) or 0

        all_strikes = sorted(set(c["strike_price"] for c in chain_meta))
        live_strikes  = set()

        # Fetch LTP only for ATM ± 10 strikes (20 strikes × 2 = 40 contracts max)
        if atm_strike and all_strikes:
            atm_idx    = min(range(len(all_strikes)),
                            key=lambda i: abs(all_strikes[i] - atm_strike))
            lo_idx     = max(0, atm_idx - 10)
            hi_idx     = min(len(all_strikes) - 1, atm_idx + 10)
            live_strikes = set(all_strikes[lo_idx: hi_idx + 1])
            logger.info(
                f"[OC] ATM={atm_strike}, fetching LTP for "
                f"{len(live_strikes)} strikes ({all_strikes[lo]}–{all_strikes[hi]})"
            )
        else:
            live_strikes = set(all_strikes[:20])   # fallback: first 20 strikes
            logger.warning(f"[OC] ATM unavailable, using first {len(live_strikes)} strikes")

        # ── Step 3: fetch LTP for live strikes one at a time with rate limiting
        token_to_ltp: dict = {}
        for entry in chain_meta:
            if entry["strike_price"] not in live_strikes:
                continue
            token = entry["token"]
            if not token or token in token_to_ltp:
                continue
    
            self.rate_limiter.wait("ltp")
            try:
                resp = self.smart_api.ltpData(
                    "NFO",
                    entry["trading_symbol"],
                    token
                )
                if resp and resp.get("status") and resp.get("data"):
                    ltp = float(resp["data"].get("ltp", 0))
                    token_to_ltp[token] = ltp
            except Exception as e:
                logger.debug(f"[OC] LTP fetch failed for {entry['trading_symbol']}: {e}")
    
        # ── Step 4: merge LTP into chain + sort ──────────────────────────────
        for entry in chain_meta:
            entry["ltp"] = token_to_ltp.get(entry["token"], 0.0)
    
        chain_meta.sort(key=lambda x: (x["strike_price"], x["option_type"]))
        ltp_fetched = sum(1 for c in chain_meta if c["ltp"] > 0)
        logger.info(
            f"[OC] {underlying} chain built: {len(chain_meta)} contracts "
            f"({len(live_strikes)} strikes with live LTP), expiry={expiry}"
            f"{ltp_fetched} with live LTP, expiry={expiry}"
        )
        return chain_meta
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # METHOD 2 — get_pcr()
    # Used by regime_classifier and OI flow strategy
    # ─────────────────────────────────────────────────────────────────────────────
    
    def get_pcr(self, chain: list) -> float:
        """
        Put-Call Ratio from option chain OI.
        Since OI is 0 in REST-based chain, we use open-interest proxy = count of
        PE contracts vs CE contracts at each strike (structural PCR).
        For real OI subscribe to WebSocket feed.
        """
        try:
            if not chain:
                return 1.0
            # If any OI data present, use it
            total_pe_oi = sum(c.get("oi", 0) for c in chain if c.get("option_type") == "PE")
            total_ce_oi = sum(c.get("oi", 0) for c in chain if c.get("option_type") == "CE")
            if total_ce_oi > 0 and total_pe_oi > 0:
                return round(total_pe_oi / total_ce_oi, 2)
            # Fallback: use LTP-weighted proxy (higher premium = more activity)
            pe_ltp = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "PE")
            ce_ltp = sum(c.get("ltp", 0) for c in chain if c.get("option_type") == "CE")
            return round(pe_ltp / ce_ltp, 2) if ce_ltp > 0 else 1.0
        except Exception:
            return 1.0
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # METHOD 3 — compute_max_pain()  (already in original but ensure it's present)
    # ─────────────────────────────────────────────────────────────────────────────
    
    def compute_max_pain(self, chain: list) -> int | None:
        """Max pain strike from option chain."""
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

            pain: dict = {}
            for test_s in strikes:
                total = sum(
                    strikes[s]["ce_oi"] * max(0, test_s - s) +
                    strikes[s]["pe_oi"] * max(0, s - test_s)
                    for s in strikes
                )
                pain[test_s] = total

            return min(pain, key=pain.get)
        except Exception:
            return None
    
    def _format_expiry_for_cache(self, expiry_yyyy_mm_dd: str) -> str:
            """
            Convert "2026-06-23" → "23JUN2026"
            Angel One instrument cache stores expiry in DDMMMYYYY format (uppercase).
            This is used to match option symbols in the 175k instrument master.
            """
            try:
                from datetime import datetime
                dt = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d")
                return dt.strftime("%d%b%Y").upper()   # e.g. "23JUN2026"
            except Exception as e:
                logger.warning(f"[FORMAT_EXPIRY] Failed to parse '{expiry_yyyy_mm_dd}': {e}")
                return expiry_yyyy_mm_dd
        