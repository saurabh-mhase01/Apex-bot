import logging
import math
import numpy as np
import pandas as pd
from typing import Dict, Tuple, List, Optional
from data.indicators import (
    ema, rsi, macd, adx, atr, bollinger_bands, keltner_channel,
    vwap, supertrend, fibonacci_levels, pivot_points,
    bb_squeeze, rate_of_change, market_structure, detect_swing_highs_lows
)

logger = logging.getLogger("STRATEGY")

BUY_CE   =  1
BUY_PE   = -1
NO_TRADE =  0


class BaseStrategy:
    name = "base"
    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        return NO_TRADE, 0.0, "Not implemented"


class SMCStrategy(BaseStrategy):
    """
    Smart Money Concepts — BOS, Order Blocks, FVG.
    Uses only price-derived features (structure/EMA/RSI/ADX from df) — no
    external market-data kwargs, so there's nothing here that can silently
    fall back to a fake default.
    """
    name = "smc"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[SMC] INPUT: candles={len(df) if df is not None else 0}")
        if len(df) < 20:
            logger.info("[SMC] OUTPUT: NO_TRADE (insufficient data)")
            return NO_TRADE, 0, "Insufficient data"
        try:
            structure = market_structure(df)
            swings    = detect_swing_highs_lows(df, lookback=3)
            rsi_val   = rsi(df["close"]).iloc[-1]
            adx_val   = adx(df["high"], df["low"], df["close"]).iloc[-1]
            close     = df["close"].iloc[-1]
            ema9      = ema(df["close"], 9).iloc[-1]
            ema21     = ema(df["close"], 21).iloc[-1]

            logger.info(f"[SMC] FEATURES: structure={structure}, RSI={rsi_val:.1f}, ADX={adx_val:.1f}, "
                        f"EMA9={ema9:.0f}, EMA21={ema21:.0f}, close={close:.2f}")

            last_swing_low  = (swings["swing_low"].dropna().iloc[-1]
                               if not swings["swing_low"].dropna().empty else None)
            last_swing_high = (swings["swing_high"].dropna().iloc[-1]
                               if not swings["swing_high"].dropna().empty else None)

            if structure == "BULLISH" and last_swing_low \
                    and close > last_swing_low * 1.001 and rsi_val < 65:
                conf = 0.70 if rsi_val < 55 else 0.55
                logger.info(f"[SMC] OUTPUT: BUY_CE conf={conf}")
                return BUY_CE, conf, f"BOS Bullish, OB retest, RSI {rsi_val:.0f}"

            if structure == "BEARISH" and last_swing_high \
                    and close < last_swing_high * 0.999 and rsi_val > 35:
                conf = 0.70 if rsi_val > 45 else 0.55
                logger.info(f"[SMC] OUTPUT: BUY_PE conf={conf}")
                return BUY_PE, conf, f"BOS Bearish, OB retest, RSI {rsi_val:.0f}"

            if structure == "SIDEWAYS" and adx_val > 30:
                if ema9 < ema21 and rsi_val < 35 and close < ema9:
                    conf = min(0.70, 0.45 + adx_val / 200)
                    logger.info(f"[SMC] OUTPUT: BUY_PE conf={round(conf,2)} (structure override)")
                    return BUY_PE, round(conf, 2), \
                        f"EMA bearish + RSI={rsi_val:.0f} + ADX={adx_val:.0f} (structure override)"

                if ema9 > ema21 and rsi_val > 65 and close > ema9:
                    conf = min(0.70, 0.45 + adx_val / 200)
                    logger.info(f"[SMC] OUTPUT: BUY_CE conf={round(conf,2)} (structure override)")
                    return BUY_CE, round(conf, 2), \
                        f"EMA bullish + RSI={rsi_val:.0f} + ADX={adx_val:.0f} (structure override)"

            logger.info(f"[SMC] OUTPUT: NO_TRADE (structure={structure})")
            return NO_TRADE, 0, f"No SMC setup ({structure})"
        except Exception as e:
            logger.error(f"[SMC] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class ORBStrategy(BaseStrategy):
    name = "orb"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[ORB] INPUT: candles={len(df) if df is not None else 0}")
        if len(df) < 5:
            logger.info("[ORB] OUTPUT: NO_TRADE (insufficient data)")
            return NO_TRADE, 0, "Insufficient data"
        try:
            # BUG FIX: df.iloc[0] used to grab the first candle of the ENTIRE
            # multi-day fetch window (i.e. a candle from days ago), not today's
            # actual opening range. Filter to today's session first.
            today = df.index[-1].date()
            today_df = df[df.index.date == today]
            if len(today_df) < 2:
                logger.info("[ORB] OUTPUT: NO_TRADE (no candles for today's session yet)")
                return NO_TRADE, 0, "No candles for today's session yet"

            orb_high  = today_df["high"].iloc[0]
            orb_low   = today_df["low"].iloc[0]
            orb_range = orb_high - orb_low
            vwap_val  = vwap(df["high"], df["low"], df["close"], df["volume"]).iloc[-1]
            close     = df["close"].iloc[-1]

            # BUG FIX: index instruments (NSE_INDEX|...) report volume=0 on every
            # candle from Angel One's historical API — vol_surge was permanently
            # False, so ORB could never fire regardless of price action. Detect
            # zero-volume data and fall back to a range-expansion confirmation
            # (current candle range vs recent average range) instead.
            vol_avg = df["volume"].rolling(10).mean().iloc[-1]
            vol_now = df["volume"].iloc[-1]
            has_real_volume = df["volume"].sum() > 0

            if has_real_volume:
                confirm = vol_now > vol_avg * 1.3
                confirm_reason = "vol surge"
            else:
                rng_avg = (df["high"] - df["low"]).rolling(10).mean().iloc[-1]
                rng_now = df["high"].iloc[-1] - df["low"].iloc[-1]
                confirm = rng_now > rng_avg * 1.2
                confirm_reason = "range expansion (no real volume data)"

            logger.info(f"[ORB] FEATURES: orb_high={orb_high}, orb_low={orb_low}, vwap={vwap_val:.2f}, "
                        f"close={close:.2f}, has_real_volume={has_real_volume}, confirm={confirm} ({confirm_reason})")

            if close > orb_high * 1.001 and close > vwap_val and confirm:
                conf = 0.75 if orb_range > 30 else 0.60
                logger.info(f"[ORB] OUTPUT: BUY_CE conf={conf}")
                return BUY_CE, conf, f"ORB breakout UP, VWAP confirmed, {confirm_reason}"
            if close < orb_low * 0.999 and close < vwap_val and confirm:
                conf = 0.75 if orb_range > 30 else 0.60
                logger.info(f"[ORB] OUTPUT: BUY_PE conf={conf}")
                return BUY_PE, conf, f"ORB breakdown DOWN, below VWAP, {confirm_reason}"
            logger.info("[ORB] OUTPUT: NO_TRADE (price within ORB range)")
            return NO_TRADE, 0, "Price within ORB range"
        except Exception as e:
            logger.error(f"[ORB] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class GreeksStrategy(BaseStrategy):
    """
    Options Greeks momentum — Delta/Gamma acceleration.

    iv_percentile, atm_delta, theta_pct, days_to_expiry are all REQUIRED
    (no defaults). These come from build_strategy_context() using live
    option-chain data. If any of them is missing from context, this
    strategy must raise (not silently assume IV=50th pct / delta=0.5 /
    theta=0.10 / DTE=7) — the outer StrategyEngine.evaluate() catches the
    exception and records NO_TRADE for this strategy, which is the correct
    safe behavior.
    """
    name = "greeks"

    def signal(self, df: pd.DataFrame, iv_percentile: float, atm_delta: float,
               theta_pct: float, days_to_expiry: int, **kwargs) -> Tuple[int, float, str]:
        logger.info(
            f"[GREEKS] INPUT: candles={len(df) if df is not None else 0}, "
            f"iv_percentile={iv_percentile}, atm_delta={atm_delta}, "
            f"theta_pct={theta_pct}, days_to_expiry={days_to_expiry}"
        )
        try:
            rsi_val                      = rsi(df["close"]).iloc[-1]
            macd_line, signal_line, hist = macd(df["close"])
            macd_bullish = hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]
            macd_bearish = hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2]
            logger.info(f"[GREEKS] FEATURES: rsi={rsi_val:.1f}, macd_bullish={macd_bullish}, macd_bearish={macd_bearish}")

            if iv_percentile > 60:
                logger.info(f"[GREEKS] OUTPUT: NO_TRADE (IV too expensive {iv_percentile:.0f}th pct)")
                return NO_TRADE, 0, f"IV too expensive ({iv_percentile:.0f}th pct)"

            if days_to_expiry == 0:
                logger.info("[GREEKS] OUTPUT: NO_TRADE (DTE=0, expiry day)")
                return NO_TRADE, 0, "DTE=0 — expiry day, skip Greeks directional buy"
            if theta_pct > 0.20:
                logger.info(f"[GREEKS] OUTPUT: NO_TRADE (too close to expiry, DTE={days_to_expiry}, theta={theta_pct:.3f})")
                return NO_TRADE, 0, f"Too close to expiry (DTE={days_to_expiry}, theta={theta_pct:.3f})"

            if 0.35 <= atm_delta <= 0.65 and macd_bullish and rsi_val > 50:
                conf = round(0.65 + (0.60 - iv_percentile / 100) * 0.3, 2)
                logger.info(f"[GREEKS] OUTPUT: BUY_CE conf={conf}")
                return BUY_CE, conf, f"Greek momentum CE, IVP {iv_percentile:.0f}"

            if 0.35 <= atm_delta <= 0.65 and macd_bearish and rsi_val < 50:
                conf = round(0.65 + (0.60 - iv_percentile / 100) * 0.3, 2)
                logger.info(f"[GREEKS] OUTPUT: BUY_PE conf={conf}")
                return BUY_PE, conf, f"Greek momentum PE, IVP {iv_percentile:.0f}"

            logger.info("[GREEKS] OUTPUT: NO_TRADE (no setup)")
            return NO_TRADE, 0, "No greeks setup"
        except Exception as e:
            logger.error(f"[GREEKS] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class FibSRStrategy(BaseStrategy):
    name = "fib_sr"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[FIB_SR] INPUT: candles={len(df) if df is not None else 0}")
        if len(df) < 20:
            logger.info("[FIB_SR] OUTPUT: NO_TRADE (insufficient data)")
            return NO_TRADE, 0, "Insufficient data"
        try:
            period_high = df["high"].tail(20).max()
            period_low  = df["low"].tail(20).min()
            fibs        = fibonacci_levels(period_high, period_low)
            close       = df["close"].iloc[-1]
            rsi_val     = rsi(df["close"]).iloc[-1]
            prev        = df.tail(2).iloc[0]
            pivots      = pivot_points(prev["high"], prev["low"], prev["close"])

            fib_zones     = [fibs["0.382"], fibs["0.5"], fibs["0.618"]]
            support_zones = [pivots["s1"], pivots["s2"], pivots["pp"]]
            resist_zones  = [pivots["r1"], pivots["r2"]]

            def near(price, level, pct=0.002):
                return abs(price - level) / level < pct

            at_fib_support = any(near(close, z) for z in fib_zones if z < close * 1.003)
            at_fib_resist  = any(near(close, z) for z in fib_zones if z > close * 0.997)
            at_support     = any(near(close, z) for z in support_zones)
            at_resist      = any(near(close, z) for z in resist_zones)

            logger.info(f"[FIB_SR] FEATURES: close={close:.2f}, rsi={rsi_val:.1f}, "
                        f"at_fib_support={at_fib_support}, at_fib_resist={at_fib_resist}, "
                        f"at_support={at_support}, at_resist={at_resist}")

            if (at_fib_support or at_support) and rsi_val < 45:
                logger.info("[FIB_SR] OUTPUT: BUY_CE conf=0.68")
                return BUY_CE, 0.68, f"Fib/SR support confluence, RSI {rsi_val:.0f}"
            if (at_fib_resist or at_resist) and rsi_val > 55:
                logger.info("[FIB_SR] OUTPUT: BUY_PE conf=0.68")
                return BUY_PE, 0.68, f"Fib/SR resistance confluence, RSI {rsi_val:.0f}"
            logger.info("[FIB_SR] OUTPUT: NO_TRADE (no setup)")
            return NO_TRADE, 0, "No Fib/SR setup"
        except Exception as e:
            logger.error(f"[FIB_SR] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class VIXStrategy(BaseStrategy):
    """vix and vix_prev are REQUIRED — no defaults. Both must come from a live VIX read."""
    name = "vix"

    def signal(self, df: pd.DataFrame, vix: float, vix_prev: float, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[VIX] INPUT: candles={len(df) if df is not None else 0}, vix={vix}, vix_prev={vix_prev}")
        try:
            vix_change = (vix - vix_prev) / vix_prev * 100 if vix_prev else 0
            rsi_val    = rsi(df["close"]).iloc[-1]
            logger.info(f"[VIX] FEATURES: vix_change_pct={vix_change:.2f}, rsi={rsi_val:.1f}")

            if vix_change > 5 and rsi_val < 40:
                logger.info("[VIX] OUTPUT: BUY_CE conf=0.62")
                return BUY_CE, 0.62, f"VIX spike {vix_change:.1f}% + oversold RSI"
            if vix_change > 5 and rsi_val > 60:
                logger.info("[VIX] OUTPUT: BUY_PE conf=0.62")
                return BUY_PE, 0.62, f"VIX spike {vix_change:.1f}% + overbought"
            if vix < 12:
                logger.info(f"[VIX] OUTPUT: NO_TRADE (VIX compressed {vix:.1f})")
                return NO_TRADE, 0, f"VIX compressed ({vix:.1f}), awaiting direction"
            logger.info(f"[VIX] OUTPUT: NO_TRADE (VIX neutral {vix:.1f})")
            return NO_TRADE, 0, f"VIX neutral ({vix:.1f})"
        except Exception as e:
            logger.error(f"[VIX] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class OIFlowStrategy(BaseStrategy):
    name = "oi_flow"

    def signal(self, df: pd.DataFrame, chain_snapshot_prev: Dict = None,
               chain_snapshot_now: Dict = None, **kwargs) -> Tuple[int, float, str]:
        logger.info(
            f"[OI_FLOW] INPUT: prev_strikes={len(chain_snapshot_prev) if chain_snapshot_prev else 0}, "
            f"now_strikes={len(chain_snapshot_now) if chain_snapshot_now else 0}"
        )
        if not chain_snapshot_prev or not chain_snapshot_now:
            logger.info("[OI_FLOW] OUTPUT: NO_TRADE (no OI chain data yet — needs 2 cycles)")
            return NO_TRADE, 0, "No OI chain data"
        try:
            pe_oi_added = ce_oi_added = pe_oi_shed = ce_oi_shed = 0
            for strike in chain_snapshot_now:
                if strike not in chain_snapshot_prev:
                    continue
                ce_diff = (chain_snapshot_now[strike].get("ce_oi", 0)
                           - chain_snapshot_prev[strike].get("ce_oi", 0))
                pe_diff = (chain_snapshot_now[strike].get("pe_oi", 0)
                           - chain_snapshot_prev[strike].get("pe_oi", 0))
                if ce_diff > 0: ce_oi_added += ce_diff
                else:           ce_oi_shed  += abs(ce_diff)
                if pe_diff > 0: pe_oi_added += pe_diff
                else:           pe_oi_shed  += abs(pe_diff)

            logger.info(f"[OI_FLOW] FEATURES: ce_added={ce_oi_added}, ce_shed={ce_oi_shed}, "
                        f"pe_added={pe_oi_added}, pe_shed={pe_oi_shed}")

            if pe_oi_added > ce_oi_added * 1.5 and ce_oi_shed > pe_oi_shed:
                logger.info("[OI_FLOW] OUTPUT: BUY_CE conf=0.70")
                return BUY_CE, 0.70, f"PE writing {pe_oi_added:,.0f} + CE unwinding"
            if ce_oi_added > pe_oi_added * 1.5 and pe_oi_shed > ce_oi_shed:
                logger.info("[OI_FLOW] OUTPUT: BUY_PE conf=0.70")
                return BUY_PE, 0.70, f"CE writing {ce_oi_added:,.0f} + PE unwinding"
            logger.info("[OI_FLOW] OUTPUT: NO_TRADE (flow neutral)")
            return NO_TRADE, 0, "OI flow neutral"
        except Exception as e:
            logger.error(f"[OI_FLOW] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class IVSkewStrategy(BaseStrategy):
    """iv_percentile is REQUIRED — no default."""
    name = "iv_skew"

    def signal(self, df: pd.DataFrame, iv_percentile: float, skew: float = 0, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[IV_SKEW] INPUT: iv_percentile={iv_percentile}, skew={skew}")
        try:
            if iv_percentile > 55:
                logger.info(f"[IV_SKEW] OUTPUT: NO_TRADE (IV expensive {iv_percentile:.0f}th pct)")
                return NO_TRADE, 0, f"IV expensive ({iv_percentile:.0f}th pct)"
            rsi_val = rsi(df["close"]).iloc[-1]
            logger.info(f"[IV_SKEW] FEATURES: rsi={rsi_val:.1f}")
            if skew > 3 and rsi_val > 50:
                conf = round(0.60 + (30 - iv_percentile) / 100, 2)
                logger.info(f"[IV_SKEW] OUTPUT: BUY_CE conf={conf}")
                return BUY_CE, conf, "IV skew + trend up"
            if skew < -2 and rsi_val < 50:
                conf = round(0.60 + (30 - iv_percentile) / 100, 2)
                logger.info(f"[IV_SKEW] OUTPUT: BUY_PE conf={conf}")
                return BUY_PE, conf, "IV reverse skew + trend down"
            if iv_percentile < 20:
                if rsi_val > 55:
                    logger.info("[IV_SKEW] OUTPUT: BUY_CE conf=0.65 (IV very cheap)")
                    return BUY_CE, 0.65, f"IV very cheap ({iv_percentile:.0f}th pct)"
                elif rsi_val < 45:
                    logger.info("[IV_SKEW] OUTPUT: BUY_PE conf=0.65 (IV very cheap)")
                    return BUY_PE, 0.65, f"IV very cheap ({iv_percentile:.0f}th pct)"
            logger.info("[IV_SKEW] OUTPUT: NO_TRADE (skew neutral)")
            return NO_TRADE, 0, "IV skew neutral"
        except Exception as e:
            logger.error(f"[IV_SKEW] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class BBSqueezeStrategy(BaseStrategy):
    name = "bb_squeeze"

    def signal(self, df: pd.DataFrame, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[BB_SQUEEZE] INPUT: candles={len(df) if df is not None else 0}")
        if len(df) < 25:
            logger.info("[BB_SQUEEZE] OUTPUT: NO_TRADE (insufficient data)")
            return NO_TRADE, 0, "Insufficient data"
        try:
            squeeze      = bb_squeeze(df["high"], df["low"], df["close"])
            was_squeezed = squeeze.iloc[-2]
            is_squeezed  = squeeze.iloc[-1]
            rsi_val      = rsi(df["close"]).iloc[-1]
            roc_val      = rate_of_change(df["close"], 3).iloc[-1]

            logger.info(f"[BB_SQUEEZE] FEATURES: was_squeezed={was_squeezed}, is_squeezed={is_squeezed}, "
                        f"rsi={rsi_val:.1f}, roc={roc_val:.2f}")

            if was_squeezed and not is_squeezed:
                if roc_val > 0 and rsi_val > 48:
                    logger.info("[BB_SQUEEZE] OUTPUT: BUY_CE conf=0.78")
                    return BUY_CE, 0.78, f"BB Squeeze breakout UP, ROC {roc_val:.1f}%"
                elif roc_val < 0 and rsi_val < 52:
                    logger.info("[BB_SQUEEZE] OUTPUT: BUY_PE conf=0.78")
                    return BUY_PE, 0.78, f"BB Squeeze breakdown DOWN, ROC {roc_val:.1f}%"
            if is_squeezed:
                candles_in = int(squeeze.iloc[-10:].sum())
                logger.info(f"[BB_SQUEEZE] OUTPUT: NO_TRADE (in squeeze {candles_in} candles)")
                return NO_TRADE, 0, f"In squeeze {candles_in} candles — prepare"
            logger.info("[BB_SQUEEZE] OUTPUT: NO_TRADE (no setup)")
            return NO_TRADE, 0, "No squeeze setup"
        except Exception as e:
            logger.error(f"[BB_SQUEEZE] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class MTFConfluenceStrategy(BaseStrategy):
    name = "mtf"

    def signal(self, df_1d: pd.DataFrame = None, df_1h: pd.DataFrame = None,
               df_15m: pd.DataFrame = None, df_5m: pd.DataFrame = None,
               **kwargs) -> Tuple[int, float, str]:
        logger.info(
            f"[MTF] INPUT: 1d={0 if df_1d is None else len(df_1d)}, "
            f"1h={0 if df_1h is None else len(df_1h)}, "
            f"15m={0 if df_15m is None else len(df_15m)}, "
            f"5m={0 if df_5m is None else len(df_5m)}"
        )
        dfs     = {"1D": df_1d, "1H": df_1h, "15M": df_15m, "5M": df_5m}
        signals = {}
        min_candles = 21
        for tf, df in dfs.items():
            if df is None or len(df) < min_candles:
                logger.info(f"[MTF] skip {tf} (candles={0 if df is None else len(df)})")
                continue

            e9    = ema(df["close"], 9).iloc[-1]
            e21   = ema(df["close"], 21).iloc[-1]
            r     = rsi(df["close"]).iloc[-1]
            price = df["close"].iloc[-1]
            v     = vwap(df["high"], df["low"], df["close"], df["volume"]).iloc[-1]

            if pd.isna(e9) or pd.isna(e21) or pd.isna(r) or pd.isna(v):
                logger.warning(f"[MTF] skip {tf} due to NaN indicators")
                continue

            if   e9 > e21 and r > 50 and price > v:
                signals[tf] = BUY_CE
            elif e9 < e21 and r < 50 and price < v:
                signals[tf] = BUY_PE
            else:
                signals[tf] = NO_TRADE

        logger.info(f"[MTF] FEATURES: per_timeframe_signals={signals}")

        if not signals:
            logger.info("[MTF] OUTPUT: NO_TRADE (no MTF data available)")
            return NO_TRADE, 0, "No MTF data"
        bull  = sum(1 for s in signals.values() if s == BUY_CE)
        bear  = sum(1 for s in signals.values() if s == BUY_PE)
        total = len(signals)
        if bull == total:
            logger.info(f"[MTF] OUTPUT: BUY_CE conf=0.85 (full bull {bull}/{total})")
            return BUY_CE, 0.85, f"MTF full BULLISH ({total}/{total})"
        elif bear == total:
            logger.info(f"[MTF] OUTPUT: BUY_PE conf=0.85 (full bear {bear}/{total})")
            return BUY_PE, 0.85, f"MTF full BEARISH ({total}/{total})"
        elif bull >= total * 0.75:
            logger.info(f"[MTF] OUTPUT: BUY_CE conf=0.65 (partial bull {bull}/{total})")
            return BUY_CE, 0.65, f"MTF partial BULL ({bull}/{total})"
        elif bear >= total * 0.75:
            logger.info(f"[MTF] OUTPUT: BUY_PE conf=0.65 (partial bear {bear}/{total})")
            return BUY_PE, 0.65, f"MTF partial BEAR ({bear}/{total})"
        logger.info(f"[MTF] OUTPUT: NO_TRADE (divergence bull={bull} bear={bear})")
        return NO_TRADE, 0, f"MTF divergence (Bull:{bull} Bear:{bear})"


class PsychLevelStrategy(BaseStrategy):
    """
    round_size is a strategy PARAMETER (which round-number grid to check),
    not a market-data value, so a default here is fine and intentional.
    """
    name = "psych"

    def signal(self, df: pd.DataFrame, round_size: int = 100, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[PSYCH] INPUT: candles={len(df) if df is not None else 0}, round_size={round_size}")
        try:
            close         = df["close"].iloc[-1]
            rsi_val       = rsi(df["close"]).iloc[-1]
            nearest_round = round(close / round_size) * round_size
            dist_pct      = abs(close - nearest_round) / nearest_round

            logger.info(f"[PSYCH] FEATURES: close={close:.2f}, nearest_round={nearest_round}, "
                        f"dist_pct={dist_pct:.4f}, rsi={rsi_val:.1f}")

            if dist_pct < 0.003:
                prev_touches = sum(
                    1 for p in df["close"].tail(20)
                    if abs(p - nearest_round) / nearest_round < 0.003
                )
                if prev_touches >= 2 and rsi_val < 45:
                    logger.info("[PSYCH] OUTPUT: BUY_CE conf=0.65")
                    return BUY_CE, 0.65, f"Psych support {nearest_round}, RSI {rsi_val:.0f}"
                elif prev_touches >= 2 and rsi_val > 55:
                    logger.info("[PSYCH] OUTPUT: BUY_PE conf=0.65")
                    return BUY_PE, 0.65, f"Psych resistance {nearest_round}, RSI {rsi_val:.0f}"

            if close > nearest_round * 1.003 and df["close"].iloc[-2] < nearest_round:
                logger.info("[PSYCH] OUTPUT: BUY_CE conf=0.72 (round breakout)")
                return BUY_CE, 0.72, f"Round level breakout above {nearest_round}"
            if close < nearest_round * 0.997 and df["close"].iloc[-2] > nearest_round:
                logger.info("[PSYCH] OUTPUT: BUY_PE conf=0.72 (round breakdown)")
                return BUY_PE, 0.72, f"Round level breakdown below {nearest_round}"
            logger.info(f"[PSYCH] OUTPUT: NO_TRADE (not near round level {nearest_round})")
            return NO_TRADE, 0, f"Not near round level {nearest_round}"
        except Exception as e:
            logger.error(f"[PSYCH] ERROR: {e}")
            return NO_TRADE, 0, str(e)


class GammaScalpStrategy(BaseStrategy):
    """vix and days_to_expiry are REQUIRED — no defaults."""
    name = "gamma_scalp"

    def signal(self, df: pd.DataFrame, vix: float, days_to_expiry: int, **kwargs) -> Tuple[int, float, str]:
        logger.info(f"[GAMMA_SCALP] INPUT: vix={vix}, days_to_expiry={days_to_expiry}")
        try:
            adx_val                          = adx(df["high"], df["low"], df["close"]).iloc[-1]
            bb_upper, bb_mid, bb_lower, bb_width = bollinger_bands(df["close"])
            bb_width_val = bb_width.iloc[-1]
            rsi_val      = rsi(df["close"]).iloc[-1]

            logger.info(f"[GAMMA_SCALP] FEATURES: adx={adx_val:.1f}, bb_width={bb_width_val:.4f}, rsi={rsi_val:.1f}")

            if (adx_val < 20 and 12 < vix < 20
                    and days_to_expiry <= 3 and bb_width_val > 0.005):
                conf      = round(0.60 + (20 - adx_val) / 100, 2)
                direction = BUY_CE if rsi_val > 50 else BUY_PE
                logger.info(f"[GAMMA_SCALP] OUTPUT: {'BUY_CE' if direction==1 else 'BUY_PE'} conf={conf}")
                return direction, conf, \
                    f"Gamma scalp: ADX {adx_val:.0f}, VIX {vix:.1f}, DTE {days_to_expiry}"
            logger.info("[GAMMA_SCALP] OUTPUT: NO_TRADE (not ideal conditions)")
            return NO_TRADE, 0, "Not ideal for gamma scalp"
        except Exception as e:
            logger.error(f"[GAMMA_SCALP] ERROR: {e}")
            return NO_TRADE, 0, str(e)


# ── Strategy Engine ────────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Regime-aware voting threshold.
    When the regime classifier is confident (≥70%), lower min_strategies_agree
    from 3 to 2 so the bot can act on high-conviction market conditions even
    when data-limited strategies (oi_flow, mtf) can't contribute.

    Also adds a regime_boost: in a strong trending regime, a direction that
    matches the regime gets +0.15 score so it clears the 0.38 floor more easily.
    """

    TRENDING_REGIMES = {"TRENDING_BULL", "TRENDING_BEAR"}

    def __init__(self, weights: Dict[str, float] = None):
        self.strategies: List[BaseStrategy] = [
            SMCStrategy(), ORBStrategy(), GreeksStrategy(), FibSRStrategy(),
            VIXStrategy(), OIFlowStrategy(), IVSkewStrategy(), BBSqueezeStrategy(),
            MTFConfluenceStrategy(), PsychLevelStrategy(), GammaScalpStrategy(),
        ]
        self.weights = weights or {
            s.name: 1.0 / len(self.strategies) for s in self.strategies
        }

        # ── Validate weights cover every active strategy, and nothing else ──
        strategy_names = {s.name for s in self.strategies}
        configured_names = set(self.weights.keys())

        missing = strategy_names - configured_names
        if missing:
            logger.error(
                f"[STRATEGY_ENGINE_INIT] Missing weights for active strategies: {missing} "
                f"— these are silently getting a 0.1 fallback. Add them to config.yaml."
            )

        orphaned = configured_names - strategy_names
        if orphaned:
            logger.warning(
                f"[STRATEGY_ENGINE_INIT] strategy_weights has entries with no matching "
                f"strategy (dead weight, contributes nothing): {orphaned}"
            )

        total = sum(self.weights.get(s.name, 0.1) for s in self.strategies)
        if abs(total - 1.0) > 0.01:
            logger.warning(
                f"[STRATEGY_ENGINE_INIT] Effective weights sum to {total:.3f}, not 1.0 "
                f"— min_score comparisons will be skewed."
            )

        self.min_strategies_agree = 3
        self.min_strategies_agree_relaxed = 2
        self.min_score = 0.38
        logger.info(f"[STRATEGY_ENGINE_INIT] weights={self.weights}")

    def evaluate(self, df: pd.DataFrame, context: Dict, regime: str,
                 regime_confidence: float) -> Tuple[int, float, Dict]:
        """
        regime and regime_confidence are REQUIRED — no defaults.
        Previously regime_confidence silently defaulted to 0.5 whenever the
        caller forgot to pass it, meaning the regime-based threshold relax
        and score boost NEVER activated even in a genuinely high-confidence
        trending regime, and you'd have no idea from the logs. Now the caller
        (BotEngine) must explicitly pass the real, just-computed regime
        confidence on every call.
        """
        logger.info(
            f"[STRATEGY_EVALUATE] INPUT: candles={len(df) if df is not None else 0}, "
            f"regime={regime}, regime_confidence={regime_confidence}, "
            f"context_keys={list(context.keys()) if context else []}"
        )
        context = context or {}
        results = {}

        for strategy in self.strategies:
            try:
                sig, conf, reason = strategy.signal(df=df, **context)
                results[strategy.name] = {
                    "signal":     sig,
                    "confidence": conf,
                    "reason":     reason,
                    "weight":     self.weights.get(strategy.name, 0.1),
                }
            except Exception as e:
                # A strategy missing a required market-data kwarg lands here —
                # that is the intended safe path, not a bug to silence.
                logger.warning(f"[STRATEGY_EVALUATE] {strategy.name} raised, treating as NO_TRADE: {e}")
                results[strategy.name] = {
                    "signal": NO_TRADE, "confidence": 0,
                    "reason": f"error: {e}", "weight": 0,
                }

        bull_score = sum(
            r["confidence"] * r["weight"]
            for r in results.values() if r["signal"] == BUY_CE
        )
        bear_score = sum(
            r["confidence"] * r["weight"]
            for r in results.values() if r["signal"] == BUY_PE
        )
        bull_votes = sum(1 for r in results.values() if r["signal"] == BUY_CE)
        bear_votes = sum(1 for r in results.values() if r["signal"] == BUY_PE)

        is_trending    = regime in self.TRENDING_REGIMES
        high_confidence = regime_confidence >= 0.70
        threshold = (self.min_strategies_agree_relaxed
                     if (is_trending and high_confidence)
                     else self.min_strategies_agree)

        regime_boost = 0.15 if (is_trending and high_confidence) else 0.0

        final_signal = NO_TRADE
        final_score  = 0.0

        bull_boosted = bull_score + (regime_boost if regime == "TRENDING_BULL" else 0)
        bear_boosted = bear_score + (regime_boost if regime == "TRENDING_BEAR" else 0)

        if bull_votes >= threshold and bull_boosted >= self.min_score:
            if bull_boosted > bear_boosted:
                final_signal = BUY_CE
                final_score  = bull_boosted

        if bear_votes >= threshold and bear_boosted >= self.min_score:
            if bear_boosted > bull_boosted:
                final_signal = BUY_PE
                final_score  = bear_boosted

        logger.info(
            f"[STRATEGY_EVALUATE] OUTPUT: CE={bull_votes}votes({bull_score:.2f}+{regime_boost:.2f}boost) "
            f"PE={bear_votes}votes({bear_score:.2f}+{regime_boost:.2f}boost) "
            f"threshold={threshold} regime={regime}({regime_confidence:.0%}) → "
            f"{'BUY_CE' if final_signal==1 else 'BUY_PE' if final_signal==-1 else 'NO_TRADE'} "
            f"score={round(final_score,3)}"
        )
        return final_signal, round(final_score, 3), results

    def update_weights(self, performance: Dict[str, float]):
        logger.info(f"[STRATEGY_UPDATE_WEIGHTS] INPUT: {performance}")
        total = sum(performance.values()) or 1
        for name, win_rate in performance.items():
            self.weights[name] = round(win_rate / total, 4)
        logger.info(f"[STRATEGY_UPDATE_WEIGHTS] OUTPUT: {self.weights}")