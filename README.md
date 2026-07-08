# 🤖 APEX AI Options Bot — Groww API Edition

## Setup in 5 Steps

### 1. Install dependencies
```
pip install -r requirements.txt
```

### 2. Get Groww API credentials
- Go to: https://groww.in/trade-api/api-keys
- Subscribe: ₹499/month at groww.in/user/profile/trading-apis
- Click "Generate TOTP token" (recommended — no daily expiry)
- Copy TOTP Token and TOTP Secret

### 3. Fill config.yaml
```yaml
groww_totp_token: "your_token_here"
groww_totp_secret: "your_secret_here"
telegram_token: "your_telegram_bot_token"
telegram_chat_id: "your_chat_id"
total_capital: 10000.0
paper_trading: true       ← Keep true until satisfied with results
```

### 4. Test connection
```
python data/groww_migration_guide.py
```

### 5. Run the bot
```
python main.py
```
Dashboard available at: http://localhost:8000

## File Structure
```
optionsbot/
├── main.py                    ← Entry point
├── config.yaml                ← Your credentials + settings
├── core/
│   ├── bot_engine.py          ← Central orchestrator
│   ├── regime_classifier.py   ← Market regime detection
│   ├── risk_guard.py          ← Capital protection
│   └── config.py              ← Config loader
├── data/
│   ├── groww_broker.py        ← Groww API adapter (NEW)
│   ├── indicators.py          ← Technical indicators
│   └── groww_migration_guide.py
├── strategies/
│   └── strategy_engine.py     ← All 11 strategies + voting
├── db/
│   └── database.py            ← SQLite trade history
├── backtest/
│   └── backtester.py          ← Walk-forward backtester
├── alerts/
│   └── telegram_alert.py      ← Telegram notifications
└── api/
    └── dashboard_api.py       ← FastAPI REST backend
```

## New architecture: DB-backed data engine
The bot now uses a persistent ingestion layer so candles and market snapshots are stored in SQLite first and reused on subsequent cycles. This removes the old pattern of re-pulling the same data on every signal check and reduces rate-limit pressure.

What changed:
- Candles are saved into the database as 5m/15m/30m/1h/1d data becomes available.
- Market snapshots such as VIX and option-chain context are also stored for later reuse.
- The strategy engine reads from the persisted store whenever possible instead of forcing fresh broker calls for each evaluation.

How to verify:
```bash
PYTHONPATH=/workspaces/Apex-bot pytest -q tests/test_data_engine.py
```

## Paper Trading First!
Run paper_trading: true for at least 1 month.
Only enable auto_trade: true after consistent profitable results.
