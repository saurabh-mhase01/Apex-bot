"""Config — loads from config.yaml or environment variables"""
import logging
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

logger = logging.getLogger("CONFIG")


@dataclass
class Config:
    # Angel One (current broker) — prefer env vars, see load()
    angleone_api_key: str = ""
    angleone_client_id: str = ""
    angleone_password: str = ""
    angleone_totp_secret: str = ""

    # Upstox (legacy)
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_access_token: str = ""
    upstox_redirect_uri: str = "http://localhost:8000/callback"

    # Groww API
    groww_api_key: str = ""
    groww_api_secret: str = ""
    groww_totp_token: str = ""
    groww_totp_secret: str = ""

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Capital / risk — these are intentional user-set trading parameters,
    # not market data, so defaults here are fine (they're config, not a
    # substitute for a live price/VIX/regime read).
    total_capital: float = 10000.0
    max_risk_per_trade_pct: float = 0.15
    max_daily_loss_pct: float = 0.25
    max_open_trades: int = 2
    min_reward_risk: float = 1.5

    instruments: list = field(default_factory=lambda: ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"])

    strategy_weights: dict = field(default_factory=lambda: {
        "smc": 0.18, "orb": 0.15, "greeks": 0.15, "fib_sr": 0.12,
        "vix": 0.10, "sentiment": 0.08, "oi_flow": 0.10,
        "iv_skew": 0.07, "bb_squeeze": 0.05
    })

    paper_trading: bool = True
    auto_trade: bool = False

    db_path: str = "data/bot.db"
    model_path: str = "ml/models/"

    @classmethod
    def load(cls, path="config.yaml") -> "Config":
        cfg = cls()
        loaded_from_yaml = []
        ignored_yaml_keys = []

        if Path(path).exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
                    loaded_from_yaml.append(k)
                else:
                    # Previously these were silently dropped with no log line —
                    # if your yaml has a key that doesn't match a Config field
                    # (typo, or a field that hasn't been added yet), you now see it.
                    ignored_yaml_keys.append(k)

        env_map = {
            "ANGELONE_API_KEY": "angleone_api_key",
            "ANGELONE_CLIENT_ID": "angleone_client_id",
            "ANGELONE_PASSWORD": "angleone_password",
            "ANGELONE_TOTP_SECRET": "angleone_totp_secret",
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
        env_overrides = []
        for env, attr in env_map.items():
            val = os.getenv(env)
            if val:
                setattr(cfg, attr, val)
                env_overrides.append(env)

        logger.info(f"[CONFIG_LOAD] INPUT: path={path}")
        logger.info(f"[CONFIG_LOAD] loaded_from_yaml={loaded_from_yaml}")
        if ignored_yaml_keys:
            logger.warning(f"[CONFIG_LOAD] ignored_yaml_keys (no matching Config field!): {ignored_yaml_keys}")
        logger.info(f"[CONFIG_LOAD] env_overrides applied: {env_overrides}")
        logger.info(
            f"[CONFIG_LOAD] OUTPUT: paper_trading={cfg.paper_trading}, auto_trade={cfg.auto_trade}, "
            f"total_capital={cfg.total_capital}, instruments={cfg.instruments}, "
            f"angleone_client_id_set={bool(cfg.angleone_client_id)}"
        )

        if not cfg.angleone_api_key or not cfg.angleone_client_id or \
           not cfg.angleone_password or not cfg.angleone_totp_secret:
            logger.error(
                "[CONFIG_LOAD] One or more Angel One credentials are missing. "
                "The broker will fail to authenticate — set ANGELONE_API_KEY / "
                "ANGELONE_CLIENT_ID / ANGELONE_PASSWORD / ANGELONE_TOTP_SECRET "
                "as environment variables (preferred) or in config.yaml."
            )

        return cfg

    def save(self, path="config.yaml"):
        import dataclasses
        with open(path, "w") as f:
            yaml.dump(dataclasses.asdict(self), f, default_flow_style=False)
        logger.info(f"[CONFIG_SAVE] OUTPUT: written to {path}")