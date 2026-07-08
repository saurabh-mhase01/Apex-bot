import logging
import numpy as np
import pandas as pd
from typing import Tuple, Dict
from data.indicators import ema, rsi, adx, bollinger_bands, market_structure, rate_of_change

logger = logging.getLogger("REGIME")

REGIMES = ["TRENDING_BULL", "TRENDING_BEAR", "RANGE_BOUND", "HIGH_VOLATILITY", "PRE_EVENT"]

# Sentinel regime returned when we do NOT have enough real data to classify.
# NEVER treat this as a tradeable regime — bot_engine must skip on this value.
NO_DATA_REGIME = "INSUFFICIENT_DATA"


class MarketRegimeClassifier:
    def __init__(self):
        # last_regime/last_confidence are kept ONLY for observability (dashboards/logs).
        # classify() no longer returns these as a silent fallback on bad input —
        # that used to let the bot trade off a stale regime with fake confidence.
        self.last_regime = NO_DATA_REGIME
        self.last_confidence = 0.0

    def classify(self, df: pd.DataFrame, vix: float, pcr: float = 1.0) -> Tuple[str, float, Dict]:
        """
        vix is REQUIRED — no default. Caller must fetch a real India VIX value
        before calling this. If you don't have a live VIX, do not call classify().
        """
        logger.info(f"[REGIME_CLASSIFY] INPUT: candles={0 if df is None else len(df)}, vix={vix}, pcr={pcr}")

        if vix is None:
            logger.error("[REGIME_CLASSIFY] vix is None — refusing to classify on fake data")
            return NO_DATA_REGIME, 0.0, {}

        if df is None or len(df) < 30:
            logger.warning(
                f"[REGIME_CLASSIFY] Insufficient candles ({0 if df is None else len(df)}/30) — "
                f"returning {NO_DATA_REGIME} instead of a guessed regime"
            )
            self.last_regime = NO_DATA_REGIME
            self.last_confidence = 0.0
            return NO_DATA_REGIME, 0.0, {}

        f = self._extract_features(df, vix, pcr)
        logger.info(f"[REGIME_CLASSIFY] FEATURES: {f}")

        regime, conf = self._rule_based_classify(f)
        logger.info(f"[REGIME_CLASSIFY] OUTPUT: regime={regime}, confidence={conf}")

        self.last_regime = regime
        self.last_confidence = conf
        return regime, conf, f

    def _extract_features(self, df, vix, pcr):
        logger.debug(f"[REGIME_FEATURES] INPUT: candles={len(df)}, vix={vix}, pcr={pcr}")
        close = df["close"]

        ema9 = ema(close, 9).iloc[-1]
        ema21 = ema(close, 21).iloc[-1]
        ema50 = ema(close, 50).iloc[-1] if len(close) >= 50 else ema21

        rsi_val = rsi(close).iloc[-1]
        adx_val = adx(df["high"], df["low"], close).iloc[-1]
        _, _, _, bb_width = bollinger_bands(close)
        bb_width_val = bb_width.iloc[-1]

        roc = rate_of_change(close, 10).iloc[-1]
        struct = market_structure(df)
        price = close.iloc[-1]

        features = {
            "price": price,
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema_cross": 1 if ema9 > ema21 else -1,
            "price_vs_ema50": (price - ema50) / ema50 * 100,
            "rsi": rsi_val,
            "adx": adx_val,
            "bb_width": bb_width_val,
            "roc": roc,
            "vix": vix,
            "pcr": pcr,
            "structure": struct,
        }
        logger.debug(f"[REGIME_FEATURES] OUTPUT: {features}")
        return features

    def _rule_based_classify(self, f):
        logger.debug(f"[REGIME_RULES] INPUT: {f}")
        score = {r: 0 for r in REGIMES}

        # VOLATILITY
        if f["vix"] > 20:
            score["HIGH_VOLATILITY"] += 0.5
        if f["bb_width"] > 0.04:
            score["HIGH_VOLATILITY"] += 0.3

        # TREND BOOST
        if f["adx"] > 30:
            score["RANGE_BOUND"] -= 0.4

        # BULL
        if f["ema_cross"] == 1:
            score["TRENDING_BULL"] += 0.3
        if f["adx"] > 25 and f["rsi"] > 50:
            score["TRENDING_BULL"] += 0.3
        if f["structure"] == "BULLISH":
            score["TRENDING_BULL"] += 0.3

        # BEAR
        if f["ema_cross"] == -1:
            score["TRENDING_BEAR"] += 0.3
        if f["adx"] > 25 and f["rsi"] < 50:
            score["TRENDING_BEAR"] += 0.3
        if f["structure"] == "BEARISH":
            score["TRENDING_BEAR"] += 0.3

        # RANGE
        if f["adx"] < 18:
            score["RANGE_BOUND"] += 0.5
        if 40 < f["rsi"] < 60:
            score["RANGE_BOUND"] += 0.2

        logger.debug(f"[REGIME_RULES] SCORES: {score}")

        # Guard against a "winning" score of 0 (all rules missed) masquerading as a real regime.
        best = max(score, key=score.get)
        if score[best] <= 0:
            logger.warning(f"[REGIME_RULES] All regime scores <= 0 ({score}) — no clear regime")
            return NO_DATA_REGIME, 0.0

        # BUG FIX: max(score, key=score.get) silently resolved ties by dict/list
        # insertion order — REGIMES = ["TRENDING_BULL", "TRENDING_BEAR", ...] means
        # every BULL/BEAR tie defaulted to TRENDING_BULL regardless of what the
        # data actually said. Confirmed in live logs: Bank Nifty scored
        # TRENDING_BULL=0.3 / TRENDING_BEAR=0.3 exactly at the moment RSI crossed
        # below 50 during an intraday reversal, and the tie silently resolved to
        # BULL — masking the reversal instead of flagging it as ambiguous.
        #
        # Now: if the top two scores are within EPSILON of each other, treat this
        # as genuine ambiguity (chop/transition), not a directional call. Prefer
        # RANGE_BOUND if it's in contention; otherwise keep the top pick but cap
        # confidence low so downstream code (regime_boost / relaxed threshold in
        # StrategyEngine) doesn't treat an ambiguous tie as high conviction.
        EPSILON = 0.05
        ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        top_regime, top_score = ranked[0]
        runner_regime, runner_score = ranked[1]

        if (top_score - runner_score) < EPSILON:
            tied_regimes = {top_regime, runner_regime}
            logger.warning(
                f"[REGIME_RULES] Ambiguous call — top two regimes tied within "
                f"{EPSILON}: {top_regime}={top_score}, {runner_regime}={runner_score}. "
                f"Not defaulting to insertion order."
            )
            if "RANGE_BOUND" in tied_regimes or score.get("RANGE_BOUND", 0) >= top_score - EPSILON:
                best = "RANGE_BOUND"
            else:
                best = top_regime  # keep top pick, but confidence gets capped below
            conf = min(top_score, 0.4)  # ambiguous → never report high confidence
        else:
            best = top_regime
            conf = min(top_score, 0.95)

        # VERIFICATION LOG — grep "[REGIME_TIEBREAK]" to confirm ties are now
        # being caught instead of silently defaulting to TRENDING_BULL.
        logger.debug(f"[REGIME_TIEBREAK] top={top_regime}({top_score}) runner={runner_regime}({runner_score}) → best={best}, conf={round(conf,2)}")
        logger.debug(f"[REGIME_RULES] OUTPUT: best={best}, confidence={round(conf,2)}")
        return best, round(conf, 2)

    def get_strategy_for_regime(self, regime: str):
        logger.debug(f"[REGIME_STRATEGY_MAP] INPUT: regime={regime}")
        mapping = {
            "TRENDING_BULL": {"direction": "CE", "confidence_threshold": 0.55},
            "TRENDING_BEAR": {"direction": "PE", "confidence_threshold": 0.55},
            "RANGE_BOUND": {"direction": "SKIP", "confidence_threshold": 0.75},
            "HIGH_VOLATILITY": {"direction": "STRADDLE", "confidence_threshold": 0.65},
            "PRE_EVENT": {"direction": "SKIP", "confidence_threshold": 0.90},
        }
        result = mapping.get(regime, {"direction": "SKIP", "confidence_threshold": 1.0})
        logger.debug(f"[REGIME_STRATEGY_MAP] OUTPUT: {result}")
        return result