"""
Groww Broker Adapter
Replaces upstox_broker.py — plug-and-play with the same interface
Uses: pip install growwapi pyotp
Subscription: ₹499/month at groww.in/user/profile/trading-apis
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import pandas as pd

logger = logging.getLogger("GROWW")

# ── Try importing growwapi ────────────────────────────────────────────────────
try:
    from growwapi import GrowwAPI
    import pyotp
    GROWW_AVAILABLE = True
except ImportError:
    GROWW_AVAILABLE = False
    logger.info("growwapi not installed. Run: pip install growwapi pyotp")


class GrowwBroker:
    """
    Drop-in replacement for UpstoxBroker.
    All method signatures are identical — bot_engine.py needs zero changes.

    Auth Method 1 (API Key + Secret) — needs daily approval on Groww Cloud
    Auth Method 2 (TOTP) — no expiry, recommended for bots ✅
    """

    def __init__(self, api_key: str, api_secret: str = None,
                 totp_secret: str = None, paper_trading: bool = True):
        self.paper_trading = paper_trading
        self._order_counter = 1000
        self.groww = None

        if not GROWW_AVAILABLE:
            logger.info("Install growwapi: pip install growwapi pyotp")
            return

        try:
            if totp_secret:
                # ── Recommended: TOTP flow (no daily expiry) ──────────────
                logger.info("Authenticating via TOTP flow...")
                totp_gen = pyotp.TOTP(totp_secret)
                totp = totp_gen.now()
                access_token = GrowwAPI.get_access_token(
                    api_key=api_key, totp=totp
                )
            else:
                # ── API Key + Secret flow ──────────────────────────────────
                logger.info("Authenticating via API Key + Secret flow...")
                access_token = GrowwAPI.get_access_token(
                    api_key=api_key, secret=api_secret
                )

            self.groww = GrowwAPI(access_token)
            logger.info("✅ Groww API authenticated successfully")

        except Exception as e:
            logger.info(f"Groww auth failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # MARKET DATA
    # ─────────────────────────────────────────────────────────────────────────

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """
        Last Traded Price.
        instrument_key: "NIFTY" / "BANKNIFTY" / trading symbol like "NIFTY25JUN24500CE"
        """
        logger.info(f"[GROWW] Fetching LTP for {instrument_key}...")
        if not self.groww:
            logger.info(f"[GROWW] Groww not initialized")
            return None
        try:
            symbol, exchange, segment = self._parse_key(instrument_key)
            resp = self.groww.get_ltp(
                exchange=exchange,
                segment=segment,
                trading_symbol=symbol
            )
            ltp = float(resp.get("last_price", 0)) or None
            logger.info(f"[GROWW] ✅ LTP for {instrument_key}: ₹{ltp:.2f}" if ltp else f"[GROWW] ⚠️ LTP fetch returned None")
            return ltp
        except Exception as e:
            logger.info(f"[GROWW] LTP error [{instrument_key}]: {e}")
            return None

    def get_ohlcv(self, instrument_key: str, interval: str = "30minute",
                  days: int = 30) -> pd.DataFrame:
        """
        Historical OHLCV candles.
        interval mapping: "1minute"→MIN_1, "5minute"→MIN_5, "15minute"→MIN_15,
                          "30minute"→MIN_30, "1hour"→HOUR_1, "1day"→DAY
        """
        if not self.groww:
            return pd.DataFrame()
        try:
            symbol, exchange, segment = self._parse_key(instrument_key)
            candle_interval = self._map_interval(interval)

            end_dt   = datetime.now()
            start_dt = end_dt - timedelta(days=days)

            resp = self.groww.get_historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=self._to_groww_symbol(instrument_key),
                start_time=start_dt.strftime("%Y-%m-%d 09:15:00"),
                end_time=end_dt.strftime("%Y-%m-%d 15:30:00"),
                candle_interval=candle_interval
            )

            candles = resp.get("candles", [])
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
            logger.info(f"OHLCV error [{instrument_key}]: {e}")
            return pd.DataFrame()

    def get_option_chain(self, instrument_key: str, expiry: str) -> List[Dict]:
        """Full option chain for an expiry date."""
        if not self.groww:
            return []
        try:
            underlying = self._underlying_from_key(instrument_key)
            resp = self.groww.get_option_chain(
                exchange=self.groww.EXCHANGE_NSE,
                underlying=underlying,
                expiry_date=expiry          # "2025-06-26"
            )
            return resp.get("option_chain", [])
        except Exception as e:
            logger.info(f"Option chain error: {e}")
            return []

    def get_option_expiries(self, instrument_key: str) -> List[str]:
        """Available expiry dates sorted ascending."""
        if not self.groww:
            return []
        try:
            underlying = self._underlying_from_key(instrument_key)
            resp = self.groww.get_expiries(
                exchange=self.groww.EXCHANGE_NSE,
                underlying_symbol=underlying
            )
            return sorted(resp.get("expiries", []))
        except Exception as e:
            logger.info(f"Expiries error: {e}")
            return []

    def get_india_vix(self) -> Optional[float]:
        """India VIX index level."""
        if not self.groww:
            return None
        try:
            resp = self.groww.get_ltp(
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                trading_symbol="INDIAVIX"
            )
            return float(resp.get("last_price", 0)) or None
        except Exception:
            return None

    def get_greeks(self, trading_symbol: str, underlying: str,
                   expiry: str) -> Dict:
        """
        Live Greeks for an option contract.
        Returns: delta, gamma, theta, vega, rho, iv
        """
        if not self.groww:
            return {}
        try:
            resp = self.groww.get_greeks(
                exchange=self.groww.EXCHANGE_NSE,
                underlying=underlying,
                trading_symbol=trading_symbol,
                expiry=expiry
            )
            return resp.get("greeks", {})
        except Exception as e:
            logger.info(f"Greeks error: {e}")
            return {}

    def get_funds(self) -> Dict:
        """Available margin and funds."""
        if not self.groww:
            return {}
        try:
            resp = self.groww.get_funds()
            return resp or {}
        except Exception as e:
            logger.info(f"Funds error: {e}")
            return {}

    def get_positions(self) -> List[Dict]:
        """Current open positions."""
        if not self.groww:
            return []
        try:
            resp = self.groww.get_positions_for_user(
                segment=self.groww.SEGMENT_FNO
            )
            return resp.get("positions", [])
        except Exception as e:
            logger.info(f"Positions error: {e}")
            return []

    def get_order_book(self) -> List[Dict]:
        """Today's order list."""
        if not self.groww:
            return []
        try:
            resp = self.groww.get_order_list()
            return resp.get("orders", [])
        except Exception as e:
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────────────────

    def place_order(self, instrument_key: str, qty: int,
                    order_type: str = "MARKET", price: float = 0,
                    transaction_type: str = "BUY") -> Dict:
        """
        Place a buy or sell order.
        instrument_key: Groww trading symbol e.g. "NIFTY25JUN24500CE"
        """
        logger.info(f"[GROWW] Placing {transaction_type} order: {qty} {instrument_key} | Type={order_type}, Price=₹{price:.2f}" if price else f"[GROWW] Placing {transaction_type} order: {qty} {instrument_key} | Type={order_type}")
        if self.paper_trading:
            result = self._paper_order(instrument_key, qty, transaction_type, price)
            logger.info(f"[GROWW] 📝 Paper order placed: {result.get('order_id')}")
            return result

        if not self.groww:
            logger.info(f"[GROWW] Groww not initialized, cannot place real order")
            return {}

        try:
            otype = (self.groww.ORDER_TYPE_MARKET if order_type == "MARKET"
                     else self.groww.ORDER_TYPE_LIMIT)
            ttype = (self.groww.TRANSACTION_TYPE_BUY if transaction_type == "BUY"
                     else self.groww.TRANSACTION_TYPE_SELL)

            logger.info(f"[GROWW] Sending order to Groww API...")
            resp = self.groww.place_order(
                trading_symbol=instrument_key,
                quantity=qty,
                validity=self.groww.VALIDITY_DAY,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
                product=self.groww.PRODUCT_MIS,        # Intraday
                order_type=otype,
                transaction_type=ttype,
                price=price if order_type == "LIMIT" else 0,
                order_reference_id=f"BOT-{uuid.uuid4().hex[:10].upper()}"
            )
            return resp or {}
        except Exception as e:
            logger.info(f"Place order error: {e}")
            return {}

    def place_sl_order(self, instrument_key: str, qty: int,
                       trigger_price: float, price: float) -> Dict:
        """Stop-loss sell order."""
        if self.paper_trading:
            return {"order_id": f"PAPER_SL_{self._order_counter}"}
        if not self.groww:
            return {}
        try:
            resp = self.groww.place_order(
                trading_symbol=instrument_key,
                quantity=qty,
                validity=self.groww.VALIDITY_DAY,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
                product=self.groww.PRODUCT_MIS,
                order_type=self.groww.ORDER_TYPE_SL,
                transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                price=price,
                trigger_price=trigger_price,
                order_reference_id=f"SL-{uuid.uuid4().hex[:10].upper()}"
            )
            return resp or {}
        except Exception as e:
            logger.info(f"SL order error: {e}")
            return {}

    def place_oco_order(self, instrument_key: str, qty: int,
                        sl_price: float, target_price: float) -> Dict:
        """
        OCO (One Cancels Other) — Groww supports GTT/OCO orders natively.
        Automatically exits at SL or Target, whichever hits first.
        """
        if self.paper_trading:
            return {"order_id": f"PAPER_OCO_{self._order_counter}"}
        if not self.groww:
            return {}
        try:
            resp = self.groww.place_oco_order(
                trading_symbol=instrument_key,
                quantity=qty,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_FNO,
                product=self.groww.PRODUCT_MIS,
                transaction_type=self.groww.TRANSACTION_TYPE_SELL,
                stop_loss_price=sl_price,
                target_price=target_price,
            )
            return resp or {}
        except Exception as e:
            logger.info(f"OCO order error: {e}")
            return {}

    def cancel_order(self, order_id: str) -> bool:
        if self.paper_trading:
            return True
        if not self.groww:
            return False
        try:
            self.groww.cancel_order(order_id=order_id)
            return True
        except Exception:
            return False

    def modify_order(self, order_id: str, price: float = None,
                     trigger_price: float = None, qty: int = None) -> Dict:
        if self.paper_trading:
            return {"success": True}
        if not self.groww:
            return {}
        try:
            kwargs = {"order_id": order_id}
            if price:          kwargs["price"] = price
            if trigger_price:  kwargs["trigger_price"] = trigger_price
            if qty:            kwargs["quantity"] = qty
            return self.groww.modify_order(**kwargs) or {}
        except Exception as e:
            logger.info(f"Modify order error: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ─────────────────────────────────────────────────────────────────────────

    def get_atm_strike(self, instrument_key: str, lot_size: int = 50) -> Optional[int]:
        ltp = self.get_ltp(instrument_key)
        if ltp:
            return round(ltp / lot_size) * lot_size
        return None

    def find_option_instrument(self, underlying_key: str, strike: int,
                                option_type: str, expiry: str) -> Optional[str]:
        """
        Build the Groww trading symbol for an option contract.
        Format: {UNDERLYING}{YYMONDD}{STRIKE}{CE/PE}
        Example: NIFTY25JUN24500CE
        """
        try:
            underlying = self._underlying_from_key(underlying_key)
            dt = datetime.strptime(expiry, "%Y-%m-%d")
            # Groww format: NIFTY25JUN24500CE
            month_map = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                         7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
            symbol = f"{underlying}{str(dt.year)[-2:]}{month_map[dt.month]}{strike}{option_type}"
            return symbol
        except Exception as e:
            logger.info(f"Symbol build error: {e}")
            return None

    def compute_max_pain(self, chain: List[Dict]) -> Optional[int]:
        try:
            pain = {}
            for item in chain:
                strike = item.get("strike_price")
                if not strike:
                    continue
                ce_oi = item.get("call_data", {}).get("oi", 0)
                pe_oi = item.get("put_data", {}).get("oi", 0)
                pain[strike] = {"ce_oi": ce_oi, "pe_oi": pe_oi}

            total_pain = {}
            for test_s in pain:
                loss = sum(
                    pain[s]["ce_oi"] * max(0, test_s - s) +
                    pain[s]["pe_oi"] * max(0, s - test_s)
                    for s in pain
                )
                total_pain[test_s] = loss

            return min(total_pain, key=total_pain.get) if total_pain else None
        except Exception:
            return None

    def get_pcr(self, chain: List[Dict]) -> Optional[float]:
        try:
            pe_oi = sum(item.get("put_data", {}).get("oi", 0) for item in chain)
            ce_oi = sum(item.get("call_data", {}).get("oi", 0) for item in chain)
            return round(pe_oi / ce_oi, 2) if ce_oi else None
        except Exception:
            return None

    def get_iv_percentile(self, underlying: str, current_iv: float,
                          lookback_days: int = 252) -> float:
        """
        Compute IV Percentile: where is today's IV vs past year?
        Returns 0-100 (lower = cheaper options = better to buy)
        """
        try:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=lookback_days)
            resp = self.groww.get_historical_candles(
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                groww_symbol=f"NSE-{underlying}",
                start_time=start_dt.strftime("%Y-%m-%d 09:15:00"),
                end_time=end_dt.strftime("%Y-%m-%d 15:30:00"),
                candle_interval=self.groww.CANDLE_INTERVAL_DAY
            )
            candles = resp.get("candles", [])
            if not candles:
                return 50.0
            closes = [c[4] for c in candles]
            # Approximate historical vol
            import numpy as np
            returns = np.diff(np.log(closes))
            hist_vols = [np.std(returns[max(0,i-20):i]) * np.sqrt(252) * 100
                         for i in range(20, len(returns))]
            if not hist_vols:
                return 50.0
            below = sum(1 for v in hist_vols if v < current_iv)
            return round(below / len(hist_vols) * 100, 1)
        except Exception:
            return 50.0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _paper_order(self, symbol: str, qty: int,
                     tx_type: str, price: float) -> Dict:
        self._order_counter += 1
        oid = f"PAPER_{self._order_counter}"
        logger.info(f"📝 PAPER ORDER: {tx_type} {qty} {symbol} @ ₹{price} → {oid}")
        return {"order_id": oid, "status": "complete"}

    def _parse_key(self, key: str):
        """Parse instrument_key into (symbol, exchange, segment)."""
        if "INDEX" in key or key in ("NIFTY", "BANKNIFTY", "FINNIFTY", "INDIAVIX"):
            return key.split("|")[-1] if "|" in key else key, \
                   (self.groww.EXCHANGE_NSE if self.groww else "NSE"), \
                   (self.groww.SEGMENT_CASH if self.groww else "CASH")
        return key, \
               (self.groww.EXCHANGE_NSE if self.groww else "NSE"), \
               (self.groww.SEGMENT_FNO if self.groww else "FNO")

    def _underlying_from_key(self, key: str) -> str:
        mapping = {
            "NSE_INDEX|Nifty 50": "NIFTY",
            "NSE_INDEX|Nifty Bank": "BANKNIFTY",
            "NSE_INDEX|Nifty Fin Service": "FINNIFTY",
        }
        return mapping.get(key, key.split("|")[-1].replace(" ", "").upper())

    def _to_groww_symbol(self, key: str) -> str:
        mapping = {
            "NSE_INDEX|Nifty 50": "NSE-NIFTY",
            "NSE_INDEX|Nifty Bank": "NSE-BANKNIFTY",
            "NSE_INDEX|Nifty Fin Service": "NSE-FINNIFTY",
        }
        return mapping.get(key, f"NSE-{key}")

    def _map_interval(self, interval: str):
        if not self.groww:
            return None
        mapping = {
            "1minute":  self.groww.CANDLE_INTERVAL_MIN_1,
            "3minute":  self.groww.CANDLE_INTERVAL_MIN_3,
            "5minute":  self.groww.CANDLE_INTERVAL_MIN_5,
            "10minute": self.groww.CANDLE_INTERVAL_MIN_10,
            "15minute": self.groww.CANDLE_INTERVAL_MIN_15,
            "30minute": self.groww.CANDLE_INTERVAL_MIN_30,
            "1hour":    self.groww.CANDLE_INTERVAL_HOUR_1,
            "1day":     self.groww.CANDLE_INTERVAL_DAY,
        }
        return mapping.get(interval, self.groww.CANDLE_INTERVAL_MIN_15)
