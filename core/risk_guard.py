"""
Risk Guard — Non-negotiable capital protection
Kelly Criterion sizing, SL/Target calculation, daily loss limits
"""
import logging
from datetime import datetime, date
from typing import Dict, Tuple, Optional
from db.database import Database

logger = logging.getLogger("RISK")

# Regime SL/Target table lives here so validate()/calculate_sl_target() have one
# source of truth. If a regime isn't in here, we refuse to guess — see
# calculate_sl_target() below.
REGIME_PARAMS = {
    "TRENDING_BULL":   {"sl": 0.28, "t1": 0.50, "t2": 1.00, "trail": True},
    "TRENDING_BEAR":   {"sl": 0.28, "t1": 0.50, "t2": 1.00, "trail": True},
    "HIGH_VOLATILITY": {"sl": 0.35, "t1": 0.75, "t2": 1.50, "trail": False},
    "RANGE_BOUND":     {"sl": 0.20, "t1": 0.40, "t2": 0.80, "trail": False},
    "PRE_EVENT":       {"sl": 0.20, "t1": 0.40, "t2": 0.70, "trail": False},
}


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
        logger.info(
            f"[RISK_LOAD_STATE] OUTPUT: daily_loss={self.daily_loss}, "
            f"daily_trades={self.daily_trades}, open_trades={self.open_trades}"
        )

    def _refresh(self):
        if date.today() != self._reset_date:
            logger.info(f"[RISK_REFRESH] New day detected ({self._reset_date} -> {date.today()}), resetting daily counters")
            self.daily_loss = 0.0
            self.daily_trades = 0
            self._reset_date = date.today()
        self.open_trades = len(self.db.get_open_trades())

    def validate(self, capital: float, premium: float,
                 qty: int, confidence: float) -> Tuple[bool, str]:
        logger.info(
            f"[RISK_VALIDATE] INPUT: capital={capital}, premium={premium}, "
            f"qty={qty}, confidence={confidence}"
        )
        self._refresh()
        logger.info(
            f"[RISK_VALIDATE] STATE: daily_loss={self.daily_loss}, "
            f"daily_trades={self.daily_trades}, open_trades={self.open_trades}"
        )

        checks = [
            self._check_daily_loss(capital),
            self._check_open_positions(),
            self._check_trade_count(),
            self._check_capital(capital, premium, qty),
            self._check_market_hours(),
        ]

        for ok, reason in checks:
            logger.info(f"[RISK_VALIDATE] CHECK: ok={ok} reason='{reason}'")
            if not ok:
                logger.warning(f"[RISK_VALIDATE] OUTPUT: REJECTED — {reason}")
                return False, reason

        logger.info("[RISK_VALIDATE] OUTPUT: APPROVED")
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

    def position_size(self, capital: float, confidence: float, lot_size: int, premium: float) -> int:
        logger.info(f"[RISK_POSITION_SIZE] INPUT: capital={capital}, confidence={confidence}, "
                    f"lot_size={lot_size}, premium={premium}")
        if not lot_size or lot_size <= 0:
            raise ValueError(f"position_size() requires a real lot_size, got {lot_size}")
        if not premium or premium <= 0:
            raise ValueError(f"position_size() requires a real premium, got {premium}")

        base_risk = capital * self.config.max_risk_per_trade_pct
        scaling   = 0.4 + confidence
        adjusted  = min(base_risk * scaling, capital * 0.20)
        # BUG FIX: previously divided by (lot_size * 100), a flat guess for premium.
        # Use the actual fetched premium so sizing reflects real capital at risk.
        lots = max(1, int(adjusted / (lot_size * premium)))

        logger.info(f"[RISK_POSITION_SIZE] OUTPUT: base_risk={base_risk:.2f}, scaling={scaling:.2f}, "
                    f"adjusted={adjusted:.2f}, lots={lots}")
        return lots

    def calculate_sl_target(self, entry_price: float, signal: int, regime: str) -> Dict:
        """
        Dynamic SL and Target based on regime.
        signal: 1=CE, -1=PE

        regime is REQUIRED — no default. Previously an unrecognized/missing
        regime silently fell back to "TRENDING_BULL" parameters, which could
        apply the wrong SL/target width to a trade. Now an unrecognized regime
        is a hard error — the caller must not place a trade without a valid
        regime read.
        """
        logger.info(f"[RISK_SL_TARGET] INPUT: entry_price={entry_price}, signal={signal}, regime={regime}")

        if regime not in REGIME_PARAMS:
            logger.error(f"[RISK_SL_TARGET] Unknown regime '{regime}' — refusing to fabricate SL/target")
            raise ValueError(f"calculate_sl_target() got unrecognized regime '{regime}'")

        p = REGIME_PARAMS[regime]
        result = {
            "sl_price": round(entry_price * (1 - p["sl"]), 1),
            "target1_price": round(entry_price * (1 + p["t1"]), 1),
            "target2_price": round(entry_price * (1 + p["t2"]), 1),
            "sl_pct": p["sl"],
            "t1_pct": p["t1"],
            "t2_pct": p["t2"],
            "trailing": p["trail"],
        }
        logger.info(f"[RISK_SL_TARGET] OUTPUT: {result}")
        return result

    def record_trade_result(self, pnl: float):
        logger.info(f"[RISK_RECORD_RESULT] INPUT: pnl={pnl}")
        self.daily_trades += 1
        if pnl < 0:
            self.daily_loss += pnl
        today = str(date.today())
        self.db.upsert_daily_pnl({
            "date": today,
            "net_pnl": self.daily_loss,
            "trades_taken": self.daily_trades,
        })
        logger.info(
            f"[RISK_RECORD_RESULT] OUTPUT: daily_loss={self.daily_loss}, daily_trades={self.daily_trades}"
        )

    def get_status(self, capital: float) -> Dict:
        logger.debug(f"[RISK_STATUS] INPUT: capital={capital}")
        self._refresh()
        loss_pct = abs(self.daily_loss) / capital if capital else 0
        status = {
            "daily_loss": self.daily_loss,
            "daily_loss_pct": round(loss_pct * 100, 2),
            "daily_trades": self.daily_trades,
            "open_trades": self.open_trades,
            "capital_at_risk": round(loss_pct * 100, 2),
            "can_trade": loss_pct < self.config.max_daily_loss_pct and self.open_trades < self.config.max_open_trades,
            "limit_remaining": self.config.max_daily_loss_pct - loss_pct,
        }
        logger.debug(f"[RISK_STATUS] OUTPUT: {status}")
        return status