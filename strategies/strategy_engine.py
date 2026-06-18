"""
Strategy Engine — All 16 strategies with weighted voting
"""
import logging
import numpy as np
import pandas as pd
from typing import Dict, Tuple, List, Optional
from data.indicators import (
    ema, rsi, macd, adx, atr, bollinger_bands, keltner_channel,
    vwap, supertrend, fibonacci_levels, pivot_points,
    bb_squeeze, rate_of_change, market_structure, detect_swing_highs_lows
)

logger = logging.getLogger("STRATEGY")

# Signal values
BUY_CE = 1
BUY_PE = -1
NO_TRADE = 0


class BaseStrategy:
    name = "base"
    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        """Returns (signal, confidence, reason)"""
        return NO_TRADE, 0.0, "Not implemented"


class SMCStrategy(BaseStrategy):
    """Smart Money Concepts — BOS, Order Blocks, FVG"""
    name = "smc"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[SMC] Evaluating Smart Money Concepts strategy with {len(df)} candles")
        if len(df) < 20:
            logger.info(f"[SMC] Insufficient data: {len(df)} < 20")
            return NO_TRADE, 0, "Insufficient data"
        try:
            structure = market_structure(df)
            swings = detect_swing_highs_lows(df, lookback=3)
            rsi_val = rsi(df["close"]).iloc[-1]
            logger.info(f"[SMC] Market structure={structure}, RSI={rsi_val:.1f}")
            close = df["close"].iloc[-1]
            last_swing_low = swings["swing_low"].dropna().iloc[-1] if not swings["swing_low"].dropna().empty else None
            last_swing_high = swings["swing_high"].dropna().iloc[-1] if not swings["swing_high"].dropna().empty else None

            if structure == "BULLISH" and last_swing_low and close > last_swing_low * 1.001 and rsi_val < 65:
                conf = 0.70 if rsi_val < 55 else 0.55
                return BUY_CE, conf, f"BOS Bullish, OB retest, RSI {rsi_val:.0f}"

            if structure == "BEARISH" and last_swing_high and close < last_swing_high * 0.999 and rsi_val > 35:
                conf = 0.70 if rsi_val > 45 else 0.55
                return BUY_PE, conf, f"BOS Bearish, OB retest, RSI {rsi_val:.0f}"

            return NO_TRADE, 0, f"No SMC setup ({structure})"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class ORBStrategy(BaseStrategy):
    """Opening Range Breakout with VWAP confirmation"""
    name = "orb"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        if len(df) < 5:
            return NO_TRADE, 0, "Insufficient data"
        try:
            # ORB = first 15-min candle
            orb_high = df["high"].iloc[0]
            orb_low = df["low"].iloc[0]
            orb_range = orb_high - orb_low

            vwap_val = vwap(df["high"], df["low"], df["close"], df["volume"]).iloc[-1]
            close = df["close"].iloc[-1]
            vol_avg = df["volume"].rolling(10).mean().iloc[-1]
            vol_now = df["volume"].iloc[-1]
            vol_surge = vol_now > vol_avg * 1.3

            if close > orb_high * 1.001 and close > vwap_val and vol_surge:
                conf = 0.75 if orb_range > 30 else 0.60
                return BUY_CE, conf, f"ORB breakout UP, VWAP confirmed, vol surge"

            if close < orb_low * 0.999 and close < vwap_val and vol_surge:
                conf = 0.75 if orb_range > 30 else 0.60
                return BUY_PE, conf, f"ORB breakdown DOWN, below VWAP"

            return NO_TRADE, 0, f"Price within ORB range"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class GreeksStrategy(BaseStrategy):
    """Options Greeks momentum — Delta/Gamma acceleration"""
    name = "greeks"

    def signal(self, df: pd.DataFrame, iv_percentile: float = 50,
               atm_delta: float = 0.5, theta_pct: float = 0.10, **kwargs) -> Tuple[int, float, str]:
        try:
            rsi_val = rsi(df["close"]).iloc[-1]
            macd_line, signal_line, hist = macd(df["close"])
            macd_bullish = hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]
            macd_bearish = hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2]

            # Only buy when options are statistically cheap
            if iv_percentile > 60:
                return NO_TRADE, 0, f"IV too expensive ({iv_percentile:.0f}th pct)"

            if theta_pct > 0.15:
                return NO_TRADE, 0, f"Theta too high ({theta_pct:.1%})"

            if 0.35 <= atm_delta <= 0.65 and macd_bullish and rsi_val > 50:
                conf = 0.65 + (0.60 - iv_percentile / 100) * 0.3
                return BUY_CE, round(conf, 2), f"Greek momentum CE, IVP {iv_percentile:.0f}"

            if 0.35 <= atm_delta <= 0.65 and macd_bearish and rsi_val < 50:
                conf = 0.65 + (0.60 - iv_percentile / 100) * 0.3
                return BUY_PE, round(conf, 2), f"Greek momentum PE, IVP {iv_percentile:.0f}"

            return NO_TRADE, 0, "No greeks setup"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class FibSRStrategy(BaseStrategy):
    """Fibonacci + Support/Resistance confluence"""
    name = "fib_sr"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        if len(df) < 20:
            return NO_TRADE, 0, "Insufficient data"
        try:
            # Recent swing high/low for Fibonacci
            period_high = df["high"].tail(20).max()
            period_low = df["low"].tail(20).min()
            fibs = fibonacci_levels(period_high, period_low)
            close = df["close"].iloc[-1]
            rsi_val = rsi(df["close"]).iloc[-1]

            # Daily pivots
            prev = df.tail(2).iloc[0]
            pivots = pivot_points(prev["high"], prev["low"], prev["close"])

            # Check confluence with Fib levels
            fib_zones = [fibs["0.382"], fibs["0.5"], fibs["0.618"]]
            support_zones = [pivots["s1"], pivots["s2"], pivots["pp"]]
            resist_zones = [pivots["r1"], pivots["r2"]]

            def near(price, level, pct=0.002):
                return abs(price - level) / level < pct

            at_fib_support = any(near(close, z) for z in fib_zones if z < close * 1.003)
            at_fib_resist = any(near(close, z) for z in fib_zones if z > close * 0.997)
            at_support = any(near(close, z) for z in support_zones)
            at_resist = any(near(close, z) for z in resist_zones)

            if (at_fib_support or at_support) and rsi_val < 45:
                return BUY_CE, 0.68, f"Fib/SR support confluence, RSI {rsi_val:.0f}"

            if (at_fib_resist or at_resist) and rsi_val > 55:
                return BUY_PE, 0.68, f"Fib/SR resistance confluence, RSI {rsi_val:.0f}"

            return NO_TRADE, 0, "No Fib/SR setup"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class VIXStrategy(BaseStrategy):
    """India VIX mean reversion strategy"""
    name = "vix"

    def signal(self, df: pd.DataFrame, vix: float = 15.0,
               vix_prev: float = 15.0, **kwargs) -> Tuple[int, float, str]:
        try:
            vix_change = (vix - vix_prev) / vix_prev * 100 if vix_prev else 0
            close = df["close"].iloc[-1]
            rsi_val = rsi(df["close"]).iloc[-1]

            # VIX spike + price at support = buy CE (fear spike into support)
            if vix_change > 5 and rsi_val < 40:
                return BUY_CE, 0.62, f"VIX spike {vix_change:.1f}% + oversold RSI"

            # VIX spike + price at resistance = buy PE
            if vix_change > 5 and rsi_val > 60:
                return BUY_PE, 0.62, f"VIX spike {vix_change:.1f}% + overbought"

            # VIX very low = compression, get ready
            if vix < 12:
                return NO_TRADE, 0, f"VIX compressed ({vix:.1f}), awaiting direction"

            return NO_TRADE, 0, f"VIX neutral ({vix:.1f})"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class OIFlowStrategy(BaseStrategy):
    """Options OI shift detector — tracks smart money flows"""
    name = "oi_flow"

    def signal(self, df: pd.DataFrame, chain_snapshot_prev: Dict = None,
               chain_snapshot_now: Dict = None, **kwargs) -> Tuple[int, float, str]:
        if not chain_snapshot_prev or not chain_snapshot_now:
            return NO_TRADE, 0, "No OI chain data"
        try:
            pe_oi_added = 0
            ce_oi_added = 0
            pe_oi_shed = 0
            ce_oi_shed = 0

            for strike in chain_snapshot_now:
                if strike not in chain_snapshot_prev:
                    continue
                ce_diff = chain_snapshot_now[strike].get("ce_oi", 0) - chain_snapshot_prev[strike].get("ce_oi", 0)
                pe_diff = chain_snapshot_now[strike].get("pe_oi", 0) - chain_snapshot_prev[strike].get("pe_oi", 0)
                if ce_diff > 0: ce_oi_added += ce_diff
                else: ce_oi_shed += abs(ce_diff)
                if pe_diff > 0: pe_oi_added += pe_diff
                else: pe_oi_shed += abs(pe_diff)

            # PE writers adding = support forming below = bullish
            if pe_oi_added > ce_oi_added * 1.5 and ce_oi_shed > pe_oi_shed:
                return BUY_CE, 0.70, f"PE writing {pe_oi_added:,.0f} + CE unwinding"

            # CE writers adding = resistance above = bearish
            if ce_oi_added > pe_oi_added * 1.5 and pe_oi_shed > ce_oi_shed:
                return BUY_PE, 0.70, f"CE writing {ce_oi_added:,.0f} + PE unwinding"

            return NO_TRADE, 0, "OI flow neutral"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class IVSkewStrategy(BaseStrategy):
    """IV Percentile + Skew — buy cheap options"""
    name = "iv_skew"

    def signal(self, df: pd.DataFrame, iv_percentile: float = 50,
               skew: float = 0, **kwargs) -> Tuple[int, float, str]:
        try:
            if iv_percentile > 55:
                return NO_TRADE, 0, f"IV expensive ({iv_percentile:.0f}th pct)"

            rsi_val = rsi(df["close"]).iloc[-1]

            if skew > 3 and rsi_val > 50:
                # PE vol premium = market fears down, fade with CE
                return BUY_CE, 0.60 + (30 - iv_percentile) / 100, "IV skew + trend up"

            if skew < -2 and rsi_val < 50:
                return BUY_PE, 0.60 + (30 - iv_percentile) / 100, "IV reverse skew + trend down"

            if iv_percentile < 20:
                # Extremely cheap — directional based on trend
                if rsi_val > 55:
                    return BUY_CE, 0.65, f"IV very cheap ({iv_percentile:.0f}th pct)"
                elif rsi_val < 45:
                    return BUY_PE, 0.65, f"IV very cheap ({iv_percentile:.0f}th pct)"

            return NO_TRADE, 0, "IV skew neutral"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class BBSqueezeStrategy(BaseStrategy):
    """Bollinger Band Squeeze — volatility breakout"""
    name = "bb_squeeze"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        if len(df) < 25:
            return NO_TRADE, 0, "Insufficient data"
        try:
            squeeze = bb_squeeze(df["high"], df["low"], df["close"])
            was_squeezed = squeeze.iloc[-2]
            is_squeezed = squeeze.iloc[-1]
            rsi_val = rsi(df["close"]).iloc[-1]
            roc_val = rate_of_change(df["close"], 3).iloc[-1]

            if was_squeezed and not is_squeezed:
                # Squeeze just released!
                if roc_val > 0 and rsi_val > 48:
                    return BUY_CE, 0.78, f"BB Squeeze breakout UP, ROC {roc_val:.1f}%"
                elif roc_val < 0 and rsi_val < 52:
                    return BUY_PE, 0.78, f"BB Squeeze breakdown DOWN, ROC {roc_val:.1f}%"

            if is_squeezed:
                candles_in_squeeze = int(squeeze.iloc[-10:].sum())
                return NO_TRADE, 0, f"In squeeze for {candles_in_squeeze} candles — prepare"

            return NO_TRADE, 0, "No squeeze setup"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class MTFConfluenceStrategy(BaseStrategy):
    """Multi-Timeframe Confluence"""
    name = "mtf"

    def signal(self, df_1d: pd.DataFrame = None, df_1h: pd.DataFrame = None,
               df_15m: pd.DataFrame = None, df_5m: pd.DataFrame = None, **kwargs) -> Tuple[int, float, str]:
        dfs = {"1D": df_1d, "1H": df_1h, "15M": df_15m, "5M": df_5m}
        signals = {}

        for tf, df in dfs.items():
            if df is None or len(df) < 5:
                continue
            e9 = ema(df["close"], 9).iloc[-1]
            e21 = ema(df["close"], 21).iloc[-1]
            r = rsi(df["close"]).iloc[-1]
            price = df["close"].iloc[-1]
            v = vwap(df["high"], df["low"], df["close"], df["volume"]).iloc[-1]

            if e9 > e21 and r > 50 and price > v:
                signals[tf] = BUY_CE
            elif e9 < e21 and r < 50 and price < v:
                signals[tf] = BUY_PE
            else:
                signals[tf] = NO_TRADE

        if not signals:
            return NO_TRADE, 0, "No MTF data"

        bull = sum(1 for s in signals.values() if s == BUY_CE)
        bear = sum(1 for s in signals.values() if s == BUY_PE)
        total = len(signals)

        if bull == total:
            return BUY_CE, 0.85, f"MTF full alignment BULLISH ({total}/{total})"
        elif bear == total:
            return BUY_PE, 0.85, f"MTF full alignment BEARISH ({total}/{total})"
        elif bull >= total * 0.75:
            return BUY_CE, 0.65, f"MTF partial BULL ({bull}/{total})"
        elif bear >= total * 0.75:
            return BUY_PE, 0.65, f"MTF partial BEAR ({bear}/{total})"

        return NO_TRADE, 0, f"MTF divergence (Bull:{bull} Bear:{bear})"


class PsychLevelStrategy(BaseStrategy):
    """Psychological round number analysis"""
    name = "psych"

    def signal(self, df: pd.DataFrame, round_size: int = 100, **kwargs) -> Tuple[int, float, str]:
        try:
            close = df["close"].iloc[-1]
            rsi_val = rsi(df["close"]).iloc[-1]
            nearest_round = round(close / round_size) * round_size
            dist_pct = abs(close - nearest_round) / nearest_round

            if dist_pct < 0.003:
                # At a round number — check context
                prev_touches = sum(
                    1 for p in df["close"].tail(20)
                    if abs(p - nearest_round) / nearest_round < 0.003
                )
                if prev_touches >= 2 and rsi_val < 45:
                    return BUY_CE, 0.65, f"Psych support at {nearest_round}, RSI {rsi_val:.0f}"
                elif prev_touches >= 2 and rsi_val > 55:
                    return BUY_PE, 0.65, f"Psych resistance at {nearest_round}, RSI {rsi_val:.0f}"

            # Breakout from round number
            if close > nearest_round * 1.003 and df["close"].iloc[-2] < nearest_round:
                return BUY_CE, 0.72, f"Round level breakout above {nearest_round}"
            if close < nearest_round * 0.997 and df["close"].iloc[-2] > nearest_round:
                return BUY_PE, 0.72, f"Round level breakdown below {nearest_round}"

            return NO_TRADE, 0, f"Not near round level {nearest_round}"
        except Exception as e:
            return NO_TRADE, 0, str(e)


class GammaScalpStrategy(BaseStrategy):
    """Gamma scalping setup detector"""
    name = "gamma_scalp"

    def signal(self, df: pd.DataFrame, vix: float = 15,
               days_to_expiry: int = 3, **kwargs) -> Tuple[int, float, str]:
        try:
            adx_val = adx(df["high"], df["low"], df["close"]).iloc[-1]
            _, _, bb_width_series = bollinger_bands(df["close"])[:3]
            rsi_val = rsi(df["close"]).iloc[-1]

            # Good for gamma scalp: low ADX, moderate VIX, near expiry
            if adx_val < 20 and 12 < vix < 20 and days_to_expiry <= 3:
                conf = 0.60 + (20 - adx_val) / 100
                return BUY_CE if rsi_val > 50 else BUY_PE, round(conf, 2), \
                    f"Gamma scalp: ADX {adx_val:.0f}, VIX {vix:.1f}, DTE {days_to_expiry}"

            return NO_TRADE, 0, "Not ideal for gamma scalp"
        except Exception as e:
            return NO_TRADE, 0, str(e)


# ── Strategy Engine ────────────────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, weights: Dict[str, float] = None):
        self.strategies: List[BaseStrategy] = [
            SMCStrategy(), ORBStrategy(), GreeksStrategy(), FibSRStrategy(),
            VIXStrategy(), OIFlowStrategy(), IVSkewStrategy(), BBSqueezeStrategy(),
            MTFConfluenceStrategy(), PsychLevelStrategy(), GammaScalpStrategy(),
        ]
        self.weights = weights or {s.name: 1.0 / len(self.strategies) for s in self.strategies}
        self.min_strategies_agree = 3
        self.min_score = 0.38

    def evaluate(self, df: pd.DataFrame, context: Dict = None) -> Tuple[int, float, Dict]:
        """
        Returns (signal, aggregate_score, details_per_strategy)
        """
        context = context or {}
        results = {}

        for strategy in self.strategies:
            try:
                sig, conf, reason = strategy.signal(df, **context)
                results[strategy.name] = {
                    "signal": sig,
                    "confidence": conf,
                    "reason": reason,
                    "weight": self.weights.get(strategy.name, 0.1),
                }
            except Exception as e:
                results[strategy.name] = {
                    "signal": NO_TRADE, "confidence": 0,
                    "reason": str(e), "weight": 0
                }

        # Weighted voting
        bull_score = sum(
            r["confidence"] * r["weight"]
            for r in results.values()
            if r["signal"] == BUY_CE
        )
        bear_score = sum(
            r["confidence"] * r["weight"]
            for r in results.values()
            if r["signal"] == BUY_PE
        )
        bull_votes = sum(1 for r in results.values() if r["signal"] == BUY_CE)
        bear_votes = sum(1 for r in results.values() if r["signal"] == BUY_PE)

        final_signal = NO_TRADE
        final_score = 0.0

        if bull_votes >= self.min_strategies_agree and bull_score >= self.min_score:
            if bull_score > bear_score:
                final_signal = BUY_CE
                final_score = bull_score

        if bear_votes >= self.min_strategies_agree and bear_score >= self.min_score:
            if bear_score > bull_score:
                final_signal = BUY_PE
                final_score = bear_score

        logger.info(f"Strategy vote: CE={bull_votes}({bull_score:.2f}) PE={bear_votes}({bear_score:.2f}) → {'BUY_CE' if final_signal==1 else 'BUY_PE' if final_signal==-1 else 'NO_TRADE'}")
        return final_signal, round(final_score, 3), results

    def update_weights(self, performance: Dict[str, float]):
        """Update weights based on win rates"""
        total = sum(performance.values()) or 1
        for name, win_rate in performance.items():
            self.weights[name] = round(win_rate / total, 4)
        logger.info(f"Strategy weights updated: {self.weights}")
