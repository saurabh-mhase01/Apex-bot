"""
Upstox Broker Interface — Data fetching + Order execution
"""
import logging
import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import pandas as pd

logger = logging.getLogger("UPSTOX")

UPSTOX_BASE = "https://api.upstox.com/v2"

# Instrument keys
NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANKNIFTY_KEY = "NSE_INDEX|Nifty Bank"
FINNIFTY_KEY = "NSE_INDEX|Nifty Fin Service"


class UpstoxBroker:
    def __init__(self, access_token: str, paper_trading: bool = True):
        self.access_token = access_token
        self.paper_trading = paper_trading
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._order_counter = 1000  # For paper trading IDs

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{UPSTOX_BASE}{endpoint}"
        r = requests.get(url, headers=self.headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        logger.error(f"GET {endpoint} failed: {r.status_code} {r.text}")
        return {}

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f"{UPSTOX_BASE}{endpoint}"
        r = requests.post(url, headers=self.headers, json=data, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        logger.error(f"POST {endpoint} failed: {r.status_code} {r.text}")
        return {}

    # ── Market Data ────────────────────────────────────────────
    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """Last traded price"""
        try:
            resp = self._get("/market-quote/ltp", {"instrument_key": instrument_key})
            data = resp.get("data", {})
            key = list(data.keys())[0] if data else None
            return data[key]["last_price"] if key else None
        except Exception as e:
            logger.error(f"LTP error: {e}")
            return None

    def get_ohlcv(self, instrument_key: str, interval: str = "30minute", days: int = 30) -> pd.DataFrame:
        """Historical OHLCV candles"""
        try:
            to_date = datetime.now().strftime("%Y-%m-%d")
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            resp = self._get(
                f"/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
            )
            candles = resp.get("data", {}).get("candles", [])
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            df = df.sort_index()
            df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
            return df
        except Exception as e:
            logger.error(f"OHLCV error: {e}")
            return pd.DataFrame()

    def get_option_chain(self, instrument_key: str, expiry: str) -> Dict:
        """Full option chain for given expiry"""
        try:
            resp = self._get("/option/chain", {
                "instrument_key": instrument_key,
                "expiry_date": expiry
            })
            return resp.get("data", {})
        except Exception as e:
            logger.error(f"Option chain error: {e}")
            return {}

    def get_option_expiries(self, instrument_key: str) -> List[str]:
        """Get available expiry dates"""
        try:
            resp = self._get("/option/contract", {"instrument_key": instrument_key})
            expiries = list(set([
                c["expiry"] for c in resp.get("data", [])
            ]))
            return sorted(expiries)
        except Exception as e:
            logger.error(f"Expiry fetch error: {e}")
            return []

    def get_funds(self) -> Dict:
        """Available margin and funds"""
        try:
            resp = self._get("/user/get-funds-and-margin", {"segment": "SEC"})
            return resp.get("data", {})
        except Exception as e:
            logger.error(f"Funds error: {e}")
            return {}

    def get_positions(self) -> List[Dict]:
        """Current open positions"""
        try:
            resp = self._get("/portfolio/short-term-positions")
            return resp.get("data", [])
        except Exception as e:
            logger.error(f"Positions error: {e}")
            return []

    def get_holdings(self) -> List[Dict]:
        """Portfolio holdings"""
        try:
            resp = self._get("/portfolio/long-term-holdings")
            return resp.get("data", [])
        except Exception as e:
            return []

    def get_order_book(self) -> List[Dict]:
        """Today's orders"""
        try:
            resp = self._get("/order/retrieve-all")
            return resp.get("data", [])
        except Exception as e:
            return []

    def get_india_vix(self) -> Optional[float]:
        """India VIX level"""
        try:
            resp = self._get("/market-quote/ltp", {"instrument_key": "NSE_INDEX|India VIX"})
            data = resp.get("data", {})
            key = list(data.keys())[0] if data else None
            return data[key]["last_price"] if key else None
        except Exception:
            return None

    def find_option_instrument(self, underlying: str, strike: int,
                                option_type: str, expiry: str) -> Optional[str]:
        """Find the instrument key for a specific option contract"""
        try:
            resp = self._get("/option/contract", {"instrument_key": underlying})
            for c in resp.get("data", []):
                if (c["strike_price"] == strike and
                    c["option_type"] == option_type and
                    c["expiry"] == expiry):
                    return c["instrument_key"]
        except Exception as e:
            logger.error(f"Instrument search error: {e}")
        return None

    # ── Order Management ───────────────────────────────────────
    def place_order(self, instrument_key: str, qty: int,
                    order_type: str = "MARKET", price: float = 0,
                    transaction_type: str = "BUY") -> Dict:
        if self.paper_trading:
            return self._paper_order(instrument_key, qty, transaction_type, price)

        payload = {
            "quantity": qty,
            "product": "D",
            "validity": "DAY",
            "price": price,
            "tag": "ai_options_bot",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False
        }
        resp = self._post("/order/place", payload)
        return resp.get("data", {})

    def cancel_order(self, order_id: str) -> bool:
        if self.paper_trading:
            return True
        resp = self._get(f"/order/cancel?order_id={order_id}")
        return bool(resp.get("data"))

    def place_sl_order(self, instrument_key: str, qty: int,
                       trigger_price: float, price: float) -> Dict:
        """Stop-loss sell order"""
        if self.paper_trading:
            return {"order_id": f"PAPER_SL_{self._order_counter}"}
        payload = {
            "quantity": qty,
            "product": "D",
            "validity": "DAY",
            "price": price,
            "instrument_token": instrument_key,
            "order_type": "SL",
            "transaction_type": "SELL",
            "trigger_price": trigger_price,
            "disclosed_quantity": 0,
            "is_amo": False
        }
        resp = self._post("/order/place", payload)
        return resp.get("data", {})

    def _paper_order(self, instrument_key: str, qty: int,
                     transaction_type: str, price: float) -> Dict:
        self._order_counter += 1
        oid = f"PAPER_{self._order_counter}"
        logger.info(f"📝 PAPER ORDER: {transaction_type} {qty} {instrument_key} @ {price} → {oid}")
        return {"order_id": oid, "status": "complete"}

    # ── Computed Helpers ───────────────────────────────────────
    def get_atm_strike(self, underlying_key: str, lot_size: int = 50) -> Optional[int]:
        ltp = self.get_ltp(underlying_key)
        if ltp:
            return round(ltp / lot_size) * lot_size
        return None

    def compute_max_pain(self, chain: Dict) -> Optional[int]:
        """Max pain calculation from options chain"""
        try:
            strike_data = {}
            for item in chain:
                strike = item["strike_price"]
                ce_oi = item.get("call_options", {}).get("market_data", {}).get("oi", 0)
                pe_oi = item.get("put_options", {}).get("market_data", {}).get("oi", 0)
                strike_data[strike] = {"ce_oi": ce_oi, "pe_oi": pe_oi}

            pain = {}
            for test_strike in strike_data:
                total_loss = 0
                for s, d in strike_data.items():
                    if s < test_strike:
                        total_loss += d["ce_oi"] * (test_strike - s)
                    elif s > test_strike:
                        total_loss += d["pe_oi"] * (s - test_strike)
                pain[test_strike] = total_loss

            return min(pain, key=pain.get) if pain else None
        except Exception:
            return None

    def get_pcr(self, chain: List[Dict]) -> Optional[float]:
        """Put-Call Ratio from OI"""
        try:
            total_pe_oi = sum(
                item.get("put_options", {}).get("market_data", {}).get("oi", 0)
                for item in chain
            )
            total_ce_oi = sum(
                item.get("call_options", {}).get("market_data", {}).get("oi", 0)
                for item in chain
            )
            return round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else None
        except Exception:
            return None
