"""Datos simulados para probar sin credenciales de SolisCloud.

Curva solar simple (los seguidores a dos ejes dan una campana ancha) y
algunos fallos fijos para ver las alertas: el S3 del 121 rinde un 30 %
menos (suciedad / panel roto) y en el 124 uno de los strings en paralelo
S1+S2 no da corriente (la entrada da la mitad).
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Plant

LOCAL_TZ = ZoneInfo("Europe/Madrid")
FAULTS = {"121-S3": 0.7, "124-S1+S2": 0.5}


def simulate(plant: Plant, now: datetime | None = None, seed: int | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rnd = random.Random(seed)
    local = now.astimezone(LOCAL_TZ)
    hour = local.hour + local.minute / 60
    # sol entre 7:30 y 18:30, máximo a las 13:00 hora local
    x = (hour - 13.0) / 5.5
    sun = max(0.0, math.cos(x * math.pi / 2)) ** 0.6 if abs(x) < 1 else 0.0
    isc = 8.2  # A aprox. de un panel de 210 W antiguo
    strings = []
    mppt_v: dict[int, float] = {}
    for s in plant.strings:
        if s.mppt not in mppt_v:
            mppt_v[s.mppt] = (0.0 if sun == 0 else plant.panels_per_string * (36.5 - 2.5 * sun) * rnd.uniform(0.99, 1.01))
        factor = FAULTS.get(s.id, 1.0)
        current = s.n_strings * isc * 0.93 * sun * factor * rnd.uniform(0.97, 1.03)
        v = mppt_v[s.mppt]
        strings.append({"string_id": s.id, "tracker": s.tracker, "voltage_v": round(v, 1),
                        "current_a": round(current, 2), "power_w": round(v * current, 1)})
    dc = sum(s["power_w"] for s in strings)
    return {"ac_power_w": round(dc * 0.97, 1), "day_energy_kwh": None, "strings": strings}
