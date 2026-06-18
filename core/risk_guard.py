"""
Risk Guard — Non-negotiable capital protection
Kelly Criterion sizing, SL/Target calculation, daily loss limits
"""
import logging
from datetime import datetime, date
from typing import Dict, Tuple, Optional
from db.database import Database

logger = logging.getLogger("RISK")


class RiskGuard:
    def __init__(self, config, db: Database):
        self.config = config
        self.db = db
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.open_trades = 0
        self._reset_date = date.today()
        self._load_daily_state()

    def _load_daily_state(self):
        today = str(date.today())
        pnl_row = next((p for p in self.db.get_daily_pnl(1) if p["date"] == today), None)
        if pnl_row:
            self.daily_loss = min(0, pnl_row["net_pnl"])
            self.daily_trades = pnl_row["trades_taken"]
        self.open_trades = len(self.db.get_open_trades())

    def _refresh(self):
        if date.today() != self._reset_date:
            self.daily_loss = 0.0
            self.daily_trades = 0
            self._reset_date = date.today()
        self.open_trades = len(self.db.get_open_trades())

    def validate(self, capital: float, premium: float,
                 qty: int, confidence: float) -> Tuple[bool, str]:
        logger.info(f"[VALIDATE] Starting risk validation: Capital={capital:.0f}, Premium={premium:.2f}, Qty={qty}, Confidence={confidence:.0%}")
        self._refresh()
        logger.info(f"[VALIDATE] Daily state: Loss={self.daily_loss:.0f}, Trades={self.daily_trades}, Open={self.open_trades}")

        checks = [
            self._check_daily_loss(capital),
            self._check_open_positions(),
            self._check_trade_count(),
            self._check_capital(capital, premium, qty),
            self._check_market_hours(),
        ]

        for ok, reason in checks:
            if not ok:
                return False, reason

        return True, "✅ Trade approved"

    def _check_daily_loss(self, capital: float) -> Tuple[bool, str]:
        loss_pct = abs(self.daily_loss) / capital if capital else 0
        if loss_pct >= self.config.max_daily_loss_pct:
            return False, f"❌ Daily loss limit hit ({loss_pct:.1%} ≥ {self.config.max_daily_loss_pct:.1%})"
        return True, ""

    def _check_open_positions(self) -> Tuple[bool, str]:
        if self.open_trades >= self.config.max_open_trades:
            return False, f"❌ Max open trades ({self.config.max_open_trades}) reached"
        return True, ""

    def _check_trade_count(self) -> Tuple[bool, str]:
        if self.daily_trades >= 5:
            return False, f"❌ Max daily trades (5) reached"
        return True, ""

    def _check_capital(self, capital: float, premium: float, qty: int) -> Tuple[bool, str]:
        trade_value = premium * qty
        max_allowed = capital * self.config.max_risk_per_trade_pct
        if trade_value > max_allowed:
            return False, f"❌ Trade ₹{trade_value:,.0f} > max allowed ₹{max_allowed:,.0f}"
        return True, ""

    def _check_market_hours(self) -> Tuple[bool, str]:
        now = datetime.now().time()
        from datetime import time as dtime
        if not (dtime(9, 15) <= now <= dtime(15, 25)):
            return False, "❌ Outside market hours"
        if dtime(15, 0) <= now <= dtime(15, 25):
            logger.warning("⚠️ Trading in last 30 mins — elevated risk")
        return True, ""

    def position_size(self, capital: float, confidence: float,
                      lot_size: int = 50) -> int:
        """
        Kelly Criterion-inspired sizing
        Returns number of lots (minimum 1)
        """
        base_risk = capital * self.config.max_risk_per_trade_pct
        # Scale with confidence: 50% conf → 70% of base, 90% conf → 130% of base
        scaling = 0.4 + confidence
        adjusted = base_risk * scaling
        adjusted = min(adjusted, capital * 0.20)  # Hard cap 20%
        return max(1, int(adjusted / (lot_size * 100)))  # Rough lot cost

    def calculate_sl_target(self, entry_price: float, signal: int,
                             regime: str = "TRENDING_BULL") -> Dict:
        """
        Dynamic SL and Target based on regime
        signal: 1=CE, -1=PE
        """
        regime_params = {
            "TRENDING_BULL":   {"sl": 0.28, "t1": 0.50, "t2": 1.00, "trail": True},
            "TRENDING_BEAR":   {"sl": 0.28, "t1": 0.50, "t2": 1.00, "trail": True},
            "HIGH_VOLATILITY": {"sl": 0.35, "t1": 0.75, "t2": 1.50, "trail": False},
            "RANGE_BOUND":     {"sl": 0.20, "t1": 0.40, "t2": 0.80, "trail": False},
            "PRE_EVENT":       {"sl": 0.20, "t1": 0.40, "t2": 0.70, "trail": False},
        }
        p = regime_params.get(regime, regime_params["TRENDING_BULL"])
        return {
            "sl_price": round(entry_price * (1 - p["sl"]), 1),
            "target1_price": round(entry_price * (1 + p["t1"]), 1),
            "target2_price": round(entry_price * (1 + p["t2"]), 1),
            "sl_pct": p["sl"],
            "t1_pct": p["t1"],
            "t2_pct": p["t2"],
            "trailing": p["trail"],
        }

    def record_trade_result(self, pnl: float):
        self.daily_trades += 1
        if pnl < 0:
            self.daily_loss += pnl
        # Update daily P&L in DB
        today = str(date.today())
        self.db.upsert_daily_pnl({
            "date": today,
            "net_pnl": self.daily_loss,
            "trades_taken": self.daily_trades,
        })

    def get_status(self, capital: float) -> Dict:
        self._refresh()
        loss_pct = abs(self.daily_loss) / capital if capital else 0
        return {
            "daily_loss": self.daily_loss,
            "daily_loss_pct": round(loss_pct * 100, 2),
            "daily_trades": self.daily_trades,
            "open_trades": self.open_trades,
            "capital_at_risk": round(loss_pct * 100, 2),
            "can_trade": loss_pct < self.config.max_daily_loss_pct and self.open_trades < self.config.max_open_trades,
            "limit_remaining": self.config.max_daily_loss_pct - loss_pct,
        }
