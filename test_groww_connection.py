"""
Test Groww API Connection
Quick verification that your credentials work
"""

import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("TEST")

def test_connection():
    """Test Groww API connection"""
    logger.info("🧪 Testing Groww API Connection...")
    
    try:
        from core.config import Config
        from data.groww_broker import GrowwBroker
        
        # Load config
        logger.info("📝 Loading config.yaml...")
        config = Config.load("config.yaml")
        
        logger.info(f"  groww_api_key: {config.groww_api_key[:20]}..." if config.groww_api_key else "  groww_api_key: EMPTY")
        logger.info(f"  groww_api_secret: {config.groww_api_secret[:10]}..." if config.groww_api_secret else "  groww_api_secret: EMPTY")
        logger.info(f"  groww_totp_token: {config.groww_totp_token[:20]}..." if config.groww_totp_token else "  groww_totp_token: EMPTY")
        logger.info(f"  paper_trading: {config.paper_trading}")
        
        if not config.groww_api_key and not config.groww_totp_token:
            logger.error("❌ No API credentials found! Update config.yaml first.")
            return False
        
        # Initialize broker
        logger.info("🔗 Connecting to Groww API...")
        broker = GrowwBroker(
            api_key=config.groww_totp_token or config.groww_api_key,
            api_secret=config.groww_api_secret or None,
            totp_secret=config.groww_totp_secret or None,
            paper_trading=config.paper_trading
        )
        
        if not broker.groww:
            logger.error("❌ Groww broker not initialized. Check growwapi installation.")
            return False
        
        logger.info("✅ Groww API authenticated successfully!")
        
        # Test API calls
        logger.info("\n📊 Testing API Calls...")
        
        # Get Nifty LTP
        ltp = broker.get_ltp("NSE_INDEX|Nifty 50")
        if ltp:
            logger.info(f"  ✅ Nifty 50 LTP: ₹{ltp:.2f}")
        else:
            logger.warning("  ⚠️  Could not fetch Nifty 50 LTP")
        
        # Get VIX
        vix = broker.get_india_vix()
        if vix:
            logger.info(f"  ✅ India VIX: {vix:.2f}")
        else:
            logger.warning("  ⚠️  Could not fetch VIX")
        
        # Get available funds (if live trading)
        if not config.paper_trading:
            funds = broker.get_funds()
            if funds:
                logger.info(f"  ✅ Available funds: {funds}")
            else:
                logger.warning("  ⚠️  Could not fetch funds")
        else:
            logger.info("  📝 Paper trading mode - funds tracking via database")
        
        logger.info("\n✅ All tests passed! Ready to run bot.")
        logger.info("\n🚀 Start bot with: python main.py")
        return True
        
    except ImportError as e:
        logger.error(f"❌ Import error: {e}")
        logger.error("   Run: pip install -r requirements.txt")
        return False
    except Exception as e:
        logger.error(f"❌ Connection failed: {e}")
        logger.error("   • Check API credentials in config.yaml")
        logger.error("   • Verify credentials are correctly copied")
        logger.error("   • Check internet connection")
        return False


if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)
