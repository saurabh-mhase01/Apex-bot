"""
Market Data Service — the ONLY component allowed to talk to the broker.

Every other part of the bot (regime classifier, strategy engine, dashboard
API, execution) reads through this service instead of AngelOneBroker
directly. This exists specifically to fix the AB1021 "Access denied because
of exceeding access rate" errors, which came from three independent,
uncoordinated polling paths (background ingestion thread every 60s,
check_signals() every 5min, compute_daily_regime() every 15min) all calling
the broker with no shared cache and no global rate ceiling.
"""
import logging
import threading
import time
from typing import Dict, Optional, List, Tuple, Any, Callable

import pandas as pd

from data.Angle_broker_v2 import AngelOneBroker, GreeksCache, KEY_TO_UNDERLYING, INDEX_TOKENS
from data.data_engine import DataEngine

logger = logging.getLogger("MARKET_DATA_SERVICE")


class TokenBucketLimiter:
    """
    Global rate limiter — ALL broker requests draw from ONE bucket.
    The old per-endpoint RateLimiter let every bucket (ltp/historical/orders/...)
    burst independently, so the combined call volume across buckets could still
    blow through Angel One's real account-level limit even though each
    individual bucket looked fine.
    """
    def __init__(self, rate_per_sec: float = 3.0, burst: int = 5):
        self.rate = rate_per_sec
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self._lock = threading.Lock()

    # def acquire(self, cost: float = 1.0):
    #     with self._lock:
    #         now = time.monotonic()
    #         self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
    #         self.last = now
    #         if self.tokens < cost:
    #             sleep_for = (cost - self.tokens) / self.rate
    #             logger.debug(f"[TOKEN_BUCKET] waiting {sleep_for:.2f}s (tokens={self.tokens:.2f})")
    #             time.sleep(sleep_for)
    #             self.tokens = 0.0
    #         else:
    #             self.tokens -= cost
    def acquire(self, cost: float = 1.0):
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                sleep_for = (cost - self.tokens) / self.rate
            time.sleep(sleep_for)   # sleep OUTSIDE the lock


class MarketDataService:
    def __init__(self, broker: AngelOneBroker, data_engine: DataEngine,
                 rate_per_sec: float = 3.0, hot_cache_ttl: float = 3.0):
        """
        rate_per_sec: keep this comfortably BELOW your Angel One plan's documented
        limit (commonly ~3 req/sec for historical/quote endpoints) — verify against
        your account tier before deploying live.
        hot_cache_ttl: window in which repeat requests for the SAME hot value
        (VIX, an LTP) are served from cache instead of hitting the network. This is
        what stops the 3-independent-call-sites-hitting-VIX-at-once problem.
        """
        self.broker = broker
        self.data_engine = data_engine
        self.limiter = TokenBucketLimiter(rate_per_sec=rate_per_sec)
        self.hot_cache_ttl = hot_cache_ttl
        self._hot_cache: Dict[str, Tuple[float, Any]] = {}
        self._hot_cache_lock = threading.Lock()

        self.greeks_cache = GreeksCache()
        self._ws = None
        self._ws_subscribed_tokens: set = set()
        self._ws_lock = threading.Lock()

        logger.info(f"[MDS_INIT] rate_per_sec={rate_per_sec}, hot_cache_ttl={hot_cache_ttl}")

    # ── Hot cache (VIX / LTP) ────────────────────────────────────────────
    def _cached(self, cache_key: str, fetch_fn: Callable[[], Any]) -> Any:
        with self._hot_cache_lock:
            hit = self._hot_cache.get(cache_key)
            now = time.time()
            if hit and (now - hit[0]) < self.hot_cache_ttl:
                logger.debug(f"[MDS_CACHE] HIT {cache_key}")
                return hit[1]
        self.limiter.acquire()
        value = fetch_fn()
        with self._hot_cache_lock:
            self._hot_cache[cache_key] = (time.time(), value)
        logger.debug(f"[MDS_CACHE] MISS {cache_key} → fetched fresh")
        return value

    def get_vix(self) -> Optional[float]:
        ws_val = self._ws_ltp("NSE_INDEX|India VIX")
        if ws_val is not None:
            return ws_val
        return self._cached("vix", self.broker.get_india_vix)

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        ws_val = self._ws_ltp(instrument_key)
        if ws_val is not None:
            return ws_val
        return self._cached(f"ltp:{instrument_key}", lambda: self.broker.get_ltp(instrument_key))

    # ── Historical candles — always read local DB first ─────────────────
    def get_ohlcv(self, instrument_key: str, interval: str, days: int = 30, use_db_fallback: bool = True) -> pd.DataFrame:
        cached = self.data_engine.get_candles_with_live_bar(instrument_key, interval, limit=500)
        if not cached.empty:
            return cached
        # Only hits the broker on a genuine cold start (empty DB for this key/interval).
        # Steady-state, backfill() below keeps the DB populated so this branch
        # shouldn't fire during normal running or after a restart.
        self.limiter.acquire()
        df = self.broker.get_ohlcv(instrument_key, interval, days=days, use_db_fallback=use_db_fallback)
        if not df.empty:
            self.data_engine.save_candles(instrument_key, interval, [
                {"timestamp": idx, "open": r.open, "high": r.high, "low": r.low,
                 "close": r.close, "volume": r.volume, "source": "backfill"}
                for idx, r in df.iterrows()
            ])
        return df

    def backfill(self, instrument_key: str, interval: str):
        """
        Called periodically by the scheduler loop (NOT per-evaluation). Pulls
        only the gap since the last stored candle via the rate-gated broker
        call and merges into DataEngine. This is the sole replacement for the
        old unconditional 60s background-ingestion thread.
        """
        self.limiter.acquire()
        self.data_engine.ingest_from_broker(self.broker, instrument_key, interval)

    # ── Options chain / expiries — rate-gated pass-through ───────────────
    def get_option_expiries(self, instrument_key: str) -> List[str]:
        self.limiter.acquire()
        return self.broker.get_option_expiries(instrument_key)

    def get_option_chain(self, instrument_key: str, expiry: str) -> Tuple[list, int]:
        self.limiter.acquire()
        return self.broker.get_option_chain(instrument_key, expiry)

    def get_lot_size(self, instrument_key: str) -> Optional[int]:
        return self.broker.get_lot_size(instrument_key)  # instrument-cache backed, no network

    def get_strike_step(self, instrument_key: str) -> int:
        return self.broker.get_strike_step(instrument_key)

    def find_option_instrument(self, underlying_key: str, strike: int,
                                option_type: str, expiry: str) -> Optional[str]:
        return self.broker.find_option_instrument(underlying_key, strike, option_type, expiry)

    # ── Orders — never cached, always rate-gated ─────────────────────────
    def place_order(self, instrument_key: str, qty: int, transaction_type: str = "BUY") -> Dict:
        self.limiter.acquire()
        return self.broker.place_order(instrument_key, qty, transaction_type=transaction_type)

    # ── WebSocket live feed ───────────────────────────────────────────────
    def start(self, index_tokens: List[str]):
        """Subscribe to the underlying indices via WS. Call once at startup."""
        self._ws = self.broker.start_live_feed(
            [{"exchangeType": 2, "tokens": index_tokens}],
            callback=self._on_ws_tick,
        )
        with self._ws_lock:
            self._ws_subscribed_tokens.update(index_tokens)
        logger.info(f"[MDS_WS] started, subscribed underlying tokens={index_tokens}")

    def sync_option_subscriptions(self, tokens: List[str]):
        """
        Call once per cycle with the current ATM±N option tokens for each
        instrument. Diffs against what's already subscribed and only adds the
        delta, instead of re-subscribing everything every cycle as ATM drifts.
        mode=3 is Angel One's SnapQuote mode (LTP + OI + depth) — confirm
        against current SmartAPI WS docs before relying on the field names.
        """
        if not self._ws or not tokens:
            return
        with self._ws_lock:
            new_tokens = set(tokens) - self._ws_subscribed_tokens
            if new_tokens:
                try:
                    self._ws.subscribe("options", mode=3, token_list=[
                        {"exchangeType": 2, "tokens": list(new_tokens)}
                    ])
                    self._ws_subscribed_tokens.update(new_tokens)
                    logger.info(f"[MDS_WS] subscribed {len(new_tokens)} new option tokens")
                except Exception as e:
                    logger.warning(f"[MDS_WS] subscribe failed: {e}")

    def _on_ws_tick(self, message: Dict):
        try:
            token = str(message.get("token", ""))
            if not token:
                return
            ltp = message.get("ltp")
            oi = message.get("oi")
            now = time.time()
            if ltp is not None:
                with self._hot_cache_lock:
                    self._hot_cache[f"ws_ltp_token:{token}"] = (now, float(ltp))
                update = {"ltp": float(ltp)}
                if oi is not None:
                    update["oi"] = int(oi)
                self.greeks_cache.update(token, update)
            self.data_engine.save_market_snapshot("LIVE", f"quote:{token}", message)
        except Exception as e:
            logger.warning(f"[MDS_WS] tick handling error: {e}")

    def _ws_ltp(self, instrument_key: str) -> Optional[float]:
        """Fresh WS-pushed LTP for an underlying index, or None to fall through to REST."""
        underlying = KEY_TO_UNDERLYING.get(instrument_key)
        if not underlying or underlying not in INDEX_TOKENS:
            return None
        token = INDEX_TOKENS[underlying]["ltp_token"]
        with self._hot_cache_lock:
            hit = self._hot_cache.get(f"ws_ltp_token:{token}")
        if hit and (time.time() - hit[0]) < self.hot_cache_ttl:
            return hit[1]
        return None

    def stop(self):
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass