"""Comparación entre strings.

Todos los strings son iguales (15 x 210 W) y todos los seguidores siguen al
sol, así que en un mismo instante deberían dar casi la misma corriente. Se
compara cada string con la mediana de todos:

* corriente muy por debajo de la mediana -> aviso / alarma
* corriente ~0 cuando los demás producen  -> alarma (fusible, conector, string abierto)
* todos los strings de un seguidor bajos a la vez -> probable problema del
  seguidor (no orienta bien, parado, sombra)
* tensión de la MPPT por debajo de la mediana -> paneles en bypass / diodo
"""
from __future__ import annotations

from statistics import median

from .config import Plant

OK, WARNING, ALARM, IDLE, NODATA = "ok", "warning", "alarm", "idle", "nodata"
_RANK = {NODATA: 0, IDLE: 0, OK: 1, WARNING: 2, ALARM: 3}


def worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0)) if statuses else NODATA


def analyze(strings: list[dict], plant: Plant) -> dict:
    cfg = plant.analysis
    currents = [s["current_a"] for s in strings if s.get("current_a") is not None]
    med_i = median(currents) if currents else None
    active = med_i is not None and med_i >= float(cfg["min_median_current_a"])

    out_strings = []
    for s in strings:
        s = dict(s)
        i = s.get("current_a")
        if i is None:
            s.update(deviation_pct=None, status=NODATA, message="Sin dato")
        elif not active:
            s.update(deviation_pct=None, status=IDLE, message="Poca radiación")
        else:
            dev = (i - med_i) / med_i * 100
            s["deviation_pct"] = round(dev, 1)
            if i < 0.1:
                s.update(status=ALARM, message="String sin corriente (¿fusible, conector?)")
            elif dev <= -float(cfg["alarm_pct"]):
                s.update(status=ALARM, message=f"Corriente {dev:.0f} % respecto a la media")
            elif dev <= -float(cfg["warning_pct"]):
                s.update(status=WARNING, message=f"Corriente {dev:.0f} % respecto a la media")
            else:
                s.update(status=OK, message="")
        out_strings.append(s)

    by_tracker: dict[str, list[dict]] = {}
    for s in out_strings:
        by_tracker.setdefault(s["tracker"], []).append(s)

    # tensión por seguidor (MPPT) frente a la mediana
    tracker_v = {}
    for tid, ss in by_tracker.items():
        vs = [x["voltage_v"] for x in ss if x.get("voltage_v")]
        tracker_v[tid] = sum(vs) / len(vs) if vs else None
    vs_all = [v for v in tracker_v.values() if v]
    med_v = median(vs_all) if vs_all else None

    trackers = []
    for t in plant.trackers:
        ss = by_tracker.get(t.id, [])
        power = sum(x["power_w"] or 0 for x in ss)
        statuses = [x["status"] for x in ss] or [NODATA]
        status = worst(*statuses)
        msgs = []
        low = [x for x in ss if x["status"] in (WARNING, ALARM)]
        if active and len(ss) >= 2 and len(low) == len(ss):
            msgs.append("Todos sus strings bajos: revisar seguimiento del seguidor")
        v = tracker_v.get(t.id)
        v_dev = None
        if active and v and med_v:
            v_dev = round((v - med_v) / med_v * 100, 1)
            if v_dev <= -float(cfg["voltage_warning_pct"]):
                status = worst(status, WARNING)
                msgs.append(f"Tensión {v_dev:.0f} % baja (¿paneles en bypass?)")
        msgs += [f"{x['string_id']}: {x['message']}" for x in low]
        trackers.append({"id": t.id, "x": t.x, "y": t.y, "mppt": t.mppt,
                         "power_w": round(power, 1), "voltage_v": v,
                         "voltage_dev_pct": v_dev, "status": status,
                         "messages": msgs, "strings": ss})

    return {"active": active, "median_current_a": med_i, "median_voltage_v": med_v,
            "status": worst(*[t["status"] for t in trackers]) if trackers else NODATA,
            "trackers": trackers}
