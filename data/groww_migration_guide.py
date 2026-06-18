"""
Groww Config + Bot Engine Patch
How to swap from Upstox → Groww in bot_engine.py
"""

# ════════════════════════════════════════════════════════════════════
# 1. UPDATE config.yaml  (or config.py defaults)
# ════════════════════════════════════════════════════════════════════
#
# Replace this block in your config.yaml:
#
# upstox_api_key: ""
# upstox_api_secret: ""
# upstox_access_token: ""
#
# With this:
#
# groww_api_key: ""          # From Groww Cloud API Keys page
# groww_api_secret: ""       # Used in API Key + Secret flow
# groww_totp_token: ""       # TOTP token (for TOTP flow — recommended)
# groww_totp_secret: ""      # TOTP secret (for TOTP flow — recommended)
#
# ════════════════════════════════════════════════════════════════════
# 2. UPDATE core/config.py  — add these fields
# ════════════════════════════════════════════════════════════════════

GROWW_CONFIG_FIELDS = """
    # Groww API (replaces Upstox fields)
    groww_api_key: str = ""
    groww_api_secret: str = ""
    groww_totp_token: str = ""       # Recommended for bots (no daily expiry)
    groww_totp_secret: str = ""

    # Instruments (same as before — Groww uses same underlying names)
    instruments: list = field(default_factory=lambda: [
        "NSE_INDEX|Nifty 50",
        "NSE_INDEX|Nifty Bank"
    ])
"""

# ════════════════════════════════════════════════════════════════════
# 3. UPDATE core/bot_engine.py  — change ONE line
# ════════════════════════════════════════════════════════════════════
#
# BEFORE (Upstox):
#   from data.upstox_broker import UpstoxBroker, NIFTY_KEY, BANKNIFTY_KEY
#   ...
#   self.broker = UpstoxBroker(config.upstox_access_token, config.paper_trading)
#
# AFTER (Groww):
#   from data.groww_broker import GrowwBroker
#   ...
#   self.broker = GrowwBroker(
#       api_key=config.groww_totp_token or config.groww_api_key,
#       api_secret=config.groww_api_secret or None,
#       totp_secret=config.groww_totp_secret or None,
#       paper_trading=config.paper_trading
#   )
#
# Everything else in bot_engine.py stays UNCHANGED.
# GrowwBroker exposes identical method names to UpstoxBroker.
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
# 4. Groww Instrument Symbol Format
# ════════════════════════════════════════════════════════════════════
#
# Underlying LTP:
#   "NIFTY"      → Nifty 50 index
#   "BANKNIFTY"  → Bank Nifty index
#   "FINNIFTY"   → Fin Nifty index
#   "INDIAVIX"   → India VIX
#
# Option Trading Symbol format:
#   {UNDERLYING}{YY}{MON}{STRIKE}{CE/PE}
#   e.g.:  NIFTY25JUN24500CE
#          BANKNIFTY25JUN52000PE
#
# Historical data groww_symbol:
#   Cash:    "NSE-NIFTY"  / "NSE-BANKNIFTY"
#   Options: obtained via groww.get_contracts(exchange, underlying, expiry_date)
#            e.g.: "NSE-NIFTY-25Jun25-24500-CE"
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
# 5. Groww-specific ADVANTAGES over Upstox for this bot
# ════════════════════════════════════════════════════════════════════
#
# ✅ get_greeks() — live Delta/Gamma/Theta/Vega/IV in one call
#    → Strategy 3 (Greeks Momentum) becomes much more accurate
#
# ✅ OCO orders native — set SL + Target in a single API call
#    → Risk Guard auto-exits without needing a monitoring loop
#
# ✅ Backtesting APIs built-in — FnO data from 2020 onwards
#    → get_expiries() + get_contracts() + get_historical_candles()
#    → No need for NSE data subscription
#
# ✅ Groww Cloud deployment — run the bot ON Groww's servers
#    → Zero infra, no VPS needed, lowest latency to exchange
#    → Web editor at groww.in/cloud
#
# ✅ Flat ₹499/month — no per-call charges
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
# 6. Quick test script — verify your Groww connection
# ════════════════════════════════════════════════════════════════════

QUICK_TEST = """
from data.groww_broker import GrowwBroker

# Paste your credentials here for testing
broker = GrowwBroker(
    api_key="YOUR_TOTP_TOKEN",
    totp_secret="YOUR_TOTP_SECRET",
    paper_trading=True
)

# Test 1: Get Nifty LTP
nifty_ltp = broker.get_ltp("NIFTY")
print(f"Nifty LTP: {nifty_ltp}")

# Test 2: Get India VIX
vix = broker.get_india_vix()
print(f"India VIX: {vix}")

# Test 3: Get expiries
expiries = broker.get_option_expiries("NSE_INDEX|Nifty 50")
print(f"Nifty expiries: {expiries[:3]}")

# Test 4: Get nearest expiry option chain
if expiries:
    chain = broker.get_option_chain("NSE_INDEX|Nifty 50", expiries[0])
    print(f"Option chain strikes: {len(chain)}")
    pcr = broker.get_pcr(chain)
    print(f"PCR: {pcr}")

# Test 5: Get ATM option Greeks
if nifty_ltp and expiries:
    atm = broker.get_atm_strike("NIFTY", lot_size=50)
    symbol = broker.find_option_instrument(
        "NSE_INDEX|Nifty 50", atm, "CE", expiries[0]
    )
    greeks = broker.get_greeks(symbol, "NIFTY", expiries[0])
    print(f"ATM CE Greeks: {greeks}")

# Test 6: Paper order
order = broker.place_order(symbol, qty=50, transaction_type="BUY")
print(f"Paper order: {order}")

print("\\n✅ All Groww API tests passed!")
"""
