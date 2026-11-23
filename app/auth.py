"""Comptes d'usuari i sessió per galeta signada.

- Contrasenyes amb PBKDF2-SHA256 (biblioteca estàndard).
- La galeta porta id d'usuari, caducitat i una empremta del hash de la
  contrasenya: si es canvia la contrasenya o s'esborra el compte, la sessió cau.
- El primer cop que arrenca amb DASHBOARD_PASSWORD definit, es crea el compte
  "admin" amb aquesta contrasenya. Si no hi ha cap compte, no es demana login
  (ús local).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from .config import get_settings

COOKIE = "fv_session"
MAX_AGE = 30 * 24 * 3600   # 30 dies
ITER = 200_000


def hash_password(pwd: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), ITER).hex()
    return f"pbkdf2${ITER}${salt}${h}"


def check_password(pwd: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(calc, h)
    except Exception:  # noqa: BLE001
        return False


def _secret() -> bytes:
    s = get_settings()
    base = os.getenv("SESSION_SECRET") or f"{s.dashboard_password}|{s.database_url}|fv"
    return hashlib.sha256(base.encode()).digest()


def _sig(msg: str) -> str:
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(user) -> str:
    msg = f"{user.id}.{int(time.time()) + MAX_AGE}.{user.pw_hash[-8:]}"
    return f"{msg}.{_sig(msg)}"


def read_token(token: str | None, db):
    """Retorna l'usuari de la galeta o None."""
    from .db import User
    if not token or token.count(".") != 3:
        return None
    uid, exp, fp, sig = token.split(".")
    if not hmac.compare_digest(sig, _sig(f"{uid}.{exp}.{fp}")):
        return None
    if not exp.isdigit() or int(exp) < time.time() or not uid.isdigit():
        return None
    user = db.get(User, int(uid))
    if not user or user.pw_hash[-8:] != fp:
        return None
    return user


def users_exist(db) -> bool:
    from .db import User
    return (db.scalar(select(func.count()).select_from(User)) or 0) > 0


def seed_admin() -> None:
    from .db import SessionLocal, User
    pwd = get_settings().dashboard_password
    if not pwd:
        return
    with SessionLocal() as db:
        if not users_exist(db):
            db.add(User(username="admin", pw_hash=hash_password(pwd), is_admin=True,
                        created=datetime.now(timezone.utc)))
            db.commit()
