"""Alerts: Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, log line otherwise."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("tryme.alerts")


class Alerts:
    def __init__(self, token: str | None = None, chat_id: str | None = None, post=requests.post):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.post = post
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        log.warning("ALERT %s", text)
        if not (self.token and self.chat_id):
            return False
        try:
            r = self.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000]},
                timeout=10,
            )
            return bool(r.ok)
        except Exception as e:  # an alert failure must never take the trader down
            log.error("telegram send failed: %s", type(e).__name__)
            return False
