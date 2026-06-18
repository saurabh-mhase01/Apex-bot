"""
Market Regime Classifier
Detects: TRENDING_BULL / TRENDING_BEAR / RANGE_BOUND / HIGH_VOLATILITY / PRE_EVENT
"""
import logging
import numpy as np
import pandas as pd
from typing import Tuple, Dict
from data.indicators import ema, rsi, adx, bollinger_bands, market_structure, rate_of_change

logger = logging.getLogger("REGIME")

REGIMES = ["TRENDING_BULL", "TRENDING_BEAR", "RANGE_BOUND", "HIGH_VOLATILITY", "PRE_EVENT"]


class MarketRegimeClassifier:
    def __init__(self):
        self.last_regime = "RANGE_BOUND"
        self.last_confidence = 0.5

    def classify(self, df: pd.DataFrame, vix: float = 15.0, pcr: float = 1.0) -> Tuple[str, float, Dict]:
        """Returns (regime, confidence, feature_dict)"""
        logger.info(f"[CLASSIFY] Starting regime classification with {len(df) if df is not None else 0} candles, VIX={vix:.2f}, PCR={pcr:.2f}")
        if df is None or len(df) < 30:
            logger.info(f"[CLASSIFY] Insufficient data ({len(df) if df is not None else 0} < 30), using previous regime: {self.last_regime}")
            return self.last_regime, 0.4, {}

        features = self._extract_features(df, vix, pcr)
        logger.info(f"[CLASSIFY] Features extracted: Price={features.get('price', 0):.2f}, EMA9={features.get('ema9', 0):.2f}, RSI={features.get('rsi', 0):.1f}, ADX={features.get('adx', 0):.1f}")
        
        regime, confidence = self._rule_based_classify(features)
        logger.info(f"[CLASSIFY] ✅ Regime classified: {regime} (confidence={confidence:.0%})")

        self.last_regime = regime
        self.last_confidence = confidence
        return regime, confidence, features

    def _extract_features(self, df: pd.DataFrame, vix: float, pcr: float) -> Dict:
        logger.info(f"[EXTRACT] Computing technical indicators...")
        close = df["close"]
        high = df["high"]
        low = df["low"]

        ema9 = ema(close, 9).iloc[-1]
        ema21 = ema(close, 21).iloc[-1]
        ema50 = ema(close, 50).iloc[-1] if len(close) >= 50 else ema(close, 21).iloc[-1]
        logger.info(f"[EXTRACT] EMAs: 9={ema9:.2f}, 21={ema21:.2f}, 50={ema50:.2f}")
        
        rsi_val = rsi(close).iloc[-1]
        adx_val = adx(high, low, close).iloc[-1]
        logger.info(f"[EXTRACT] RSI={rsi_val:.1f}, ADX={adx_val:.1f}")
        
        _, _, _, bb_width = bollinger_bands(close)
        bb_width_val = bb_width.iloc[-1]
        roc = rate_of_change(close, 10).iloc[-1]
        struct = market_structure(df)
        price = close.iloc[-1]
        logger.info(f"[EXTRACT] BBWidth={bb_width_val:.2f}, ROC10={roc:.2f}, Structure={struct}")

        return {
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

    def _rule_based_classify(self, f: Dict) -> Tuple[str, float]:
        score = {r: 0.0 for r in REGIMES}

        # HIGH_VOLATILITY
        if f["vix"] > 20:
            score["HIGH_VOLATILITY"] += 0.5
        if f["bb_width"] > 0.04:
            score["HIGH_VOLATILITY"] += 0.3
        if abs(f["roc"]) > 2.5:
            score["HIGH_VOLATILITY"] += 0.2

        # TRENDING_BULL
        if f["ema_cross"] == 1:
            score["TRENDING_BULL"] += 0.3
        if f["price_vs_ema50"] > 0.5:
            score["TRENDING_BULL"] += 0.2
        if f["adx"] > 25 and f["rsi"] > 50:
            score["TRENDING_BULL"] += 0.25
        if f["structure"] == "BULLISH":
            score["TRENDING_BULL"] += 0.25
        if f["pcr"] < 0.8:
            score["TRENDING_BULL"] += 0.1
        if f["roc"] > 0.5:
            score["TRENDING_BULL"] += 0.1

        # TRENDING_BEAR
        if f["ema_cross"] == -1:
            score["TRENDING_BEAR"] += 0.3
        if f["price_vs_ema50"] < -0.5:
            score["TRENDING_BEAR"] += 0.2
        if f["adx"] > 25 and f["rsi"] < 50:
            score["TRENDING_BEAR"] += 0.25
        if f["structure"] == "BEARISH":
            score["TRENDING_BEAR"] += 0.25
        if f["pcr"] > 1.3:
            score["TRENDING_BEAR"] += 0.1
        if f["roc"] < -0.5:
            score["TRENDING_BEAR"] += 0.1

        # RANGE_BOUND
        if f["adx"] < 20:
            score["RANGE_BOUND"] += 0.4
        if 40 < f["rsi"] < 60:
            score["RANGE_BOUND"] += 0.2
        if f["bb_width"] < 0.02:
            score["RANGE_BOUND"] += 0.2
        if f["structure"] == "SIDEWAYS":
            score["RANGE_BOUND"] += 0.2

        # HIGH_VOLATILITY overrides on extreme VIX
        if f["vix"] > 25:
            score["HIGH_VOLATILITY"] = max(score["HIGH_VOLATILITY"], 0.85)

        best = max(score, key=score.get)
        confidence = min(score[best], 0.95)
        return best, round(confidence, 2)

    def get_strategy_for_regime(self, regime: str) -> Dict:
        """Return trading parameters for the detected regime"""
        mapping = {
            "TRENDING_BULL": {
                "direction": "CE",
                "strike_offset": 1,       # 1 strike OTM
                "sl_pct": 0.28,
                "t1_pct": 0.50,
                "t2_pct": 1.00,
                "confidence_threshold": 0.55,
            },
            "TRENDING_BEAR": {
                "direction": "PE",
                "strike_offset": 1,
                "sl_pct": 0.28,
                "t1_pct": 0.50,
                "t2_pct": 1.00,
                "confidence_threshold": 0.55,
            },
            "RANGE_BOUND": {
                "direction": "SKIP",
                "confidence_threshold": 0.80,  # Very selective
            },
            "HIGH_VOLATILITY": {
                "direction": "STRADDLE",
                "strike_offset": 0,           # ATM
                "sl_pct": 0.35,
                "t1_pct": 0.70,
                "t2_pct": 1.50,
                "confidence_threshold": 0.65,
            },
            "PRE_EVENT": {
                "direction": "SKIP",
                "confidence_threshold": 0.90,
            },
        }
        return mapping.get(regime, mapping["RANGE_BOUND"])
