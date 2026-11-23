"""Regles d'alarma configurables.

Cada regla té un tipus, un llindar, quantes lectures seguides s'ha de complir
abans de donar d'alta l'alarma, a quin seguidor s'aplica (o a tots) i la gravetat.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

from .config import Plant

# tipus -> (nom, descripció, unitat del llindar o None, llindar per defecte, necessita sol)
RULE_TYPES: dict[str, dict] = {
    "string_dev": {"name": "Desviació d'un string",
                   "desc": "Un string dona un X % menys de corrent que els del seu tipus",
                   "unit": "%", "default": 20, "sun": True},
    "string_zero": {"name": "String sense corrent",
                    "desc": "Una entrada dona 0 A mentre les altres produeixen (fusible, connector)",
                    "unit": None, "default": None, "sun": True},
    "string_spread": {"name": "Diferència dins d'un seguidor",
                      "desc": "Entre el millor i el pitjor string d'un mateix seguidor hi ha més d'un X %",
                      "unit": "%", "default": 30, "sun": True},
    "tracker_dev": {"name": "Desviació d'un seguidor",
                    "desc": "Un seguidor produeix un X % menys (per kWp) que la mediana dels seguidors",
                    "unit": "%", "default": 15, "sun": True},
    "voltage_dev": {"name": "Tensió baixa d'un seguidor",
                    "desc": "La tensió de la MPPT d'un seguidor és un X % més baixa que la mediana",
                    "unit": "%", "default": 10, "sun": True},
    "ac_low": {"name": "Potència AC baixa",
               "desc": "La potència de l'inversor és inferior a X kW amb sol",
               "unit": "kW", "default": 5, "sun": True},
    "stale_data": {"name": "Dades antigues",
                   "desc": "L'última dada de SolisCloud té més de X minuts (comunicació)",
                   "unit": "min", "default": 30, "sun": False},
}

DEFAULT_RULES = [
    {"name": "String per sota de la resta", "rtype": "string_dev", "threshold": 20,
     "min_readings": 3, "severity": "alarm"},
    {"name": "String sense corrent", "rtype": "string_zero", "threshold": None,
     "min_readings": 2, "severity": "alarm"},
    {"name": "Seguidor per sota de la resta", "rtype": "tracker_dev", "threshold": 15,
     "min_readings": 3, "severity": "warning"},
    {"name": "Tensió baixa", "rtype": "voltage_dev", "threshold": 10,
     "min_readings": 3, "severity": "warning"},
    {"name": "Sense dades noves de SolisCloud", "rtype": "stale_data", "threshold": 30,
     "min_readings": 1, "severity": "warning"},
]


def seed_default_rules() -> None:
    from sqlalchemy import func, select
    from .db import AlertRule, SessionLocal
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(AlertRule)):
            return
        for r in DEFAULT_RULES:
            db.add(AlertRule(enabled=True, tracker=None, **r))
        db.commit()


def evaluate(rule, result: dict, data: dict, raw: dict | None, now: datetime,
             plant: Plant) -> list[dict]:
    """Retorna les coincidències: [{tracker, string_id, text}]."""
    meta = RULE_TYPES.get(rule.rtype)
    if not meta or not rule.enabled:
        return []
    if meta["sun"] and not result.get("active"):
        return []
    thr = rule.threshold if rule.threshold is not None else meta["default"]
    trackers = [t for t in result["trackers"] if not rule.tracker or t["id"] == rule.tracker]
    hits: list[dict] = []
    if rule.rtype == "string_dev":
        for t in trackers:
            for s in t["strings"]:
                d = s.get("deviation_pct")
                if d is not None and d <= -thr:
                    hits.append({"tracker": t["id"], "string_id": s["string_id"],
                                 "text": f"{s['label']}: {d:.0f} % respecte als del seu tipus"})
    elif rule.rtype == "string_zero":
        for t in trackers:
            for s in t["strings"]:
                if s.get("current_a") is not None and s["current_a"] < 0.1:
                    hits.append({"tracker": t["id"], "string_id": s["string_id"],
                                 "text": f"{s['label']}: entrada sense corrent"})
    elif rule.rtype == "string_spread":
        for t in trackers:
            vals = [(s["power_w"] or 0) / max(1, s.get("n_strings", 1)) for s in t["strings"]
                    if s.get("power_w") is not None]
            if len(vals) > 1 and max(vals) > 0:
                spread = (max(vals) - min(vals)) / max(vals) * 100
                if spread >= thr:
                    hits.append({"tracker": t["id"], "string_id": None,
                                 "text": f"Diferència de {spread:.0f} % entre strings"})
    elif rule.rtype == "tracker_dev":
        kwp = {t.id: plant.string_wp * sum(s.n_strings for s in t.strings) / 1000
               for t in plant.trackers}
        spec = {t["id"]: t["power_w"] / kwp[t["id"]] for t in result["trackers"]
                if kwp.get(t["id"])}
        if spec:
            med = median(spec.values())
            for t in trackers:
                if med > 0 and t["id"] in spec:
                    d = (spec[t["id"]] - med) / med * 100
                    if d <= -thr:
                        hits.append({"tracker": t["id"], "string_id": None,
                                     "text": f"Produeix un {abs(d):.0f} % menys que la mediana"})
    elif rule.rtype == "voltage_dev":
        for t in trackers:
            d = t.get("voltage_dev_pct")
            if d is not None and d <= -thr:
                hits.append({"tracker": t["id"], "string_id": None,
                             "text": f"Tensió {d:.0f} % respecte a la mediana"})
    elif rule.rtype == "ac_low":
        ac = data.get("ac_power_w")
        if ac is not None and ac < thr * 1000:
            hits.append({"tracker": None, "string_id": None,
                         "text": f"Potència AC {ac / 1000:.1f} kW (< {thr:g} kW)"})
    elif rule.rtype == "stale_data":
        ts = (raw or {}).get("dataTimestamp")
        try:
            age = (now - datetime.fromtimestamp(float(ts) / 1000, timezone.utc)).total_seconds() / 60
        except (TypeError, ValueError):
            age = None
        if age is not None and age > thr:
            hits.append({"tracker": None, "string_id": None,
                         "text": f"L'última dada de SolisCloud és de fa {age:.0f} min"})
    return hits
