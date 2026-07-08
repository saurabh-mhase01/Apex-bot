"""
Backtester — Walk-forward simulation on historical OHLCV data
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np

from data.indicators import rsi, ema, adx, bollinger_bands, vwap, market_structure
from data.Angle_broker_v2 import AngelOneBroker
from core.regime_classifier import MarketRegimeClassifier, NO_DATA_REGIME
from strategies.strategy_engine import StrategyEngine, BUY_CE, BUY_PE, NO_TRADE

logger = logging.getLogger("BACKTEST")


class BacktestResult:
    def __init__(self):
        self.trades: List[Dict] = []
        self.equity_curve: List[float] = []
        self.period_start: str = ""
        self.period_end: str = ""

    def compute_stats(self, initial_capital: float) -> Dict:
        if not self.trades:
            return {"error": "No trades"}
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in self.trades)
        returns = pd.Series(self.equity_curve)
        max_dd = self._max_drawdown(returns)
        sharpe = self._sharpe(returns)

        strategy_wins = {}
        for t in self.trades:
            for s in (t.get("strategies_voted") or []):
                if s not in strategy_wins:
                    strategy_wins[s] = {"wins": 0, "total": 0}
                strategy_wins[s]["total"] += 1
                if t["pnl"] > 0:
                    strategy_wins[s]["wins"] += 1

        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_pnl / initial_capital * 100, 2),
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
            "best_trade": round(max((t["pnl"] for t in self.trades), default=0), 2),
            "worst_trade": round(min((t["pnl"] for t in self.trades), default=0), 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe_ratio": round(sharpe, 2),
            "expectancy": round(total_pnl / len(self.trades), 2),
            "strategy_breakdown": {k: round(v["wins"]/v["total"]*100, 1) for k, v in strategy_wins.items()},
            "equity_curve": self.equity_curve,
        }

    def _max_drawdown(self, equity: pd.Series) -> float:
        if equity.empty:
            return 0
        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / rolling_max.replace(0, np.nan)
        return float(abs(drawdown.min())) if not drawdown.isna().all() else 0

    def _sharpe(self, equity: pd.Series, risk_free: float = 0.065) -> float:
        if len(equity) < 2:
            return 0
        daily_returns = equity.pct_change().dropna()
        if daily_returns.std() == 0:
            return 0
        return float((daily_returns.mean() * 252 - risk_free) / (daily_returns.std() * np.sqrt(252)))


class Backtester:
    def __init__(self, initial_capital: float = 10000):
        self.initial_capital = initial_capital
        self.regime_clf = MarketRegimeClassifier()

    def run(self, df: pd.DataFrame, lot_size: int = 50,
        sl_pct: float = 0.28, t1_pct: float = 0.50, t2_pct: float = 1.00,
        min_confidence: float = 0.38, vix_series: pd.Series = None) -> BacktestResult:

        result = BacktestResult()
        capital = self.initial_capital
        result.equity_curve.append(capital)

        # Walk forward in 15-min windows
        window = 30
        for i in range(window, len(df) - 5):
            slice_df = df.iloc[i - window:i]
            future_df = df.iloc[i:i + 8]  # Next 2 hours
            
            try:
                vix_at_i = float(vix_series.iloc[i]) if vix_series is not None else 15.0
                regime, conf, features = self.regime_clf.classify(slice_df, vix_at_i)
                signal, score = self._simple_signal(slice_df, features)

                if signal == 0 or score < min_confidence:
                    continue

                # Simulate entry
                entry_price = future_df["close"].iloc[0]
                if entry_price <= 0:
                    continue

                sl = entry_price * (1 - sl_pct)
                t1 = entry_price * (1 + t1_pct)
                t2 = entry_price * (1 + t2_pct)

                # Walk through future candles
                pnl = 0
                exit_reason = "TIMEOUT"
                exit_price = future_df["close"].iloc[-1]
                strategies = self._get_voted_strategies(features)

                for _, candle in future_df.iterrows():
                    if candle["low"] <= sl:
                        exit_price = sl
                        pnl = (sl - entry_price) * lot_size
                        exit_reason = "SL_HIT"
                        break
                    if candle["high"] >= t2:
                        exit_price = t2
                        pnl = (t2 - entry_price) * lot_size
                        exit_reason = "T2_HIT"
                        break
                    if candle["high"] >= t1:
                        exit_price = t1
                        pnl = (t1 - entry_price) * lot_size
                        exit_reason = "T1_HIT"
                        break

                if exit_reason == "TIMEOUT":
                    pnl = (exit_price - entry_price) * lot_size

                capital += pnl
                result.equity_curve.append(capital)
                result.trades.append({
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 2),
                    "signal": "CE" if signal == 1 else "PE",
                    "regime": regime,
                    "confidence": score,
                    "exit_reason": exit_reason,
                    "strategies_voted": strategies,
                    "timestamp": str(slice_df.index[-1]),
                })

            except Exception as e:
                logger.debug(f"[BACKTEST] window {i} skipped: {e}")
                continue

        return result

    def run_real(
        self,
        broker: AngelOneBroker,
        strategy_engine: StrategyEngine,
        instrument_key: str,
        days: int = 90,
        lot_size: int = 50,
        sl_pct: float = 0.28,
        t1_pct: float = 0.50,
        t2_pct: float = 1.00,
        min_confidence: float = 0.38,
    ) -> BacktestResult:
        """Run a historical backtest using real broker OHLCV and VIX data."""
        result = BacktestResult()
        capital = self.initial_capital
        result.equity_curve.append(capital)

        logger.info(f"[BACKTEST_REAL] fetching OHLCV for {instrument_key} ({days} days)")
        df_15m = broker.get_ohlcv(instrument_key, "15minute", days=days, use_db_fallback=True)
        if df_15m is None or len(df_15m) < 50:
            logger.error(f"[BACKTEST_REAL] Not enough 15m history ({0 if df_15m is None else len(df_15m)})")
            return result

        result.period_start = str(df_15m.index.min())
        result.period_end = str(df_15m.index.max())

        df_5m = broker.get_ohlcv(instrument_key, "5minute", days=days, use_db_fallback=True)
        if df_5m is None:
            df_5m = pd.DataFrame()
        df_1h = broker.get_ohlcv(instrument_key, "1hour", days=days, use_db_fallback=True)
        if df_1h is None:
            df_1h = pd.DataFrame()
        df_1d = broker.get_ohlcv(instrument_key, "1day", days=days, use_db_fallback=True)
        if df_1d is None:
            df_1d = pd.DataFrame()
        vix_df = broker.get_ohlcv("NSE_INDEX|India VIX", "15minute", days=days, use_db_fallback=True)
        if vix_df is None:
            vix_df = pd.DataFrame()

        if vix_df.empty:
            logger.warning("[BACKTEST_REAL] No VIX history available — VIX-dependent strategies will be skipped")

        vix_series = vix_df["close"].reindex(df_15m.index, method="ffill") if not vix_df.empty else pd.Series([np.nan] * len(df_15m), index=df_15m.index)

        window = 30
        for i in range(window, len(df_15m) - 5):
            slice_df = df_15m.iloc[i - window:i]
            ts = slice_df.index[-1]

            if slice_df.empty:
                continue

            vix_at_i = None
            if i < len(vix_series) and not np.isnan(vix_series.iloc[i]):
                vix_at_i = float(vix_series.iloc[i])
            else:
                recent = vix_series.loc[:ts].dropna()
                if not recent.empty:
                    vix_at_i = float(recent.iloc[-1])

            if vix_at_i is None:
                logger.warning(f"[BACKTEST_REAL] skipping {ts} due to missing VIX")
                continue

            current_5m = df_5m.loc[:ts] if not df_5m.empty else pd.DataFrame()
            current_1h = df_1h.loc[:ts] if not df_1h.empty else pd.DataFrame()
            current_1d = df_1d.loc[:ts] if not df_1d.empty else pd.DataFrame()

            regime, regime_conf, _ = self.regime_clf.classify(slice_df, vix_at_i)
            if regime == NO_DATA_REGIME:
                continue

            context = {
                "df_1d": current_1d,
                "df_1h": current_1h,
                "df_15m": slice_df,
                "df_5m": current_5m,
                "vix": vix_at_i,
                "vix_prev": vix_series.loc[:ts].iloc[-2] if len(vix_series.loc[:ts].dropna()) >= 2 else vix_at_i,
            }

            signal, score, details = strategy_engine.evaluate(slice_df, context, regime, regime_conf)
            if signal == NO_TRADE or score < min_confidence:
                continue

            future_df = df_15m.iloc[i:i + 8]
            if future_df.empty:
                continue

            entry_price = future_df["close"].iloc[0]
            if entry_price <= 0:
                continue

            # BUG FIX: direction was never applied. SL/T1/T2 were computed as if
            # every trade was long/CE, and the exit PnL for SL_HIT used a no-op
            # ternary `(1 if signal == BUY_PE else 1)` — literally always 1. Every
            # BUY_PE trade was scored as if it were BUY_CE: a real PE win (index
            # falls) was reported as a loss, and vice versa. Now SL sits on the
            # correct side of entry for the direction, and PnL is direction-aware.
            direction = 1 if signal == BUY_CE else -1

            if direction == 1:
                sl = entry_price * (1 - sl_pct)
                t1 = entry_price * (1 + t1_pct)
                t2 = entry_price * (1 + t2_pct)
            else:
                sl = entry_price * (1 + sl_pct)
                t1 = entry_price * (1 - t1_pct)
                t2 = entry_price * (1 - t2_pct)

            exit_reason = "TIMEOUT"
            exit_price = future_df["close"].iloc[-1]

            for _, candle in future_df.iterrows():
                if direction == 1:
                    if candle["low"] <= sl:
                        exit_price, exit_reason = sl, "SL_HIT"
                        break
                    if candle["high"] >= t2:
                        exit_price, exit_reason = t2, "T2_HIT"
                        break
                    if candle["high"] >= t1:
                        exit_price, exit_reason = t1, "T1_HIT"
                        break
                else:
                    if candle["high"] >= sl:
                        exit_price, exit_reason = sl, "SL_HIT"
                        break
                    if candle["low"] <= t2:
                        exit_price, exit_reason = t2, "T2_HIT"
                        break
                    if candle["low"] <= t1:
                        exit_price, exit_reason = t1, "T1_HIT"
                        break

            pnl = (exit_price - entry_price) * lot_size * direction
            # VERIFICATION LOG — grep "[BACKTEST_REAL] TRADE" to eyeball that PE
            # trades have sl above entry / targets below, and pnl sign matches
            # whether the index actually moved in the trade's favor.
            signal_type = "CE" if signal == BUY_CE else "PE"
            logger.info(
                f"[BACKTEST_REAL] TRADE {signal_type} @ {ts} entry={entry_price:.2f} "
                f"direction={direction:+d} sl={sl:.2f} t1={t1:.2f} t2={t2:.2f} "
                f"exit={exit_price:.2f}({exit_reason}) pnl={pnl:+.2f}"
            )
            
            capital += pnl
            result.equity_curve.append(capital)
            result.trades.append({
                "entry_time": str(ts),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "pnl_pct": round((exit_price - entry_price) / entry_price * 100 * direction, 2),
                "signal": "CE" if signal == BUY_CE else "PE",
                "regime": regime,
                "confidence": score,
                "exit_reason": exit_reason,
                "strategies_voted": [k for k, v in details.items() if v["signal"] == signal],
            })

        return result

    def _simple_signal(self, df: pd.DataFrame, features: Dict) -> Tuple[int, float]:
        rsi_val = features.get("rsi", 50)
        adx_val = features.get("adx", 20)
        ema_cross = features.get("ema_cross", 0)
        struct = features.get("structure", "SIDEWAYS")

        if ema_cross == 1 and rsi_val > 52 and adx_val > 22 and struct == "BULLISH":
            score = min(0.45 + adx_val / 200, 0.85)
            return 1, score
        if ema_cross == -1 and rsi_val < 48 and adx_val > 22 and struct == "BEARISH":
            score = min(0.45 + adx_val / 200, 0.85)
            return -1, score
        return 0, 0.0

    def _get_voted_strategies(self, features: Dict) -> List[str]:
        voted = []
        if features.get("adx", 0) > 25:
            voted.append("smc")
        if features.get("rsi", 50) != 50:
            voted.append("orb")
        if features.get("ema_cross", 0) != 0:
            voted.append("greeks")
        return voted
