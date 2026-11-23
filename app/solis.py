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


MONTH_PATH = "/v1/api/inverterMonth"


def _post(settings: Settings, path: str, payload: dict,
          client: httpx.Client | None = None):
    body = json.dumps(payload, separators=(",", ":"))
    headers = sign_headers(settings.solis_key_id, settings.solis_key_secret, path, body)
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(settings.solis_api_url + path, content=body, headers=headers)
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        hint = ""
        if "wrong sign" in r.text:
            hint = " (revisa SOLIS_KEY_ID y SOLIS_KEY_SECRET: no intercambiados ni con espacios)"
        raise SolisError(f"HTTP {r.status_code}: {r.text[:300]}{hint}")
    data = r.json()
    if not data.get("success") or str(data.get("code")) != "0":
        raise SolisError(f"SolisCloud respondió {data.get('code')}: {data.get('msg')}")
    return data.get("data")


def _inverter_ref(settings: Settings) -> dict:
    payload = {}
    if settings.solis_inverter_id:
        payload["id"] = settings.solis_inverter_id
    if settings.solis_inverter_sn:
        payload["sn"] = settings.solis_inverter_sn
    return payload


def fetch_inverter_detail(settings: Settings, client: httpx.Client | None = None) -> dict:
    return _post(settings, DETAIL_PATH, _inverter_ref(settings), client) or {}


def fetch_inverter_month(settings: Settings, month: str,
                         client: httpx.Client | None = None) -> list:
    """Producción diaria de un mes ("2026-11") según SolisCloud."""
    payload = {**_inverter_ref(settings), "money": "EUR", "month": month, "timeZone": 1}
    data = _post(settings, MONTH_PATH, payload, client)
    return data if isinstance(data, list) else (data or {}).get("records") or []


DAY_PATH = "/v1/api/inverterDay"


def fetch_inverter_day(settings: Settings, day: str,
                       client: httpx.Client | None = None) -> list:
    """Dades de tot un dia ("2026-11-23"), un punt cada ~5 min."""
    payload = {**_inverter_ref(settings), "money": "EUR", "time": day, "timeZone": 1}
    data = _post(settings, DAY_PATH, payload, client)
    return data if isinstance(data, list) else (data or {}).get("records") or []


def parse_month(records: list) -> dict[str, float]:
    """{"2026-11-23": kWh} a partir de la respuesta de inverterMonth."""
    out: dict[str, float] = {}
    for r in records or []:
        day = r.get("dateStr") or r.get("timeStr")
        if not day and r.get("date") is not None:
            try:
                from datetime import datetime, timezone
                from zoneinfo import ZoneInfo
                ms = float(r["date"])
                day = datetime.fromtimestamp(ms / 1000, timezone.utc) \
                    .astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
            except (TypeError, ValueError):
                day = None
        kwh = _scaled(r.get("energy"), r.get("energyStr", "kWh"), "kWh")
        if day and kwh is not None:
            out[str(day)[:10]] = kwh
    return out


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
        power = _num(detail.get(f"pow{s.input}"))
        if power is None and voltage is not None and current is not None:
            power = voltage * current
        strings.append({"string_id": s.id, "tracker": s.tracker,
                        "voltage_v": voltage, "current_a": current, "power_w": power})
    return {
        "ac_power_w": _scaled(detail.get("pac"), detail.get("pacStr", "kW"), "W"),
        "day_energy_kwh": _scaled(detail.get("eToday"), detail.get("eTodayStr", "kWh"), "kWh"),
        "month_energy_kwh": _scaled(detail.get("eMonth"), detail.get("eMonthStr", "kWh"), "kWh"),
        "year_energy_kwh": _scaled(detail.get("eYear"), detail.get("eYearStr", "kWh"), "kWh"),
        "strings": strings,
    }
