"""
Bot Engine — Central orchestrator
Connects: Data → Regime → Strategy → Risk → Execution → Learning
"""
import json
import logging
import uuid
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List, Tuple

from data.Angle_broker_v2 import build_strategy_context
from core.config import Config
from core.regime_classifier import MarketRegimeClassifier, NO_DATA_REGIME
from core.risk_guard import RiskGuard
from backtest import Backtester
from data.Angle_broker_v2 import AngelOneBroker
from strategies.strategy_engine import StrategyEngine, BUY_CE, BUY_PE, NO_TRADE
from db.database import Database
from alerts.telegram_alert import TelegramAlerter

logger = logging.getLogger("ENGINE")

# Regimes we consider tradeable. NO_DATA_REGIME (insufficient/no data) is
# intentionally excluded — check_signals() will not evaluate instruments
# while the regime is unknown.
NON_TRADEABLE_REGIMES = {NO_DATA_REGIME}


class BotEngine:
    def __init__(self, config: Config, db: Database, alerter: TelegramAlerter):
        self.config = config
        self.db     = db
        self.alerter = alerter
        self._tf_cache: Dict[str, Tuple[datetime, pd.DataFrame]] = {}

        self.broker             = AngelOneBroker(
            db_path=config.db_path,
            api_key=config.angleone_api_key or None,
            client_id=config.angleone_client_id or None,
            password=config.angleone_password or None,
            totp_secret=config.angleone_totp_secret or None,
        )
        self.regime_classifier  = MarketRegimeClassifier()
        self.strategy_engine    = StrategyEngine(config.strategy_weights)
        self.risk_guard         = RiskGuard(config, db)

        # Start in the "no data yet" state rather than pretending we already
        # know the regime is RANGE_BOUND with 50% confidence — that fake
        # starting point could let check_signals() evaluate trades before
        # compute_daily_regime() has ever run.
        self.active_regime      = NO_DATA_REGIME
        self.regime_confidence  = 0.0
        self.capital            = config.total_capital
        self._oi_snapshot_prev: Dict = {}
        self._bot_active = True

        saved_weights = db.get_setting("strategy_weights")
        if saved_weights:
            self.strategy_engine.weights = saved_weights

        logger.info(
            f"[ENGINE_INIT] capital={self.capital}, paper_trading={config.paper_trading}, "
            f"auto_trade={config.auto_trade}, instruments={config.instruments}"
        )

    def _get_ohlcv_cached(self, instrument_key: str, interval: str, days: int, ttl_minutes: int):
        key = f"{instrument_key}:{interval}"
        cached = self._tf_cache.get(key)
        if cached and (datetime.now() - cached[0]).total_seconds() < ttl_minutes * 60:
            return cached[1]
        df = self.broker.get_ohlcv(instrument_key, interval, days=days)
        self._tf_cache[key] = (datetime.now(), df)
        return df
    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def pre_market_analysis(self):
        logger.info("🌅 [PRE_MARKET] INPUT: (none)")
        try:
            vix = self.broker.get_india_vix()
            logger.info(f"[PRE_MARKET] Live VIX fetch returned: {vix}")
            if vix is None:
                logger.error("[PRE_MARKET] VIX fetch returned None — NOT seeding a fake VIX into DB")
                self.alerter.send("🌅 Pre-market: ⚠️ VIX fetch FAILED — bot will skip regime/signal checks until VIX is available")
                return
            self.db.log_regime({"timestamp": str(datetime.now()), "instrument": "VIX",
                                "regime": "PRE_MARKET", "vix": vix, "confidence": 0})
            self.db.set_setting("today_vix", vix)
            logger.info(f"[PRE_MARKET] OUTPUT: VIX={vix:.2f} logged and seeded to DB")
            self.alerter.send(f"🌅 Pre-market: VIX={vix:.2f} | Bot ready")
        except Exception as e:
            logger.error(f"[PRE_MARKET] ERROR: {e}", exc_info=True)

    def compute_daily_regime(self):
        logger.info(f"🧠 [REGIME_CYCLE] INPUT: instruments={self.config.instruments}")
        for instrument_key in self.config.instruments:
            try:
                logger.info(f"[REGIME_CYCLE] Fetching 30-minute OHLCV for {instrument_key} (10 days)...")
                df = self.broker.get_ohlcv(instrument_key, "30minute", days=10)
                if df.empty:
                    logger.warning(f"[REGIME_CYCLE] No data for {instrument_key} — skipping (NOT defaulting to a regime)")
                    continue
                logger.info(f"[REGIME_CYCLE] Got {len(df)} candles for {instrument_key}")

                vix = self.broker.get_india_vix()
                logger.info(f"[REGIME_CYCLE] Live VIX for {instrument_key}: {vix}")
                if vix is None:
                    logger.error(f"[REGIME_CYCLE] VIX unavailable — skipping regime classification for {instrument_key}")
                    continue

                regime, confidence, features = self.regime_classifier.classify(df, vix)
                logger.info(
                    f"[REGIME_CYCLE] OUTPUT for {instrument_key}: regime={regime}, "
                    f"confidence={confidence}, features={features}"
                )

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
            except Exception as e:
                logger.error(f"[REGIME_CYCLE] ERROR for {instrument_key}: {e}", exc_info=True)

    def check_signals(self):
        logger.info(
            f"[SIGNAL_CHECK] INPUT: bot_active={self._bot_active}, "
            f"auto_trade={self.config.auto_trade}, paper_trading={self.config.paper_trading}, "
            f"active_regime={self.active_regime}, regime_confidence={self.regime_confidence}"
        )
        if not self._bot_active:
            logger.info("[SIGNAL_CHECK] OUTPUT: skipped (bot inactive)")
            return
        if not self.config.auto_trade and not self.config.paper_trading:
            logger.info("[SIGNAL_CHECK] OUTPUT: skipped (neither auto_trade nor paper_trading enabled)")
            return
        if self.active_regime in NON_TRADEABLE_REGIMES:
            logger.warning(
                f"[SIGNAL_CHECK] OUTPUT: skipped — active_regime={self.active_regime} "
                f"(compute_daily_regime hasn't produced a real regime yet)"
            )
            return

        for instrument_key in self.config.instruments:
            try:
                self._evaluate_instrument(instrument_key)
            except Exception as e:
                logger.error(f"[SIGNAL_CHECK] ERROR for {instrument_key}: {e}", exc_info=True)

    def _evaluate_instrument(self, instrument_key: str):
        logger.info(f"[EVAL] INPUT: instrument={instrument_key}")

        # ── 1. OHLCV candle data ──────────────────────────────────────────────
        df = self.broker.get_ohlcv(instrument_key, "15minute", days=3)
        if df is None or len(df) < 20:
            logger.warning(f"[EVAL] {instrument_key}: insufficient candles "
                            f"({len(df) if df is not None else 0}/20) — aborting eval")
            return
        logger.info(f"[EVAL] {instrument_key}: got {len(df)} candles")

        df_5m = df_1h = df_1d = None
        try:
            df_5m = self.broker.get_ohlcv(instrument_key, "5minute", days=2)
        except Exception as e:
            logger.warning(f"[EVAL] {instrument_key}: 5m fetch failed: {e}")
        try:
            df_1h = self._get_ohlcv_cached(instrument_key, "1hour", 10, ttl_minutes=20)
        except Exception as e:
            logger.warning(f"[EVAL] {instrument_key}: 1h fetch failed: {e}")
        try:
            df_1d = self._get_ohlcv_cached(instrument_key, "1day", 60, ttl_minutes=120)
        except Exception as e:
            logger.warning(f"[EVAL] {instrument_key}: 1D fetch failed: {e}")

        # ── 2. India VIX — REQUIRED, no fake fallback ──────────────────────────
        live_vix = self.broker.get_india_vix()
        if live_vix is None:
            logger.error(f"[EVAL] {instrument_key}: live VIX fetch failed — aborting eval "
                         f"(refusing to substitute a fake VIX=15.0)")
            return
        vix = live_vix
        logger.info(f"[EVAL] {instrument_key}: VIX={vix:.2f}")

        stored = self.db.get_setting("today_vix")
        if stored is None:
            # First cycle of the day — no prior VIX to diff against. Use current
            # VIX as the baseline (vix_change=0 this cycle), not an artificial
            # clamp to 10.0 which silently distorted the VIX-change signal.
            logger.info(f"[EVAL] {instrument_key}: no stored prior VIX yet — using current VIX as baseline")
            prev_vix = vix
        else:
            prev_vix = float(stored)
        self.db.set_setting("today_vix", live_vix)

        # ── 3. Expiries ───────────────────────────────────────────────────────
        expiries = self.broker.get_option_expiries(instrument_key)
        expiry   = expiries[0] if expiries else None
        if not expiry:
            logger.error(f"[EVAL] {instrument_key}: no available expiries — aborting eval")
            return
        logger.info(f"[EVAL] {instrument_key}: using expiry={expiry}")

        try:
            from datetime import date as _date
            dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - _date.today()).days
            dte = max(0, dte)
        except Exception as e:
            logger.error(f"[EVAL] {instrument_key}: could not compute DTE from expiry '{expiry}': {e} — aborting eval")
            return
        logger.info(f"[EVAL] {instrument_key}: DTE={dte}")

        # ── 4. Option chain ──────────────────────────────────────────────────
        chain_result = self.broker.get_option_chain(instrument_key, expiry)
        chain_list, atm = chain_result
        if not chain_list or not atm:
            logger.error(f"[EVAL] {instrument_key}: empty option chain or ATM=0 — aborting eval")
            return
        logger.info(f"[EVAL] {instrument_key}: got {len(chain_list)} contracts, ATM={atm}")

        # ── 5. Strategy context ───────────────────────────────────────────────
        prev_snapshot = self._oi_snapshot_prev.get(instrument_key, {})
        context = build_strategy_context(
            chain               = chain_list,
            vix                 = vix,
            atm_strike          = atm,
            prev_chain_snapshot = prev_snapshot,
            days_to_expiry      = dte,
            prev_vix            = prev_vix,
            underlying_ltp      = df["close"].iloc[-1] if not df.empty else None,
        )
        context["df_1d"]  = df_1d
        context["df_1h"]  = df_1h
        context["df_15m"] = df  # the 15-min df we already fetched
        context["df_5m"]  = df_5m
        self._oi_snapshot_prev[instrument_key] = context.get("chain_snapshot_now", {})
        #logger.info(f"[EVAL] {instrument_key}: context={context}")

        # ── 6. Strategy engine — regime_confidence now passed for real ────────
        signal, score, details = self.strategy_engine.evaluate(
            df, context, regime=self.active_regime, regime_confidence=self.regime_confidence
        )
        signal_type = (
            "BUY_CE"   if signal == BUY_CE else
            "BUY_PE"   if signal == BUY_PE else
            "NO_TRADE"
        )
        strategies_summary = {
            k: {"signal": v["signal"], "confidence": v["confidence"], "reason": v["reason"]}
            for k, v in details.items()
        }
        logger.info(f"[EVAL] {instrument_key}: OUTPUT signal={signal_type} score={score}")
        logger.info(f"[EVAL] {instrument_key}: strategy breakdown={json.dumps(strategies_summary, indent=2)}")

        # ── 7. Persist signal ─────────────────────────────────────────────────
        self.db.insert_signal({
            "timestamp":   str(datetime.now()),
            "instrument":  instrument_key,
            "signal_type": signal_type,
            "score":       score,
            "strategies":  json.dumps(strategies_summary),
            "regime":      self.active_regime,
            "confidence":  score,
        })

        # ── 8. Execute ────────────────────────────────────────────────────────
        if signal == NO_TRADE:
            logger.info(f"[EVAL] {instrument_key}: NO_TRADE — skipping execution")
            return
        if score < self.strategy_engine.min_score:
            logger.info(f"[EVAL] {instrument_key}: score {score} below threshold "
                        f"{self.strategy_engine.min_score} — skipping execution")
            return

        logger.info(f"[EVAL] {instrument_key}: valid signal {signal_type} score={score} → executing")
        self._execute_trade(instrument_key, signal, score, details, df, expiry, atm)

    def _execute_trade(self, instrument_key: str, signal: int, score: float,
                       details: Dict, df, expiry: str, atm: int):
        logger.info(f"[TRADE] INPUT: instrument={instrument_key}, signal={signal}, score={score}, "
                    f"expiry={expiry}, atm={atm}")

        lot_size    = self.broker.get_lot_size(instrument_key)
        strike_step = self.broker.get_strike_step(instrument_key)
        if not lot_size or lot_size <= 0:
            logger.error(f"[TRADE] {instrument_key}: no valid lot size resolved ({lot_size}) — aborting trade")
            return

        strike      = atm + (strike_step if signal == BUY_CE else -strike_step)
        option_type = "CE" if signal == BUY_CE else "PE"
        logger.info(f"[TRADE] {instrument_key}: ATM={atm} StrikeStep={strike_step} → Strike={strike}{option_type} "
                    f"LotSize={lot_size}")

        opt_key = self.broker.find_option_instrument(instrument_key, strike, option_type, expiry)
        if not opt_key:
            logger.error(f"[TRADE] Option not found: {strike}{option_type} {expiry} — aborting trade")
            return
        logger.info(f"[TRADE] Resolved option instrument: {opt_key}")

        premium = self.broker.get_ltp(opt_key)
        if not premium:
            logger.error(f"[TRADE] {opt_key}: could not fetch live premium — aborting trade")
            return
        logger.info(f"[TRADE] {opt_key}: premium=₹{premium:.2f}")

        try:
            qty = max(1, self.risk_guard.position_size(self.capital, score, lot_size, premium)) * lot_size
        except ValueError as e:
            logger.error(f"[TRADE] position sizing failed: {e} — aborting trade")
            return
        logger.info(f"[TRADE] qty={qty}")

        approved, reason = self.risk_guard.validate(self.capital, premium, qty, score)
        if not approved:
            logger.warning(f"[TRADE] REJECTED by risk guard: {reason}")
            return

        try:
            levels = self.risk_guard.calculate_sl_target(premium, signal, self.active_regime)
        except ValueError as e:
            logger.error(f"[TRADE] SL/target calc failed: {e} — aborting trade")
            return
        logger.info(f"[TRADE] levels={levels}")

        order = self.broker.place_order(opt_key, qty, transaction_type="BUY")
        if not order or not order.get("order_id"):
            logger.error(f"[TRADE] Order placement failed: {order}")
            return
        logger.info(f"[TRADE] Order placed: {order}")

        trade_id         = str(uuid.uuid4())[:8].upper()
        strategies_voted = [k for k, v in details.items() if v["signal"] == signal]

        current_vix = self.broker.get_india_vix()
        trade = {
            "trade_id":          trade_id,
            "instrument":        instrument_key,
            "strike":            strike,
            "option_type":       option_type,
            "expiry":            expiry,
            "action":            "BUY",
            "qty":               qty,
            "entry_price":       premium,
            "entry_time":        str(datetime.now()),
            "sl_price":          levels["sl_price"],
            "target1_price":     levels["target1_price"],
            "target2_price":     levels["target2_price"],
            "status":            "OPEN",
            "strategies_voted":  json.dumps(strategies_voted),
            "confidence_score":  score,
            "regime":            self.active_regime,
            "upstox_order_id":   order.get("order_id", ""),
            "paper_trade":       1 if self.config.paper_trading else 0,
            "market_conditions": json.dumps({
                "vix": current_vix,
                "regime": self.active_regime,
            }),
        }
        self.db.insert_trade(trade)
        logger.info(f"[TRADE] OUTPUT: trade persisted: {trade}")

        mode      = "📝 PAPER" if self.config.paper_trading else "🔴 LIVE"
        voted_str = ", ".join(strategies_voted[:4])
        self.alerter.send(
            f"{mode} TRADE ENTRY\nID: {trade_id}\n"
            f"{'Nifty' if 'Nifty 50' in instrument_key else 'BankNifty'} "
            f"{strike}{option_type} {expiry}\n"
            f"Premium: ₹{premium} | Qty: {qty}\n"
            f"SL: ₹{levels['sl_price']} | T1: ₹{levels['target1_price']} "
            f"| T2: ₹{levels['target2_price']}\n"
            f"Confidence: {score:.0%} | Regime: {self.active_regime}\n"
            f"Strategies: {voted_str}"
        )

    # ── Position Management ───────────────────────────────────────────────────

    def mid_day_review(self):
        open_trades = self.db.get_open_trades()
        logger.info(f"[MID_DAY_REVIEW] INPUT: open_trades={len(open_trades)}")
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                if not opt_key:
                    logger.warning(f"[MID_DAY_REVIEW] {trade['trade_id']}: could not resolve option instrument — skipping")
                    continue
                current  = self.broker.get_ltp(opt_key)
                if not current:
                    logger.warning(f"[MID_DAY_REVIEW] {trade['trade_id']}: LTP fetch failed — skipping")
                    continue
                entry    = trade["entry_price"]
                gain_pct = (current - entry) / entry
                logger.info(f"[MID_DAY_REVIEW] {trade['trade_id']}: current={current}, entry={entry}, gain_pct={gain_pct:.2%}")
                if gain_pct >= 0.50:
                    new_sl = max(trade["sl_price"], entry * 1.05)
                    if new_sl != trade["sl_price"]:
                        self.db.update_trade(trade["trade_id"], {"sl_price": new_sl})
                        logger.info(f"[MID_DAY_REVIEW] {trade['trade_id']}: trailed SL to ₹{new_sl}")
                if current <= trade["sl_price"]:
                    self._close_trade(trade, current, "SL_HIT")
                if current >= trade["target2_price"]:
                    self._close_trade(trade, current, "TARGET_HIT")
            except Exception as e:
                logger.error(f"[MID_DAY_REVIEW] ERROR for {trade.get('trade_id')}: {e}", exc_info=True)

    def pre_close_review(self):
        open_trades = self.db.get_open_trades()
        logger.info(f"[PRE_CLOSE_REVIEW] INPUT: open_trades={len(open_trades)}")
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                current = self.broker.get_ltp(opt_key) if opt_key else None
                if not current:
                    logger.warning(f"[PRE_CLOSE_REVIEW] {trade['trade_id']}: LTP unavailable — skipping")
                    continue
                pnl_pct = (current - trade["entry_price"]) / trade["entry_price"]
                logger.info(f"[PRE_CLOSE_REVIEW] {trade['trade_id']}: current={current}, pnl_pct={pnl_pct:.2%}")
                if pnl_pct < 0.10:
                    self._close_trade(trade, current, "PRE_CLOSE")
            except Exception as e:
                logger.error(f"[PRE_CLOSE_REVIEW] ERROR for {trade.get('trade_id')}: {e}", exc_info=True)

    def force_exit_all(self):
        open_trades = self.db.get_open_trades()
        logger.info(f"[FORCE_EXIT] INPUT: open_trades={len(open_trades)}")
        for trade in open_trades:
            try:
                opt_key = self.broker.find_option_instrument(
                    trade["instrument"], trade["strike"],
                    trade["option_type"], trade["expiry"]
                )
                current = self.broker.get_ltp(opt_key) if opt_key else None
                if current is None:
                    logger.error(
                        f"[FORCE_EXIT] {trade['trade_id']}: could not fetch live LTP — "
                        f"closing at entry_price as a LAST RESORT (paper/PNL will be inaccurate)"
                    )
                    current = trade["entry_price"]
                self._close_trade(trade, current, "FORCE_EXIT")
            except Exception as e:
                logger.error(f"[FORCE_EXIT] ERROR for {trade.get('trade_id')}: {e}", exc_info=True)
        logger.info("[FORCE_EXIT] OUTPUT: all positions processed")

    def _close_trade(self, trade: Dict, exit_price: float, reason: str):
        logger.info(f"[CLOSE_TRADE] INPUT: trade_id={trade['trade_id']}, exit_price={exit_price}, reason={reason}")
        pnl     = (exit_price - trade["entry_price"]) * trade["qty"]
        pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
        if not self.config.paper_trading:
            opt_key = self.broker.find_option_instrument(
                trade["instrument"], trade["strike"],
                trade["option_type"], trade["expiry"]
            )
            if opt_key:
                self.broker.place_order(opt_key, trade["qty"], transaction_type="SELL")
        self.db.update_trade(trade["trade_id"], {
            "exit_price": exit_price, "exit_time": str(datetime.now()),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "status": reason, "notes": reason,
        })
        self.risk_guard.record_trade_result(pnl)
        self.capital += pnl
        logger.info(f"[CLOSE_TRADE] OUTPUT: pnl={round(pnl,2)}, pnl_pct={round(pnl_pct,2)}, capital={self.capital}")
        emoji = "✅" if pnl > 0 else "❌"
        mode  = "📝" if trade.get("paper_trade") else "🔴"
        self.alerter.send(
            f"{emoji} {mode} TRADE EXIT — {reason}\nID: {trade['trade_id']}\n"
            f"Entry: ₹{trade['entry_price']} → Exit: ₹{exit_price}\n"
            f"P&L: ₹{pnl:+,.0f} ({pnl_pct:+.1f}%)\nCapital: ₹{self.capital:,.0f}"
        )

    def post_market_log(self):
        logger.info("[POST_MARKET] INPUT: (none)")
        stats = self.db.get_summary_stats()
        daily = self.risk_guard.get_status(self.capital)
        logger.info(f"[POST_MARKET] OUTPUT: stats={stats}, daily={daily}")
        self.alerter.send(
            f"📊 DAILY REPORT\n"
            f"Trades: {daily['daily_trades']} | P&L: ₹{daily['daily_loss']:+,.0f}\n"
            f"Win Rate: {stats.get('win_rate', 0):.1f}% | Capital: ₹{self.capital:,.0f}"
        )

    # ── Learning ──────────────────────────────────────────────────────────────

    def weekly_backtest_and_retrain(self):
        logger.info("🎓 [RETRAIN] INPUT: (none)")
        try:
            perf = self.db.get_strategy_performance()
            logger.info(f"[RETRAIN] Fetched performance rows: {perf}")
            if not perf:
                logger.warning("[RETRAIN] No performance data — skipping retrain (NOT applying default weights)")
                return
            new_weights = {
                row["strategy_name"]: max(0.05, row["avg_win_rate"] or 0.5)
                for row in perf
            }
            self.strategy_engine.update_weights(new_weights)
            self.db.set_setting("strategy_weights", self.strategy_engine.weights)
            logger.info(f"[RETRAIN] OUTPUT: weights retrained → {self.strategy_engine.weights}")
        except Exception as e:
            logger.error(f"[RETRAIN] ERROR: {e}", exc_info=True)

    def run_full_backtest(self, instrument_key: str = "NSE_INDEX|Nifty 50", days: int = 90):
        logger.info(f"[BACKTEST] Running walk-forward backtest for {instrument_key}, {days} days")
        
        bt = Backtester(initial_capital=self.capital)
        result = bt.run_real(
            broker=self.broker,
            strategy_engine=self.strategy_engine,
            instrument_key=instrument_key,
            days=days,
            min_confidence=self.strategy_engine.min_score,
        )
        stats = result.compute_stats(self.capital)
        if "error" in stats:
            logger.warning(f"[BACKTEST] {stats['error']} — nothing to persist")
            return stats
        stats["period_start"] = result.period_start
        stats["period_end"] = result.period_end
        self.db.insert_backtest_result(stats)
        logger.info(f"[BACKTEST] OUTPUT: {stats}")
        return stats
    
    # ── Control ───────────────────────────────────────────────────────────────

    def set_active(self, active: bool):
        logger.info(f"[SET_ACTIVE] INPUT: active={active}")
        self._bot_active = active
        self.alerter.send(f"{'▶️ Bot activated' if active else '⏸️ Bot paused'}")

    def manual_exit_trade(self, trade_id: str) -> Dict:
        logger.info(f"[MANUAL_EXIT] INPUT: trade_id={trade_id}")
        trade = self.db.get_trade_by_id(trade_id)
        if not trade or trade["status"] != "OPEN":
            logger.warning(f"[MANUAL_EXIT] OUTPUT: trade not found or not open ({trade_id})")
            return {"error": "Trade not found or not open"}
        opt_key = self.broker.find_option_instrument(
            trade["instrument"], trade["strike"],
            trade["option_type"], trade["expiry"]
        )
        price = self.broker.get_ltp(opt_key) if opt_key else None
        if price is None:
            logger.error(f"[MANUAL_EXIT] {trade_id}: could not fetch live LTP — aborting manual exit")
            return {"error": "Could not fetch live price — exit aborted, close manually if urgent"}
        self._close_trade(trade, price, "MANUAL_EXIT")
        logger.info(f"[MANUAL_EXIT] OUTPUT: exit_price={price}")
        return {"success": True, "exit_price": price}

    def update_settings(self, updates: Dict):
        logger.info(f"[UPDATE_SETTINGS] INPUT: {updates}")
        for k, v in updates.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        self.config.save()
        logger.info("[UPDATE_SETTINGS] OUTPUT: config saved")

    def get_live_status(self) -> Dict:
        status = {
            "active":            self._bot_active,
            "paper_trading":     self.config.paper_trading,
            "auto_trade":        self.config.auto_trade,
            "regime":            self.active_regime,
            "regime_confidence": self.regime_confidence,
            "capital":           self.capital,
            "risk_status":       self.risk_guard.get_status(self.capital),
            "open_trades":       len(self.db.get_open_trades()),
            "strategy_weights":  self.strategy_engine.weights,
        }
        logger.debug(f"[GET_LIVE_STATUS] OUTPUT: {status}")
        return status