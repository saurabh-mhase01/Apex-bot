"""Technical Indicators — pure pandas/numpy, no TA-Lib dependency"""
import numpy as np
import pandas as pd
from typing import Tuple, Optional
import logging; 

logger = logging.getLogger("INDICATORS")


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr_val = tr.ewm(span=period, adjust=False).mean()
    dm_plus = (high - high.shift()).clip(lower=0)
    dm_minus = (low.shift() - low).clip(lower=0)
    dm_plus = dm_plus.where(dm_plus > dm_minus, 0)
    dm_minus = dm_minus.where(dm_minus > dm_plus, 0)
    di_plus = 100 * ema(dm_plus, period) / atr_val
    di_minus = 100 * ema(dm_minus, period) / atr_val
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return ema(dx, period)

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    width = (upper - lower) / mid
    return upper, mid, lower, width

def keltner_channel(high: pd.Series, low: pd.Series, close: pd.Series,
                    period: int = 20, atr_mult: float = 1.5):
    mid = ema(close, period)
    atr_val = atr(high, low, close, period)
    upper = mid + atr_mult * atr_val
    lower = mid - atr_mult * atr_val
    return upper, mid, lower

def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    tp = (high + low + close) / 3
    vol_cum = volume.cumsum()
    if volume.sum() == 0:
        logger.warning("[VWAP] volume is all-zero — falling back to typical-price average (no real volume data)")
        return tp.expanding().mean()
    return (tp * volume).cumsum() / vol_cum.replace(0, np.nan)

def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 7, multiplier: float = 3.0) -> Tuple[pd.Series, pd.Series]:
    atr_val = atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper_band = hl2 + multiplier * atr_val
    lower_band = hl2 - multiplier * atr_val

    supertrend_vals = pd.Series(index=close.index, dtype=float)
    direction = pd.Series(index=close.index, dtype=int)

    for i in range(1, len(close)):
        if close.iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        supertrend_vals.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]

    return supertrend_vals, direction

def fibonacci_levels(high: float, low: float) -> dict:
    diff = high - low
    return {
        "0": low,
        "0.236": low + 0.236 * diff,
        "0.382": low + 0.382 * diff,
        "0.5": low + 0.5 * diff,
        "0.618": low + 0.618 * diff,
        "0.786": low + 0.786 * diff,
        "1": high,
        "1.272": low + 1.272 * diff,
        "1.618": low + 1.618 * diff,
    }

def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
    pp = (prev_high + prev_low + prev_close) / 3
    return {
        "pp": pp,
        "r1": 2 * pp - prev_low,
        "r2": pp + (prev_high - prev_low),
        "r3": prev_high + 2 * (pp - prev_low),
        "s1": 2 * pp - prev_high,
        "s2": pp - (prev_high - prev_low),
        "s3": prev_low - 2 * (prev_high - pp),
    }

def detect_swing_highs_lows(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    df = df.copy()
    df["swing_high"] = df["high"][(df["high"] == df["high"].rolling(lookback * 2 + 1, center=True).max())]
    df["swing_low"] = df["low"][(df["low"] == df["low"].rolling(lookback * 2 + 1, center=True).min())]
    return df

def market_structure(df: pd.DataFrame) -> str:
    """Detect BOS/CHoCH — returns BULLISH / BEARISH / SIDEWAYS"""
    highs = df["high"].rolling(5).max().dropna()
    lows = df["low"].rolling(5).min().dropna()
    if len(highs) < 10:
        return "SIDEWAYS"
    recent_highs = highs.tail(6).values
    recent_lows = lows.tail(6).values
    hh = all(recent_highs[i] < recent_highs[i+1] for i in range(len(recent_highs)-1))
    hl = all(recent_lows[i] < recent_lows[i+1] for i in range(len(recent_lows)-1))
    ll = all(recent_lows[i] > recent_lows[i+1] for i in range(len(recent_lows)-1))
    lh = all(recent_highs[i] > recent_highs[i+1] for i in range(len(recent_highs)-1))
    if hh and hl:
        return "BULLISH"
    elif ll and lh:
        return "BEARISH"
    return "SIDEWAYS"

def bb_squeeze(high: pd.Series, low: pd.Series, close: pd.Series,
               bb_period: int = 20, kc_period: int = 20) -> pd.Series:
    """True when Bollinger Bands are inside Keltner Channel"""
    bb_upper, _, bb_lower, _ = bollinger_bands(close, bb_period)
    kc_upper, _, kc_lower = keltner_channel(high, low, close, kc_period)
    return (bb_upper < kc_upper) & (bb_lower > kc_lower)

def rate_of_change(series: pd.Series, period: int = 10) -> pd.Series:
    return ((series - series.shift(period)) / series.shift(period)) * 100
