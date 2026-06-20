"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          ANGEL ONE — SmartAPI Broker Adapter                                ║
║          Drop-in replacement for groww_broker.py / upstox_broker.py        ║
║          pip install smartapi-python pyotp logzero requests                ║
║          API: https://smartapi.angelbroking.com                             ║
╚══════════════════════════════════════════════════════════════════════════════╝

Auth flow:
  - api_key      → from smartapi.angelbroking.com (create app → Trading API)
  - client_id    → your Angel One login ID (e.g. A12345)
  - password     → your 4-digit Angel One MPIN
  - totp_secret  → QR secret from Angel One TOTP setup page
  Session lasts until midnight, auto-refreshed by this adapter.
"""

import logging
import uuid
import time
import json
import requests
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Tuple
import pandas as pd

logger = logging.getLogger("ANGELONE")

# ── Try importing SmartApi ────────────────────────────────────────────────────
try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    import pyotp
    SMARTAPI_AVAILABLE = True
    logger.info("datetime.now() = %s", datetime.now())
except ImportError:
    SMARTAPI_AVAILABLE = False
    logger.warning("SmartApi not installed. Run: pip install smartapi-python pyotp logzero")

# ── Angel One instrument token cache ─────────────────────────────────────────
# Angel One requires symbol_token for every order.
# We load the master instrument list once per day.
_INSTRUMENT_CACHE: Dict[str, Dict] = {}
_INSTRUMENT_CACHE_DATE: Optional[date] = None

ANGEL_BASE = "https://apiconnect.angelone.in"
INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Exchange & Segment constants (Angel One uses strings)
EXCHANGE_NSE   = "NSE"
EXCHANGE_BSE   = "BSE"
EXCHANGE_NFO   = "NFO"   # F&O segment
EXCHANGE_MCX   = "MCX"

PRODUCT_INTRADAY  = "INTRADAY"
PRODUCT_DELIVERY  = "DELIVERY"
PRODUCT_MARGIN    = "MARGIN"

ORDER_MARKET  = "MARKET"
ORDER_LIMIT   = "LIMIT"
ORDER_STOPLOSS = "STOPLOSS_MARKET"
ORDER_STOPLOSS_LIMIT = "STOPLOSS_LIMIT"

VARIETY_NORMAL  = "NORMAL"
VARIETY_STOPLOSS = "STOPLOSS"
VARIETY_AMO     = "AMO"

DURATION_DAY  = "DAY"
DURATION_IOC  = "IOC"

# Interval mapping → Angel One candle interval strings
INTERVAL_MAP = {
    "1minute":  "ONE_MINUTE",
    "3minute":  "THREE_MINUTE",
    "5minute":  "FIVE_MINUTE",
    "10minute": "TEN_MINUTE",
    "15minute": "FIFTEEN_MINUTE",
    "30minute": "THIRTY_MINUTE",
    "1hour":    "ONE_HOUR",
    "1day":     "ONE_DAY",
}


class AngelOneBroker:
    """
    Angel One SmartAPI adapter.
    Identical public interface to GrowwBroker and UpstoxBroker.
    bot_engine.py needs ZERO changes — just swap the broker class.
    """

    def __init__(self,
                 paper_trading: bool = True):
        """
        Parameters
        ----------
        api_key      : SmartAPI API key from smartapi.angelbroking.com
        client_id    : Angel One login ID (e.g. "A12345")
        password     : Angel One 4-digit MPIN
        totp_secret  : Base32 TOTP secret from Angel One TOTP setup
        paper_trading: If True, orders are simulated (no real money)
        """
        self.api_key       = "JGMUJJ4n"
        self.client_id     = "S62103272"
        self.password      = "5763"
        self.totp_secret   = "OJOAT3LK5KW5M3U4LXJSHHZSDA"
        self.paper_trading = paper_trading

        self.smart_api      = None
        self.auth_token     = None
        self.refresh_token  = None
        self.feed_token     = None
        self._order_counter = 2000
        self._session_date  = None

        if not SMARTAPI_AVAILABLE:
            logger.error("Install SmartApi: pip install smartapi-python pyotp logzero")
            return

        self._authenticate()
        self._load_instrument_cache()

    # ─────────────────────────────────────────────────────────────────────────
    # AUTHENTICATION
    # ─────────────────────────────────────────────────────────────────────────

    def _authenticate(self) -> bool:
        """Login with TOTP. Session valid until midnight — auto-renews."""
        try:
            totp = pyotp.TOTP(self.totp_secret).now()
            self.smart_api = SmartConnect(self.api_key)
            data = self.smart_api.generateSession(
                self.client_id, self.password, totp
            )

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
        """Re-authenticate if session has expired (new day)."""
        if self._session_date != date.today():
            logger.info("Session expired — re-authenticating...")
            self._authenticate()

    def _refresh_session(self):
        """Refresh JWT using refresh token (lighter than full re-login)."""
        try:
            self.smart_api.generateToken(self.refresh_token)
            logger.info("Session refreshed via refresh token")
        except Exception:
            self._authenticate()

    # ─────────────────────────────────────────────────────────────────────────
    # INSTRUMENT CACHE
    # ─────────────────────────────────────────────────────────────────────────

    def _load_instrument_cache(self):
        """
        Download Angel One's full instrument master (all NSE/NFO symbols).
        Cached in memory for the day — called once on startup.
        """
        global _INSTRUMENT_CACHE, _INSTRUMENT_CACHE_DATE
        if _INSTRUMENT_CACHE_DATE == date.today() and _INSTRUMENT_CACHE:
            return  # Already loaded today

        try:
            logger.info("📥 Loading Angel One instrument master...")
            resp = requests.get(INSTRUMENT_URL, timeout=30)
            instruments = resp.json()

            _INSTRUMENT_CACHE = {}
            for inst in instruments:
                key = f"{inst.get('exch_seg', '')}:{inst.get('symbol', '')}"
                _INSTRUMENT_CACHE[key] = inst
                # Also index by trading symbol for quick lookup
                ts = inst.get("symbol", "")
                _INSTRUMENT_CACHE[ts] = inst

            _INSTRUMENT_CACHE_DATE = date.today()
            logger.info(f"✅ Instrument cache loaded: {len(instruments):,} instruments")

        except Exception as e:
            logger.error(f"Instrument cache load error: {e}")

    def _get_token(self, trading_symbol: str, exchange: str = "NFO") -> Optional[str]:
        """Get Angel One symbol token for a trading symbol."""
        self._load_instrument_cache()
        key = f"{exchange}:{trading_symbol}"
        inst = _INSTRUMENT_CACHE.get(key) or _INSTRUMENT_CACHE.get(trading_symbol)
        if inst:
            return str(inst.get("token", ""))
        logger.warning(f"Token not found: {trading_symbol} on {exchange}")
        return None

    def _search_option_token(self, underlying: str, strike: int,
                              option_type: str, expiry_str: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Find trading_symbol + token for an option contract.
        expiry_str: "2025-06-26" format
        Returns (trading_symbol, token)
        """
        try:
            # Angel One NFO symbol format: NIFTY26JUN2524500CE
            dt = datetime.strptime(expiry_str, "%Y-%m-%d")
            mon = dt.strftime("%b").upper()         # JUN
            yy  = dt.strftime("%y")                 # 25
            dd  = dt.strftime("%d")                 # 26
            symbol = f"{underlying}{dd}{mon}{yy}{strike}{option_type}"

            token = self._get_token(symbol, "NFO")
            if token:
                return symbol, token

            # Try alternate format: NIFTY2562624500CE (compact)
            mm = dt.strftime("%m")
            symbol2 = f"{underlying}{yy}{mm}{dd}{strike}{option_type}"
            token2 = self._get_token(symbol2, "NFO")
            if token2:
                return symbol2, token2

            logger.warning(f"Option symbol not found: {symbol}")
            return None, None

        except Exception as e:
            logger.error(f"Option search error: {e}")
            return None, None

    # ─────────────────────────────────────────────────────────────────────────
    # MARKET DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """
        Last Traded Price.
        instrument_key: "NSE_INDEX|Nifty 50" / "NSE_INDEX|Nifty Bank"
                        or a trading symbol like "NIFTY26JUN2524500CE"
        """
        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return None

            resp = self.smart_api.ltpData(exchange, symbol, token)
            if resp and resp.get("status"):
                return float(resp["data"].get("ltp", 0)) or None
            return None

        except Exception as e:
            logger.error(f"LTP error [{instrument_key}]: {e}")
            return None

    def get_ohlcv(self, instrument_key: str, interval: str = "30minute",
                  days: int = 30) -> pd.DataFrame:
        """
        Historical OHLCV candles from Angel One SmartAPI.
        interval: "1minute", "5minute", "15minute", "30minute", "1hour", "1day"
        """
        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return pd.DataFrame()

            angel_interval = INTERVAL_MAP.get(interval, "THIRTY_MINUTE")
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
            to_date   = datetime.now().strftime("%Y-%m-%d %H:%M")

            params = {
                "exchange":    exchange,
                "symboltoken": token,
                "interval":    angel_interval,
                "fromdate":    from_date,
                "todate":      to_date,
            }
            
            logger.info("Candle params: %s", params)
            resp = self.smart_api.getCandleData(params)
            logger.info("Candle response: %s", resp)
            if not resp or not resp.get("status"):
                logger.warning(f"No candle data for {instrument_key}: {resp}")
                return pd.DataFrame()

            candles = resp.get("data", [])
            if not candles:
                return pd.DataFrame()

            df = pd.DataFrame(candles,
                              columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            df = df.sort_index()
            df[["open","high","low","close","volume"]] = \
                df[["open","high","low","close","volume"]].astype(float)
            return df

        except Exception as e:
            logger.error(f"OHLCV error [{instrument_key}]: {e}")
            return pd.DataFrame()

    def get_option_chain(self, instrument_key: str, expiry: str) -> List[Dict]:
        """
        Full option chain for a given expiry.
        Uses Angel One's market data API to build chain from instrument master.
        """
        self._ensure_session()
        try:
            underlying = self._underlying_from_key(instrument_key)
            chain = []

            # Get all option strikes for this expiry from instrument cache
            expiry_formatted = self._format_expiry_for_cache(expiry)
            strikes_seen = set()

            for key, inst in _INSTRUMENT_CACHE.items():
                sym = inst.get("symbol", "")
                if (inst.get("exch_seg") == "NFO" and
                    underlying in sym and
                    expiry_formatted in sym):

                    strike = inst.get("strike", 0)
                    try:
                        strike = int(float(strike)) // 100  # Angel stores as strike*100
                    except Exception:
                        continue

                    opt_type = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else None
                    if not opt_type or strike in strikes_seen:
                        continue
                    strikes_seen.add((strike, opt_type))

                    token = inst.get("token", "")
                    ltp_resp = self.smart_api.ltpData("NFO", sym, str(token))
                    ltp = 0
                    oi  = 0
                    iv  = 0
                    if ltp_resp and ltp_resp.get("status"):
                        d = ltp_resp.get("data", {})
                        ltp = float(d.get("ltp", 0))

                    chain.append({
                        "strike_price":  strike,
                        "option_type":   opt_type,
                        "trading_symbol": sym,
                        "token":         token,
                        "ltp":           ltp,
                        "oi":            oi,
                        "iv":            iv,
                    })

            # Sort by strike
            chain.sort(key=lambda x: x["strike_price"])
            return chain

        except Exception as e:
            logger.error(f"Option chain error: {e}")
            return []

    def get_option_expiries(self, instrument_key: str) -> List[str]:
        """Available expiry dates for an underlying, sorted ascending."""
        try:
            underlying = self._underlying_from_key(instrument_key)
            expiry_set = set()

            for key, inst in _INSTRUMENT_CACHE.items():
                if (inst.get("exch_seg") == "NFO" and
                    underlying in inst.get("symbol", "") and
                    inst.get("instrumenttype") in ("OPTIDX", "OPTSTK")):
                    exp = inst.get("expiry", "")
                    if exp:
                        # Convert DDMMMYYYY → YYYY-MM-DD
                        try:
                            dt = datetime.strptime(exp, "%d%b%Y")
                            expiry_set.add(dt.strftime("%Y-%m-%d"))
                        except Exception:
                            pass

            return sorted(expiry_set)

        except Exception as e:
            logger.error(f"Expiry fetch error: {e}")
            return []

    def get_india_vix(self) -> Optional[float]:
        """India VIX level via Angel One."""
        self._ensure_session()
        try:
            # India VIX token = 13 on NSE
            resp = self.smart_api.ltpData("NSE", "India VIX", "13")
            if resp and resp.get("status"):
                return float(resp["data"].get("ltp", 0)) or None
        except Exception as e:
            logger.error(f"VIX error: {e}")
        return None

    def get_funds(self) -> Dict:
        """Available margin, cash, and fund details."""
        self._ensure_session()
        try:
            resp = self.smart_api.rmsLimit()
            if resp and resp.get("status"):
                return resp.get("data", {})
        except Exception as e:
            logger.error(f"Funds error: {e}")
        return {}

    def get_positions(self) -> List[Dict]:
        """Current open positions (intraday + carryforward)."""
        self._ensure_session()
        try:
            resp = self.smart_api.position()
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception as e:
            logger.error(f"Positions error: {e}")
        return []

    def get_holdings(self) -> List[Dict]:
        """Long-term holdings."""
        self._ensure_session()
        try:
            resp = self.smart_api.holding()
            if resp and resp.get("status"):
                return resp.get("data", {}).get("holdings", []) or []
        except Exception:
            return []

    def get_order_book(self) -> List[Dict]:
        """Today's full order list."""
        self._ensure_session()
        try:
            resp = self.smart_api.orderBook()
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception:
            return []

    def get_trade_book(self) -> List[Dict]:
        """Today's executed trades."""
        self._ensure_session()
        try:
            resp = self.smart_api.tradeBook()
            if resp and resp.get("status"):
                return resp.get("data", []) or []
        except Exception:
            return []

    def get_profile(self) -> Dict:
        """User profile details."""
        self._ensure_session()
        try:
            resp = self.smart_api.getProfile(self.refresh_token)
            if resp and resp.get("status"):
                return resp.get("data", {})
        except Exception:
            return {}
        return {}

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, instrument_key: str, qty: int,
                    order_type: str = "MARKET", price: float = 0,
                    transaction_type: str = "BUY") -> Dict:
        """
        Place a BUY or SELL order.
        instrument_key: trading symbol like "NIFTY26JUN2524500CE"
                        or internal key like "NSE_INDEX|Nifty 50"
        """
        if self.paper_trading:
            return self._paper_order(instrument_key, qty, transaction_type, price)

        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return {"error": f"Token not found for {instrument_key}"}

            # Determine segment product
            product = PRODUCT_INTRADAY if exchange in ("NFO", "NSE") else PRODUCT_DELIVERY
            angel_order_type = ORDER_MARKET if order_type == "MARKET" else ORDER_LIMIT

            order_params = {
                "variety":         VARIETY_NORMAL,
                "tradingsymbol":   symbol,
                "symboltoken":     token,
                "transactiontype": transaction_type,       # "BUY" / "SELL"
                "exchange":        exchange,
                "ordertype":       angel_order_type,
                "producttype":     product,
                "duration":        DURATION_DAY,
                "price":           str(price) if order_type == "LIMIT" else "0",
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(qty),
            }

            resp = self.smart_api.placeOrder(order_params)
            if resp and resp.get("status"):
                oid = resp.get("data", {}).get("orderid", "")
                logger.info(f"✅ Order placed: {transaction_type} {qty} {symbol} → OrderID: {oid}")
                return {"order_id": oid, "status": "complete"}
            else:
                logger.error(f"Order failed: {resp}")
                return {"error": str(resp)}

        except Exception as e:
            logger.error(f"Place order exception: {e}")
            return {"error": str(e)}

    def place_sl_order(self, instrument_key: str, qty: int,
                       trigger_price: float, price: float) -> Dict:
        """Stop-loss sell order — triggered when LTP hits trigger_price."""
        if self.paper_trading:
            return {"order_id": f"PAPER_SL_{self._order_counter}"}

        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return {}

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

            resp = self.smart_api.placeOrder(order_params)
            if resp and resp.get("status"):
                return {"order_id": resp["data"].get("orderid", "")}
            return {"error": str(resp)}

        except Exception as e:
            logger.error(f"SL order error: {e}")
            return {"error": str(e)}

    def place_bracket_order(self, instrument_key: str, qty: int,
                             entry_price: float, sl_price: float,
                             target_price: float) -> Dict:
        """
        Bracket Order — Angel One supports this natively.
        Entry + SL + Target in a single order. Auto-manages exit.
        """
        if self.paper_trading:
            return {"order_id": f"PAPER_BO_{self._order_counter}"}

        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return {}

            sl_points     = round(entry_price - sl_price, 2)
            target_points = round(target_price - entry_price, 2)

            order_params = {
                "variety":         "ROBO",              # Bracket order variety
                "tradingsymbol":   symbol,
                "symboltoken":     token,
                "transactiontype": "BUY",
                "exchange":        exchange,
                "ordertype":       ORDER_LIMIT,
                "producttype":     PRODUCT_INTRADAY,
                "duration":        DURATION_DAY,
                "price":           str(entry_price),
                "squareoff":       str(target_points),  # Points above entry
                "stoploss":        str(sl_points),      # Points below entry
                "quantity":        str(qty),
                "triggerprice":    "0",
            }

            resp = self.smart_api.placeOrder(order_params)
            if resp and resp.get("status"):
                return {"order_id": resp["data"].get("orderid", "")}
            return {"error": str(resp)}

        except Exception as e:
            logger.error(f"Bracket order error: {e}")
            return {"error": str(e)}

    def place_gtt_order(self, instrument_key: str, qty: int,
                        trigger_price: float, limit_price: float,
                        transaction_type: str = "SELL") -> Dict:
        """
        GTT (Good Till Trigger) order — survives across sessions.
        Use for long-term SL/Target orders that don't need daily re-entry.
        """
        if self.paper_trading:
            return {"order_id": f"PAPER_GTT_{self._order_counter}"}

        self._ensure_session()
        try:
            exchange, symbol, token = self._resolve_instrument(instrument_key)
            if not token:
                return {}

            ltp = self.get_ltp(instrument_key) or limit_price

            gtt_params = {
                "tradingsymbol": symbol,
                "symboltoken":   token,
                "exchange":      exchange,
                "producttype":   PRODUCT_DELIVERY,
                "transactiontype": transaction_type,
                "price":         str(limit_price),
                "qty":           str(qty),
                "disclosedqty":  str(qty),
                "triggerprice":  str(trigger_price),
                "timeperiod":    "365",             # GTT valid for 1 year
            }

            resp = self.smart_api.gttCreateRule(
                CreateRuleParams=gtt_params,
                ltp=str(ltp)
            )
            if resp and resp.get("status"):
                return {"order_id": resp.get("data", {}).get("id", "")}
            return {"error": str(resp)}

        except Exception as e:
            logger.error(f"GTT order error: {e}")
            return {"error": str(e)}

    def modify_order(self, order_id: str, price: float = None,
                     qty: int = None, order_type: str = None) -> Dict:
        """Modify an existing pending order."""
        if self.paper_trading:
            return {"success": True}

        self._ensure_session()
        try:
            params = {"orderid": order_id, "variety": VARIETY_NORMAL}
            if price:      params["price"] = str(price)
            if qty:        params["quantity"] = str(qty)
            if order_type: params["ordertype"] = order_type

            resp = self.smart_api.modifyOrder(params)
            return resp if resp else {}
        except Exception as e:
            logger.error(f"Modify order error: {e}")
            return {"error": str(e)}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel a pending order."""
        if self.paper_trading:
            return True
        self._ensure_session()
        try:
            resp = self.smart_api.cancelOrder(order_id, variety)
            return bool(resp and resp.get("status"))
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # COMPUTED HELPERS  (same interface as GrowwBroker)
    # ─────────────────────────────────────────────────────────────────────────

    def get_atm_strike(self, instrument_key: str, lot_size: int = 50) -> Optional[int]:
        """Return ATM strike rounded to nearest lot_size multiple."""
        ltp = self.get_ltp(instrument_key)
        if ltp:
            return round(ltp / lot_size) * lot_size
        return None

    def find_option_instrument(self, underlying_key: str, strike: int,
                                option_type: str, expiry: str) -> Optional[str]:
        """
        Find the trading symbol for an option contract.
        Returns the symbol string to pass to place_order().
        """
        underlying = self._underlying_from_key(underlying_key)
        symbol, token = self._search_option_token(underlying, strike, option_type, expiry)
        return symbol  # bot_engine uses this as instrument_key for orders

    def compute_max_pain(self, chain: List[Dict]) -> Optional[int]:
        """Max pain calculation from option chain data."""
        try:
            strikes = {}
            for item in chain:
                s = item.get("strike_price")
                if not s:
                    continue
                if s not in strikes:
                    strikes[s] = {"ce_oi": 0, "pe_oi": 0}
                if item.get("option_type") == "CE":
                    strikes[s]["ce_oi"] = item.get("oi", 0)
                else:
                    strikes[s]["pe_oi"] = item.get("oi", 0)

            pain = {}
            for test_s in strikes:
                total = sum(
                    strikes[s]["ce_oi"] * max(0, test_s - s) +
                    strikes[s]["pe_oi"] * max(0, s - test_s)
                    for s in strikes
                )
                pain[test_s] = total

            return min(pain, key=pain.get) if pain else None
        except Exception:
            return None

    def get_pcr(self, chain: List[Dict]) -> Optional[float]:
        """Put-Call Ratio from Open Interest."""
        try:
            pe_oi = sum(item.get("oi", 0) for item in chain if item.get("option_type") == "PE")
            ce_oi = sum(item.get("oi", 0) for item in chain if item.get("option_type") == "CE")
            return round(pe_oi / ce_oi, 2) if ce_oi else None
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # WEBSOCKET LIVE FEED
    # ─────────────────────────────────────────────────────────────────────────

    def start_live_feed(self, tokens: List[Dict], on_tick_callback):
        """
        Start WebSocket live price feed.
        tokens: [{"exchangeType": 2, "tokens": ["26000", "26009"]}]
          exchangeType: 1=NSE, 2=NFO, 3=BSE, 4=MCX
        callback receives tick dicts with LTP, OI, bid/ask
        """
        if not self.auth_token or not self.feed_token:
            logger.error("Not authenticated for WebSocket feed")
            return

        try:
            def on_data(wsapp, message):
                on_tick_callback(message)

            def on_open(wsapp):
                logger.info("📡 Angel One WebSocket feed connected")
                sws.subscribe("correlationID", 3, tokens)  # mode 3 = SNAP_QUOTE

            def on_error(wsapp, error):
                logger.error(f"WS error: {error}")

            def on_close(wsapp):
                logger.info("WS feed closed")

            sws = SmartWebSocketV2(
                self.auth_token,
                self.api_key,
                self.client_id,
                self.feed_token
            )
            sws.on_open  = on_open
            sws.on_data  = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            sws.connect()

        except Exception as e:
            logger.error(f"WebSocket start error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_instrument(self, key: str) -> Tuple[str, str, Optional[str]]:
        """
        Convert instrument_key → (exchange, trading_symbol, token)
        Handles both internal keys and Angel One symbols.
        """
        mapping = {
            "NSE_INDEX|Nifty 50":          ("NSE", "Nifty 50",   "26000"),
            "NSE_INDEX|Nifty Bank":        ("NSE", "Nifty Bank", "26009"),
            "NSE_INDEX|Nifty Fin Service": ("NSE", "FINNIFTY",   "26037"),
            "NSE_INDEX|India VIX":         ("NSE", "India VIX",  "13"),
            "NIFTY":                       ("NSE", "Nifty 50",   "26000"),
            "BANKNIFTY":                   ("NSE", "Nifty Bank", "26009"),
        }

        if key in mapping:
            return mapping[key]

        # It's an NFO option symbol — look up token
        token = self._get_token(key, "NFO")
        return "NFO", key, token

    def _underlying_from_key(self, key: str) -> str:
        mapping = {
            "NSE_INDEX|Nifty 50":          "NIFTY",
            "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
            "NSE_INDEX|Nifty Fin Service": "FINNIFTY",
        }
        return mapping.get(key, key.split("|")[-1].replace(" ", "").upper())

    def _format_expiry_for_cache(self, expiry_yyyy_mm_dd: str) -> str:
        """Convert "2025-06-26" → "26JUN2025" (Angel One cache format)."""
        try:
            dt = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d")
            return dt.strftime("%d%b%Y").upper()
        except Exception:
            return expiry_yyyy_mm_dd

    def _paper_order(self, symbol: str, qty: int,
                     tx_type: str, price: float) -> Dict:
        self._order_counter += 1
        oid = f"PAPER_AO_{self._order_counter}"
        logger.info(f"📝 PAPER ORDER [AngelOne]: {tx_type} {qty} {symbol} @ ₹{price} → {oid}")
        return {"order_id": oid, "status": "complete"}

    def logout(self):
        """Gracefully logout from SmartAPI session."""
        try:
            self.smart_api.terminateSession(self.client_id)
            logger.info("Angel One session terminated")
        except Exception:
            pass