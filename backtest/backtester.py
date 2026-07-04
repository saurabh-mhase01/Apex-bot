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
from core.regime_classifier import MarketRegimeClassifier

logger = logging.getLogger("BACKTEST")


class BacktestResult:
    def __init__(self):
        self.trades: List[Dict] = []
        self.equity_curve: List[float] = []

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
