"""
Bot Engine — Central orchestrator
Connects: Data → Regime → Strategy → Risk → Execution → Learning
"""
import json
import logging
import uuid
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List

from core.config import Config
from core.regime_classifier import MarketRegimeClassifier
from core.risk_guard import RiskGuard
from data.angelone_broker import AngelOneBroker
# from data.groww_broker import GrowwBroker
from strategies.strategy_engine import StrategyEngine, BUY_CE, BUY_PE, NO_TRADE
from db.database import Database
from alerts.telegram_alert import TelegramAlerter

logger = logging.getLogger("ENGINE")


class BotEngine:
    def __init__(self, config: Config, db: Database, alerter: TelegramAlerter):
        self.config = config
        self.db = db
        self.alerter = alerter

        # Initialize AngelOneBroker with paper trading enabled
        self.broker = AngelOneBroker(paper_trading=config.paper_trading)
        
        self.regime_classifier = MarketRegimeClassifier()
        self.strategy_engine = StrategyEngine(config.strategy_weights)
        self.risk_guard = RiskGuard(config, db)

        self.active_regime = "RANGE_BOUND"
        self.regime_confidence = 0.5
        self.capital = config.total_capital
        self._oi_snapshot_prev: Dict = {}
        self._bot_active = True

        # Load weights from DB if they exist
        saved_weights = db.get_setting("strategy_weights")
        if saved_weights:
            self.strategy_engine.weights = saved_weights

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def pre_market_analysis(self):
        logger.info("🌅 Pre-market analysis starting...")
        logger.info("[PRE_MARKET] Fetching India VIX...")
        try:
            vix = self.broker.get_india_vix()
            vix_display = f"{vix:.2f}" if vix else "N/A"
            if vix:
                logger.info(f"[PRE_MARKET] VIX fetched: {vix:.2f}")
                logger.info(f"[PRE_MARKET] India VIX: {vix:.2f}")
                self.db.log_regime({"timestamp": str(datetime.now()), "instrument": "VIX",
                                    "regime": "PRE_MARKET", "vix": vix, "confidence": 0})
                logger.info(f"[PRE_MARKET] VIX logged to database")
            else:
                logger.info(f"[PRE_MARKET] VIX fetch returned None")
            self.alerter.send(f"🌅 Pre-market: VIX={vix_display} | Bot ready")
            logger.info(f"[PRE_MARKET] Telegram alert sent")
        except Exception as e:
            logger.info(f"[PRE_MARKET] Error: {e}", exc_info=True)

    def compute_daily_regime(self):
        logger.info("🧠 Computing daily market regime...")
        logger.info(f"[REGIME] Processing {len(self.config.instruments)} instruments...")
        for instrument_key in self.config.instruments:
            try:
                logger.info(f"[REGIME] Fetching 30-minute OHLCV for {instrument_key} (10 days)...")
                df = self.broker.get_ohlcv(instrument_key, "30minute", days=10)
                if df.empty:
                    logger.info(f"[REGIME] No data for {instrument_key}, skipping")
                    continue
                logger.info(f"[REGIME] ✅ Got {len(df)} candles")
                
                logger.info(f"[REGIME] Fetching VIX...")
                vix = self.broker.get_india_vix() or 15.0
                logger.info(f"[REGIME] VIX: {vix:.2f}")
                
                logger.info(f"[REGIME] Running regime classification...")
                regime, confidence, features = self.regime_classifier.classify(df, vix)
                logger.info(f"[REGIME] Classification result: {regime} (confidence: {confidence:.0%})")
                logger.info(f"[REGIME] Features: ADX={features.get('adx', 0):.1f}, RSI={features.get('rsi', 0):.1f}, PCR={features.get('pcr', 1.0):.2f}")
                
                self.active_regime = regime
                self.regime_confidence = confidence
                self.db.log_regime({
                    "timestamp": str(datetime.now()),
                    "instrument": instrument_key,
                    "regime": regime,
                    "confidence": confidence,
                    "adx": features.get("adx", 0),
                    "rsi": features.get("rsi", 50),
                    "vix": vix,
                    "pcr": features.get("pcr", 1.0),
                    "ema_cross": str(features.get("ema_cross", 0)),
                })
                logger.info(f"[REGIME] Regime logged to database")
                logger.info(f"[REGIME] ✅ {instrument_key}: {regime} ({confidence:.0%}) | ADX={features.get('adx', 0):.1f} RSI={features.get('rsi', 50):.1f} VIX={vix:.2f}")
            except Exception as e:
                logger.info(f"[REGIME] Error for {instrument_key}: {e}", exc_info=True)

    def check_signals(self):
        logger.info("[SIGNAL_CHECK] Starting signal check cycle...")
        if not self._bot_active:
            logger.info("[SIGNAL_CHECK] Bot is inactive, skipping")
            return
        if not self.config.auto_trade and not self.config.paper_trading:
            logger.info("[SIGNAL_CHECK] Neither auto_trade nor paper_trading enabled")
            return

        logger.info(f"[SIGNAL_CHECK] Checking {len(self.config.instruments)} instruments...")
        for instrument_key in self.config.instruments:
            try:
                logger.info(f"[SIGNAL_CHECK] Evaluating {instrument_key}...")
                self._evaluate_instrument(instrument_key)
            except Exception as e:
                logger.info(f"Signal check error for {instrument_key}: {e}", exc_info=True)

    def _evaluate_instrument(self, instrument_key: str):
        logger.info(f"[EVAL] Starting evaluation for {instrument_key}")
        
        # Fetch OHLCV data
        logger.info(f"[EVAL] Fetching 15-minute OHLCV data (3 days)...")
        df = self.broker.get_ohlcv(instrument_key, "15minute", days=3)
        if df is None or len(df) < 20:
            logger.info(f"[EVAL] Insufficient data: {len(df) if df is not None else 0} candles (need 20+)")
            return
        logger.info(f"[EVAL] ✅ Got {len(df)} candles")

        # Fetch VIX
        logger.info(f"[EVAL] Fetching India VIX...")
        vix = self.broker.get_india_vix() or 15.0
        logger.info(f"[EVAL] VIX: {vix:.2f}")
        
        # Fetch expiries
        logger.info(f"[EVAL] Fetching option expiries...")
        expiries = self.broker.get_option_expiries(instrument_key)
        expiry = expiries[0] if expiries else None
        if not expiry:
            logger.info(f"[EVAL] No available expiries found")
            return
        logger.info(f"[EVAL] Using expiry: {expiry}")

        # Fetch option chain
        logger.info(f"[EVAL] Fetching option chain...")
        chain = self.broker.get_option_chain(instrument_key, expiry)
        chain_list = chain if isinstance(chain, list) else []
        logger.info(f"[EVAL] ✅ Got {len(chain_list)} option contracts")

        # Build context for strategies
        logger.info(f"[EVAL] Building strategy context...")
        oi_snap_now = self._build_oi_snapshot(chain_list)
        context = {
            "vix": vix,
            "vix_prev": vix,  # Would store previous VIX in production
            "iv_percentile": 40,  # Would compute from history
            "atm_delta": 0.50,
            "theta_pct": 0.08,
            "skew": 0,
            "chain_snapshot_prev": self._oi_snapshot_prev.get(instrument_key, {}),
            "chain_snapshot_now": oi_snap_now,
            "days_to_expiry": 3,
        }
        logger.info(f"[EVAL] Context prepared: VIX={vix:.2f}, OI_strikes={len(oi_snap_now)}")

        # Update OI snapshot
        self._oi_snapshot_prev[instrument_key] = oi_snap_now

        # Run strategy engine
        logger.info(f"[EVAL] Running strategy evaluation...")
        signal, score, details = self.strategy_engine.evaluate(df, context)
        logger.info(f"[EVAL] Strategy evaluation complete: score={score:.2%}, signal={signal}")

        # Log signal details
        signal_type = "BUY_CE" if signal == BUY_CE else "BUY_PE" if signal == BUY_PE else "NO_TRADE"
        strategies_summary = {k: {"signal": v["signal"], "confidence": v["confidence"]} for k, v in details.items()}
        logger.info(f"[EVAL] Signal details: {signal_type}, Score: {score:.2%}")
        logger.info(f"[EVAL] Strategy breakdown: {json.dumps(strategies_summary, indent=2)}")
        
        self.db.insert_signal({
            "timestamp": str(datetime.now()),
            "instrument": instrument_key,
            "signal_type": signal_type,
            "score": score,
            "strategies": json.dumps(strategies_summary),
            "regime": self.active_regime,
            "confidence": score,
        })
        logger.info(f"[EVAL] Signal saved to database")

        if signal == NO_TRADE:
            logger.info(f"[EVAL] Signal is NO_TRADE, skipping execution")
            return
        
        if score < 0.38:
            logger.info(f"[EVAL] Score {score:.2%} below threshold (0.38), skipping execution")
            return
        
        logger.info(f"[EVAL] ✅ Valid signal: {signal_type} with score {score:.2%}")

        self._execute_trade(instrument_key, signal, score, details, df, expiry)

    def _execute_trade(self, instrument_key: str, signal: int, score: float,
                       details: Dict, df, expiry: str):
        logger.info(f"[TRADE] Starting trade execution for {instrument_key}...")
        
        # Determine strike
        logger.info(f"[TRADE] Fetching LTP for {instrument_key}...")
        ltp = self.broker.get_ltp(instrument_key)
        if not ltp:
            logger.info(f"[TRADE] Could not fetch LTP, aborting trade")
            return
        logger.info(f"[TRADE] LTP: ₹{ltp:.2f}")

        lot_size = 50 if "Nifty 50" in instrument_key else 25
        logger.info(f"[TRADE] Lot size: {lot_size}")
        
        atm = round(ltp / lot_size) * lot_size
        otm_offset = lot_size  # 1 strike OTM
        strike = atm + (otm_offset if signal == BUY_CE else -otm_offset)
        option_type = "CE" if signal == BUY_CE else "PE"
        logger.info(f"[TRADE] Strike calculation: ATM={atm}, Strike={strike}, Type={option_type}")

        # Find option instrument key
        logger.info(f"[TRADE] Finding option instrument: {strike}{option_type} {expiry}")
        opt_key = self.broker.find_option_instrument(instrument_key, strike, option_type, expiry)
        if not opt_key:
            logger.info(f"[TRADE] Could not find option instrument for {strike}{option_type} {expiry}")
            return
        logger.info(f"[TRADE] ✅ Option found: {opt_key}")

        # Get option premium
        logger.info(f"[TRADE] Fetching premium for {opt_key}...")
        premium = self.broker.get_ltp(opt_key)
        if not premium:
            logger.info(f"[TRADE] Could not fetch premium, aborting")
            return
        logger.info(f"[TRADE] Premium: ₹{premium:.2f}")

        # Qty calculation
        logger.info(f"[TRADE] Calculating position size...")
        qty = max(1, self.risk_guard.position_size(self.capital, score, lot_size)) * lot_size
        logger.info(f"[TRADE] Position size: {qty} contracts")

        # Risk validation
        logger.info(f"[TRADE] Running risk validation checks...")
        approved, reason = self.risk_guard.validate(self.capital, premium, qty, score)
        if not approved:
            logger.info(f"[TRADE] ❌ Trade rejected: {reason}")
            return
        logger.info(f"[TRADE] ✅ Risk checks passed")

        # SL/Target calculation
        logger.info(f"[TRADE] Calculating SL and Target levels...")
        levels = self.risk_guard.calculate_sl_target(premium, signal, self.active_regime)
        logger.info(f"[TRADE] SL: ₹{levels['sl_price']:.2f}, T1: ₹{levels['target1_price']:.2f}, T2: ₹{levels['target2_price']:.2f}")

        # Place order
        logger.info(f"[TRADE] Placing order: BUY {qty} {opt_key} @ ₹{premium}...")
        order = self.broker.place_order(opt_key, qty, transaction_type="BUY")
        if not order:
            logger.info(f"[TRADE] Order placement failed")
            return
        logger.info(f"[TRADE] ✅ Order placed: {order.get('order_id')}")

        trade_id = str(uuid.uuid4())[:8].upper()
        strategies_voted = [k for k, v in details.items() if v["signal"] == signal]

        trade = {
            "trade_id": trade_id,
            "instrument": instrument_key,
            "strike": strike,
            "option_type": option_type,
            "expiry": expiry,
            "action": "BUY",
            "qty": qty,
            "entry_price": premium,
            "entry_time": str(datetime.now()),
            "sl_price": levels["sl_price"],
            "target1_price": levels["target1_price"],
            "target2_price": levels["target2_price"],
            "status": "OPEN",
            "strategies_voted": json.dumps(strategies_voted),
            "confidence_score": score,
            "regime": self.active_regime,
            "upstox_order_id": order.get("order_id", ""),
            "paper_trade": 1 if self.config.paper_trading else 0,
            "market_conditions": json.dumps({"vix": self.broker.get_india_vix(), "regime": self.active_regime}),
        }

        self.db.insert_trade(trade)
        logger.info(f"[TRADE] Trade record saved to database")
        logger.info(f"✅ [TRADE] EXECUTED: {trade_id} | {qty}x {instrument_key} {strike}{option_type} @ ₹{premium} | SL:₹{levels['sl_price']} T1:₹{levels['target1_price']} T2:₹{levels['target2_price']}")

        # Alert
        mode = "📝 PAPER" if self.config.paper_trading else "🔴 LIVE"
        voted_str = ", ".join(strategies_voted[:4])
        self.alerter.send(
            f"{mode} TRADE ENTRY\n"
            f"ID: {trade_id}\n"
            f"{'Nifty' if 'Nifty 50' in instrument_key else 'BankNifty'} {strike}{option_type} {expiry}\n"
            f"Premium: ₹{premium} | Qty: {qty}\n"
            f"SL: ₹{levels['sl_price']} | T1: ₹{levels['target1_price']} | T2: ₹{levels['target2_price']}\n"
            f"Confidence: {score:.0%} | Regime: {self.active_regime}\n"
            f"Strategies: {voted_str}"
        )

    def _build_oi_snapshot(self, chain: List) -> Dict:
        snap = {}
        for item in chain:
            strike = item.get("strike_price")
            if strike:
                snap[strike] = {
                    "ce_oi": item.get("call_options", {}).get("market_data", {}).get("oi", 0),
                    "pe_oi": item.get("put_options", {}).get("market_data", {}).get("oi", 0),
                }
        return snap

    # ── Position Management ───────────────────────────────────────────────────
    def mid_day_review(self):
        """Check open positions, trail SL if profitable"""
        open_trades = self.db.get_open_trades()
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                if not opt_key:
                    continue
                current = self.broker.get_ltp(opt_key)
                if not current:
                    continue
                entry = trade["entry_price"]
                gain_pct = (current - entry) / entry

                # Hit Target 1 — trail SL to entry
                if gain_pct >= 0.50:
                    new_sl = max(trade["sl_price"], entry * 1.05)
                    if new_sl != trade["sl_price"]:
                        self.db.update_trade(trade["trade_id"], {"sl_price": new_sl})
                        logger.info(f"Trail SL for {trade['trade_id']}: ₹{new_sl}")

                # Check SL hit
                if current <= trade["sl_price"]:
                    self._close_trade(trade, current, "SL_HIT")

                # Check Target 2
                if current >= trade["target2_price"]:
                    self._close_trade(trade, current, "TARGET_HIT")

            except Exception as e:
                logger.info(f"Mid-day review error: {e}")

    def pre_close_review(self):
        """3 PM review — exit uncertain positions"""
        open_trades = self.db.get_open_trades()
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                current = self.broker.get_ltp(opt_key) if opt_key else None
                if not current:
                    continue
                pnl_pct = (current - trade["entry_price"]) / trade["entry_price"]
                if pnl_pct < 0.10:  # Less than 10% gain — exit before close
                    self._close_trade(trade, current, "PRE_CLOSE")
            except Exception as e:
                logger.info(f"Pre-close error: {e}")

    def force_exit_all(self):
        """15:25 — force close all positions"""
        open_trades = self.db.get_open_trades()
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                current = self.broker.get_ltp(opt_key) if opt_key else trade["entry_price"] * 0.9
                self._close_trade(trade, current, "FORCE_EXIT")
            except Exception as e:
                logger.info(f"Force exit error: {e}")
        logger.info("🔒 All positions closed")

    def _close_trade(self, trade: Dict, exit_price: float, reason: str):
        pnl = (exit_price - trade["entry_price"]) * trade["qty"]
        pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100

        if not self.config.paper_trading:
            opt_key = self.broker.find_option_instrument(
                trade["instrument"], trade["strike"],
                trade["option_type"], trade["expiry"]
            )
            if opt_key:
                self.broker.place_order(opt_key, trade["qty"], transaction_type="SELL")

        self.db.update_trade(trade["trade_id"], {
            "exit_price": exit_price,
            "exit_time": str(datetime.now()),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "status": reason,
            "notes": reason,
        })
        self.risk_guard.record_trade_result(pnl)
        self.capital += pnl

        emoji = "✅" if pnl > 0 else "❌"
        mode = "📝" if trade.get("paper_trade") else "🔴"
        self.alerter.send(
            f"{emoji} {mode} TRADE EXIT — {reason}\n"
            f"ID: {trade['trade_id']}\n"
            f"Entry: ₹{trade['entry_price']} → Exit: ₹{exit_price}\n"
            f"P&L: ₹{pnl:+,.0f} ({pnl_pct:+.1f}%)\n"
            f"Capital: ₹{self.capital:,.0f}"
        )

    def post_market_log(self):
        stats = self.db.get_summary_stats()
        daily = self.risk_guard.get_status(self.capital)
        self.alerter.send(
            f"📊 DAILY REPORT\n"
            f"Trades: {daily['daily_trades']} | P&L: ₹{daily['daily_loss']:+,.0f}\n"
            f"Win Rate: {stats.get('win_rate', 0):.1f}% | Capital: ₹{self.capital:,.0f}"
        )

    # ── Learning Engine ───────────────────────────────────────────────────────
    def weekly_backtest_and_retrain(self):
        logger.info("🎓 Weekly learning cycle starting...")
        try:
            perf = self.db.get_strategy_performance()
            if not perf:
                return
            new_weights = {}
            for row in perf:
                name = row["strategy_name"]
                win_rate = row["avg_win_rate"] or 0.5
                new_weights[name] = max(0.05, win_rate)
            self.strategy_engine.update_weights(new_weights)
            self.db.set_setting("strategy_weights", self.strategy_engine.weights)
            logger.info("✅ Strategy weights retrained")
            self.alerter.send(f"🎓 Weekly retraining complete\nNew weights: {json.dumps(self.strategy_engine.weights, indent=2)}")
        except Exception as e:
            logger.info(f"Retrain error: {e}")

    # ── Control Methods (called from API) ─────────────────────────────────────
    def set_active(self, active: bool):
        self._bot_active = active
        self.alerter.send(f"{'▶️ Bot activated' if active else '⏸️ Bot paused'}")

    def manual_exit_trade(self, trade_id: str) -> Dict:
        trade = self.db.get_trade_by_id(trade_id)
        if not trade or trade["status"] != "OPEN":
            return {"error": "Trade not found or not open"}
        opt_key = self.broker.find_option_instrument(
            trade["instrument"], trade["strike"], trade["option_type"], trade["expiry"]
        )
        price = self.broker.get_ltp(opt_key) if opt_key else trade["entry_price"] * 0.9
        self._close_trade(trade, price, "MANUAL_EXIT")
        return {"success": True, "exit_price": price}

    def update_settings(self, updates: Dict):
        for k, v in updates.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        self.config.save()

    def get_live_status(self) -> Dict:
        return {
            "active": self._bot_active,
            "paper_trading": self.config.paper_trading,
            "auto_trade": self.config.auto_trade,
            "regime": self.active_regime,
            "regime_confidence": self.regime_confidence,
            "capital": self.capital,
            "risk_status": self.risk_guard.get_status(self.capital),
            "open_trades": len(self.db.get_open_trades()),
            "strategy_weights": self.strategy_engine.weights,
        }
