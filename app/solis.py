"""Cliente mínimo de la SolisCloud Platform API (v1).

Autenticación (documento "SolisCloud Platform API" de Ginlong):
  Content-MD5   = base64(md5(body))
  Date          = fecha GMT, p. ej. "Mon, 23 Nov 2026 13:00:00 GMT"
  Authorization = "API " + KeyId + ":" + base64(HMAC-SHA1(KeySecret,
                   "POST\\n" + Content-MD5 + "\\n" + Content-Type + "\\n" + Date + "\\n" + path))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx

from .config import Plant, Settings

# El cliente de referencia de SolisCloud firma con "application/json" a secas;
# con ";charset=UTF-8" el servidor responde "wrong sign".
CONTENT_TYPE = "application/json"
DETAIL_PATH = "/v1/api/inverterDetail"


class SolisError(RuntimeError):
    pass


def sign_headers(key_id: str, key_secret: str, path: str, body: str,
                 now: datetime | None = None) -> dict[str, str]:
    md5 = base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode()
    date = format_datetime(now or datetime.now(timezone.utc), usegmt=True)
    to_sign = f"POST\n{md5}\n{CONTENT_TYPE}\n{date}\n{path}"
    sig = base64.b64encode(hmac.new(key_secret.encode(), to_sign.encode(),
                                    hashlib.sha1).digest()).decode()
    return {"Content-MD5": md5, "Content-Type": CONTENT_TYPE, "Date": date,
            "Authorization": f"API {key_id}:{sig}"}


def fetch_inverter_detail(settings: Settings, client: httpx.Client | None = None) -> dict:
    payload = {}
    if settings.solis_inverter_id:
        payload["id"] = settings.solis_inverter_id
    if settings.solis_inverter_sn:
        payload["sn"] = settings.solis_inverter_sn
    body = json.dumps(payload, separators=(",", ":"))
    headers = sign_headers(settings.solis_key_id, settings.solis_key_secret, DETAIL_PATH, body)
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(settings.solis_api_url + DETAIL_PATH, content=body, headers=headers)
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise SolisError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if not data.get("success") or str(data.get("code")) != "0":
        raise SolisError(f"SolisCloud respondió {data.get('code')}: {data.get('msg')}")
    return data.get("data") or {}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scaled(value, unit: str | None, base: str) -> float | None:
    """Pasa a W o kWh según la unidad que indique SolisCloud (W/kW/MW, kWh/MWh)."""
    v = _num(value)
    if v is None:
        return None
    u = (unit or "").strip()
    factors = {"W": {"W": 1, "kW": 1e3, "MW": 1e6},
               "kWh": {"Wh": 1e-3, "kWh": 1, "MWh": 1e3, "GWh": 1e6}}[base]
    default = 1e3 if base == "W" else 1  # sin unidad: kW y kWh
    return v * factors.get(u, default)


def parse_detail(detail: dict, plant: Plant) -> dict:
    """Convierte la respuesta de inverterDetail en lecturas por string."""
    strings = []
    for s in plant.strings:
        v_key = f"uPv{s.mppt}" if plant.voltage_by == "mppt" else f"uPv{s.input}"
        voltage = _num(detail.get(v_key))
        current = _num(detail.get(f"iPv{s.input}"))
        power = None
        if voltage is not None and current is not None:
            power = voltage * current
        strings.append({"string_id": s.id, "tracker": s.tracker,
                        "voltage_v": voltage, "current_a": current, "power_w": power})
    return {
        "ac_power_w": _scaled(detail.get("pac"), detail.get("pacStr", "kW"), "W"),
        "day_energy_kwh": _scaled(detail.get("eToday"), detail.get("eTodayStr", "kWh"), "kWh"),
        "strings": strings,
    }
