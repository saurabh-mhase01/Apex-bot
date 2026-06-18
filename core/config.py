"""Config — loads from config.yaml or environment variables"""
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class Config:
    # Upstox (legacy)
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_access_token: str = ""
    upstox_redirect_uri: str = "http://localhost:8000/callback"

    # Groww API (recommended)
    groww_api_key: str = ""
    groww_api_secret: str = ""
    groww_totp_token: str = ""           # TOTP flow (recommended — no expiry)
    groww_totp_secret: str = ""          # TOTP secret for auto-generation

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Capital
    total_capital: float = 10000.0
    max_risk_per_trade_pct: float = 0.15
    max_daily_loss_pct: float = 0.25
    max_open_trades: int = 2
    min_reward_risk: float = 1.5

    # Instruments
    instruments: list = field(default_factory=lambda: ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"])

    # Strategy weights (dynamic, stored in DB but initialized here)
    strategy_weights: dict = field(default_factory=lambda: {
        "smc": 0.18, "orb": 0.15, "greeks": 0.15, "fib_sr": 0.12,
        "vix": 0.10, "sentiment": 0.08, "oi_flow": 0.10,
        "iv_skew": 0.07, "bb_squeeze": 0.05
    })

    # Bot modes
    paper_trading: bool = True
    auto_trade: bool = False

    # Paths
    db_path: str = "data/bot.db"
    model_path: str = "ml/models/"

    @classmethod
    def load(cls, path="config.yaml") -> "Config":
        cfg = cls()
        if Path(path).exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        # Env vars override yaml
        env_map = {
            "UPSTOX_API_KEY": "upstox_api_key",
            "UPSTOX_API_SECRET": "upstox_api_secret",
            "UPSTOX_ACCESS_TOKEN": "upstox_access_token",
            "GROWW_API_KEY": "groww_api_key",
            "GROWW_API_SECRET": "groww_api_secret",
            "GROWW_TOTP_TOKEN": "groww_totp_token",
            "GROWW_TOTP_SECRET": "groww_totp_secret",
            "TELEGRAM_TOKEN": "telegram_token",
            "TELEGRAM_CHAT_ID": "telegram_chat_id",
        }
        for env, attr in env_map.items():
            if os.getenv(env):
                setattr(cfg, env_map[env], os.getenv(env))
        return cfg

    def save(self, path="config.yaml"):
        import dataclasses
        with open(path, "w") as f:
            yaml.dump(dataclasses.asdict(self), f, default_flow_style=False)
