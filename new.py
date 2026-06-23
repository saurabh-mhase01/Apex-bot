"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  STEP 1 — Run this FIRST to see what symbol format Angel One actually uses  ║
║  python diagnose_chain.py                                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── import broker (adjust path if yours differs) ──────────────────────────────
from data.Angle_broker_v2 import AngelOneBroker, _INSTRUMENT_CACHE, KEY_TO_UNDERLYING

print("=" * 65)
print("  OPTION CHAIN SYMBOL FORMAT DIAGNOSTIC")
print("=" * 65)

# ── Check cache is loaded ─────────────────────────────────────────────────────
total = len(_INSTRUMENT_CACHE)
print(f"\n✅ Cache size: {total:,} instruments")
if total == 0:
    print("❌ Cache is empty — broker not initialised. Check credentials.")
    sys.exit(1)

# ── Find ALL NFO OPTIDX records and sample their symbols ─────────────────────
nfo_opts = [
    inst for inst in _INSTRUMENT_CACHE.values()
    if inst.get("exch_seg") == "NFO"
    and inst.get("instrumenttype") in ("OPTIDX", "OPTSTK")
    and "NIFTY" in inst.get("symbol", "")
    and "BANK" not in inst.get("symbol", "")
]

print(f"\n📋 NIFTY OPTIDX contracts in cache: {len(nfo_opts)}")
print("\nFirst 15 symbol samples (THIS SHOWS THE EXACT FORMAT):")
seen = set()
for inst in sorted(nfo_opts, key=lambda x: x.get("expiry", ""))[:30]:
    sym = inst.get("symbol", "")
    if sym in seen:
        continue
    seen.add(sym)
    print(f"  symbol=[{sym}]  expiry=[{inst.get('expiry')}]  "
          f"strike={inst.get('strike')}  token={inst.get('token')}")
    if len(seen) >= 15:
        break

# ── Extract unique expiry strings ─────────────────────────────────────────────
expiries_in_cache = sorted(set(
    inst.get("expiry", "") for inst in nfo_opts if inst.get("expiry")
))
print(f"\n📅 Expiry strings found in cache ({len(expiries_in_cache)} unique):")
for e in expiries_in_cache[:10]:
    print(f"  [{e}]")

# ── Test the format conversion function ──────────────────────────────────────
print("\n🔬 Testing _format_expiry_for_cache('2026-06-23'):")
from datetime import datetime

def format_v1(expiry_str):
    """Current version — returns 23JUN2026"""
    dt = datetime.strptime(expiry_str, "%Y-%m-%d")
    return dt.strftime("%d%b%Y").upper()

def format_v2(expiry_str):
    """Alternative — returns 23JUN26 (2-digit year)"""
    dt = datetime.strptime(expiry_str, "%Y-%m-%d")
    return dt.strftime("%d%b%y").upper()

test = "2026-06-23"
v1 = format_v1(test)
v2 = format_v2(test)
print(f"  format_v1 → [{v1}]  (4-digit year)")
print(f"  format_v2 → [{v2}]  (2-digit year)")

# Check which one is present in the actual cache symbols
v1_hits = sum(1 for i in nfo_opts if v1 in i.get("symbol", "").upper())
v2_hits = sum(1 for i in nfo_opts if v2 in i.get("symbol", "").upper())
print(f"\n  Symbols containing [{v1}]: {v1_hits}")
print(f"  Symbols containing [{v2}]: {v2_hits}")

if v2_hits > v1_hits:
    print(f"\n  ✅ CONFIRMED: Angel One uses 2-digit year in option symbols [{v2}]")
    print(f"  ❌ Current code uses 4-digit year [{v1}] — this is why chain returns 0 contracts")
    print(f"\n  FIX: Change _format_expiry_for_cache to use strftime('%d%b%y').upper()")
elif v1_hits > 0:
    print(f"\n  ✅ 4-digit year format works — chain match should work")
    print(f"  Something else is blocking the match. Check 'underlying' filter below.")
else:
    print(f"\n  ❓ Neither format matched. Checking underlying name filter...")
    # Try searching for just the month pattern
    for sym_fragment in ["JUN26", "JUN2026", "JUNE", "23JUN"]:
        hits = sum(1 for i in nfo_opts if sym_fragment in i.get("symbol","").upper())
        print(f"  Fragment [{sym_fragment}] matches: {hits} symbols")

# ── Check underlying name in symbols ─────────────────────────────────────────
print("\n🔬 Testing KEY_TO_UNDERLYING mapping:")
for key, underlying in KEY_TO_UNDERLYING.items():
    sample = next((i.get("symbol","") for i in nfo_opts
                   if underlying in i.get("symbol","")[:len(underlying)+2]), None)
    hits = sum(1 for i in nfo_opts if underlying in i.get("symbol",""))
    print(f"  [{key}] → [{underlying}]  symbols containing it: {hits}  sample: {sample}")

print("\n" + "=" * 65)
print("  Copy the output above and share — it will show exact fix needed")
print("=" * 65)