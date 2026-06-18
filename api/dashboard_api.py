"""
Dashboard API — FastAPI backend for the control panel
All endpoints for the React frontend
"""
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

logger = logging.getLogger("API")

# Global bot engine reference (set by main.py)
_engine = None
_db = None

app = FastAPI(title="AI Options Bot Dashboard", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request Models ─────────────────────────────────────────────────────────────
class SettingsUpdate(BaseModel):
    total_capital: Optional[float] = None
    max_risk_per_trade_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_open_trades: Optional[int] = None
    paper_trading: Optional[bool] = None
    auto_trade: Optional[bool] = None

class WeightUpdate(BaseModel):
    weights: Dict[str, float]

class ManualTradeRequest(BaseModel):
    instrument: str
    strike: int
    option_type: str
    expiry: str
    qty: int
    order_type: str = "MARKET"

# ── Core Status ────────────────────────────────────────────────────────────────
@app.get("/api/status")
def get_status():
    if not _engine:
        return {"error": "Engine not initialized"}
    return _engine.get_live_status()

@app.post("/api/bot/activate")
def activate_bot():
    _engine.set_active(True)
    return {"success": True, "active": True}

@app.post("/api/bot/pause")
def pause_bot():
    _engine.set_active(False)
    return {"success": True, "active": False}

@app.post("/api/bot/force-exit-all")
def force_exit_all():
    _engine.force_exit_all()
    return {"success": True}

# ── Trades ────────────────────────────────────────────────────────────────────
@app.get("/api/trades")
def get_trades(limit: int = 50, offset: int = 0, status: Optional[str] = None,
               instrument: Optional[str] = None):
    trades = _db.get_trades(limit=limit, offset=offset, status=status, instrument=instrument)
    return {"trades": trades, "total": len(trades)}

@app.get("/api/trades/open")
def get_open_trades():
    return {"trades": _db.get_open_trades()}

@app.post("/api/trades/{trade_id}/exit")
def exit_trade(trade_id: str):
    result = _engine.manual_exit_trade(trade_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result

@app.post("/api/trades/manual")
def place_manual_trade(req: ManualTradeRequest):
    """Place a manual trade bypassing strategy engine"""
    # Minimal risk check still applies
    ltp = _engine.broker.get_ltp(f"NSE_FO|{req.instrument}")
    if not ltp:
        raise HTTPException(400, "Could not get price")
    order = _engine.broker.place_order(
        f"NSE_FO|{req.instrument}", req.qty,
        order_type=req.order_type, transaction_type="BUY"
    )
    return {"success": True, "order": order}

# ── Analytics ─────────────────────────────────────────────────────────────────
@app.get("/api/analytics/summary")
def get_summary():
    return _db.get_summary_stats()

@app.get("/api/analytics/daily-pnl")
def get_daily_pnl(days: int = 30):
    return {"data": _db.get_daily_pnl(days=days)}

@app.get("/api/analytics/strategy-performance")
def get_strategy_performance():
    return {"strategies": _db.get_strategy_performance()}

@app.get("/api/analytics/regime-history")
def get_regime_history(limit: int = 100):
    return {"history": _db.get_regime_history(limit)}

# ── Signals ───────────────────────────────────────────────────────────────────
@app.get("/api/signals")
def get_signals(limit: int = 50):
    return {"signals": _db.get_signals(limit=limit)}

# ── Settings ──────────────────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    import dataclasses
    return dataclasses.asdict(_engine.config)

@app.put("/api/settings")
def update_settings(req: SettingsUpdate):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    _engine.update_settings(updates)
    return {"success": True, "updated": updates}

@app.put("/api/settings/weights")
def update_weights(req: WeightUpdate):
    _engine.strategy_engine.update_weights(req.weights)
    _db.set_setting("strategy_weights", req.weights)
    return {"success": True, "weights": _engine.strategy_engine.weights}

# ── Market Data ───────────────────────────────────────────────────────────────
@app.get("/api/market/ltp/{instrument}")
def get_ltp(instrument: str):
    key_map = {
        "nifty": "NSE_INDEX|Nifty 50",
        "banknifty": "NSE_INDEX|Nifty Bank",
        "vix": "NSE_INDEX|India VIX"
    }
    key = key_map.get(instrument.lower(), instrument)
    ltp = _engine.broker.get_ltp(key)
    return {"instrument": instrument, "ltp": ltp}

@app.get("/api/market/regime")
def get_regime():
    return {
        "regime": _engine.active_regime,
        "confidence": _engine.regime_confidence,
    }

@app.get("/api/market/funds")
def get_funds():
    return _engine.broker.get_funds()

@app.get("/api/market/positions")
def get_positions():
    return {"positions": _engine.broker.get_positions()}

# ── Backtest ──────────────────────────────────────────────────────────────────
@app.get("/api/backtest/results")
def get_backtest_results():
    return {"results": _db.get_backtests()}

@app.post("/api/backtest/run")
def run_backtest():
    """Trigger backtest (async in background)"""
    import threading
    def _run():
        _engine.weekly_backtest_and_retrain()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"success": True, "message": "Backtest started in background"}

# ── WebSocket for live updates ────────────────────────────────────────────────
connected_clients: List[WebSocket] = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            import asyncio
            await asyncio.sleep(5)
            if _engine:
                data = {
                    "type": "status",
                    "data": _engine.get_live_status(),
                    "timestamp": str(datetime.now()),
                }
                await websocket.send_text(json.dumps(data))
    except WebSocketDisconnect:
        connected_clients.remove(websocket)

# ── Serve React frontend ───────────────────────────────────────────────────────
frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")


def start_api_server(engine=None, db=None, host="0.0.0.0", port=8000):
    global _engine, _db
    if engine:
        _engine = engine
    if db:
        _db = db
    uvicorn.run(app, host=host, port=port, log_level="warning")
