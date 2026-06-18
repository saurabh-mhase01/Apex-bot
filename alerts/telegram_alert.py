"""Telegram Alerter — sends trade notifications"""
import logging
import requests

logger = logging.getLogger("TELEGRAM")


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            logger.info(f"[TELEGRAM] (No token configured) {message[:80]}...")
            return False
        try:
            logger.info(f"[TELEGRAM] Sending alert: {message[:60]}...")
            r = requests.post(f"{self.base}/sendMessage", json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=5)
            if r.status_code == 200:
                logger.info(f"[TELEGRAM] ✅ Alert sent successfully")
                return True
            else:
                logger.info(f"[TELEGRAM] ⚠️ Alert send returned status {r.status_code}")
                return False
        except Exception as e:
            logger.info(f"[TELEGRAM] ❌ Error: {e}")
            return False
