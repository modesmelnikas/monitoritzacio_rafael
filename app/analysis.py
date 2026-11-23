"""Comparación entre strings.

Todos los strings son iguales (15 x 210 W) y todos los seguidores siguen al
sol, así que en un mismo instante deberían dar casi la misma corriente. Se
compara cada entrada con la mediana de las entradas de su mismo tipo (las
de 2 strings en paralelo entre sí, las de 1 string entre sí), usando la
corriente dividida por el número de strings:

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
    n_of = {d.id: d.n_strings for d in plant.strings}
    label_of = {d.id: d.label for d in plant.strings}
    input_of = {d.id: d.input for d in plant.strings}

    def per_string(s: dict) -> float | None:
        i = s.get("current_a")
        return None if i is None else i / n_of.get(s["string_id"], 1)

    # Se compara cada entrada con las de su mismo tipo (las S1+S2 en paralelo
    # entre sí, las S3 entre sí): en la primera lectura real las S3 daban de
    # forma sistemática más corriente por string que las S1+S2 (posición en el
    # seguidor, sombras entre seguidores con el sol bajo).
    groups: dict[int, list[float]] = {}
    for s in strings:
        i = per_string(s)
        if i is not None:
            groups.setdefault(n_of.get(s["string_id"], 1), []).append(i)
    med_group = {n: median(v) for n, v in groups.items() if v}
    currents = [i for v in groups.values() for i in v]
    med_i = median(currents) if currents else None
    active = med_i is not None and med_i >= float(cfg["min_median_current_a"])

    out_strings = []
    for s in strings:
        s = dict(s)
        n = n_of.get(s["string_id"], 1)
        s["n_strings"] = n
        s["label"] = label_of.get(s["string_id"], s["string_id"])
        s["input"] = input_of.get(s["string_id"])
        i = per_string(s)
        s["current_per_string_a"] = None if i is None else round(i, 3)
        if i is None:
            s.update(deviation_pct=None, status=NODATA, message="Sense dada")
        elif not active:
            s.update(deviation_pct=None, status=IDLE, message="Poca radiació")
        else:
            ref = med_group.get(n) or med_i
            dev = (i - ref) / ref * 100 if ref else 0.0
            s["deviation_pct"] = round(dev, 1)
            if i < 0.1:
                s.update(status=ALARM, message="Entrada sense corrent (fusible, connector?)")
            elif n > 1 and -100 / n - 12 <= dev <= -100 / n + 12:
                s.update(status=ALARM, message=f"Corrent per string {dev:.0f} %: "
                         f"probablement un dels {n} strings en paral·lel no dona corrent")
            elif dev <= -float(cfg["alarm_pct"]):
                s.update(status=ALARM, message=f"Corrent per string {dev:.0f} % respecte a les del seu tipus")
            elif dev <= -float(cfg["warning_pct"]):
                s.update(status=WARNING, message=f"Corrent per string {dev:.0f} % respecte a les del seu tipus")
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
        issues = []
        low = [x for x in ss if x["status"] in (WARNING, ALARM)]
        if active and len(ss) >= 2 and len(low) == len(ss):
            msgs.append("Tots els strings baixos: revisar el seguiment del seguidor")
            issues.append({"kind": "tracking", "severity": WARNING, "string_id": None,
                           "text": msgs[-1]})
        v = tracker_v.get(t.id)
        v_dev = None
        if active and v and med_v:
            v_dev = round((v - med_v) / med_v * 100, 1)
            if v_dev <= -float(cfg["voltage_warning_pct"]):
                status = worst(status, WARNING)
                msgs.append(f"Tensió {v_dev:.0f} % baixa (panells en bypass?)")
                issues.append({"kind": "voltage", "severity": WARNING, "string_id": None,
                               "text": msgs[-1]})
        msgs += [f"{x['label']}: {x['message']}" for x in low]
        issues += [{"kind": "current", "severity": x["status"], "string_id": x["string_id"],
                    "text": f"{x['label']}: {x['message']}"} for x in low]
        trackers.append({"id": t.id, "x": t.x, "y": t.y, "mppt": t.mppt,
                         "power_w": round(power, 1), "voltage_v": v,
                         "voltage_dev_pct": v_dev, "status": status,
                         "messages": msgs, "issues": issues, "strings": ss})

    return {"active": active, "median_current_a": med_i, "median_voltage_v": med_v,
            "median_by_group": {f"{n} string": round(m, 3) for n, m in med_group.items()},
            "status": worst(*[t["status"] for t in trackers]) if trackers else NODATA,
            "trackers": trackers}
