"""Avisos al mòbil per Telegram.

Cal definir TELEGRAM_BOT_TOKEN (del bot creat amb @BotFather) i TELEGRAM_CHAT_ID
(un o més ids separats per comes). Si no hi són, no s'envia res.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)
SEV_ICON = {"alarm": "🔴", "warning": "🟠", "info": "🔵"}


def enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text: str, client: httpx.Client | None = None) -> int:
    """Envia el text a tots els xats configurats. Retorna quants han anat bé."""
    if not enabled():
        return 0
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chats = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    client = client or httpx.Client(timeout=15)
    ok = 0
    for chat in chats:
        try:
            r = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": text})
            r.raise_for_status()
            ok += 1
        except Exception as e:  # un avís que falla no ha d'aturar la lectura
            log.warning("Telegram: no s'ha pogut enviar a %s: %s", chat, e)
    return ok


def alert_text(plant_name: str, severity: str, title: str) -> str:
    return f"{SEV_ICON.get(severity, '⚠️')} {plant_name}\n{title}"
