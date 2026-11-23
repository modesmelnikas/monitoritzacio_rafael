"""Lectura periódica: SolisCloud (o simulador) -> análisis -> base de datos."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .analysis import analyze
from .config import get_plant, get_settings
from .db import (Alert, DailyEnergy, InputDaily, SessionLocal, Snapshot, StringReading,
                 init_db)
from .simulator import LOCAL_TZ, simulate
from .solis import fetch_inverter_detail, fetch_inverter_month, parse_detail, parse_month

log = logging.getLogger("poller")


NIGHT_AC_W = 300      # per sota d'això (0,3 % de 100 kW) es considera nit
NIGHT_MAX_A = 0.5     # corrents residuals de capvespre (p. ex. 0,03 A)


def _is_night(data: dict) -> bool:
    currents = [s["current_a"] or 0 for s in data["strings"]]
    return max(currents, default=0) < NIGHT_MAX_A and (data["ac_power_w"] or 0) < NIGHT_AC_W


def save_daily(values: dict[str, float], now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        for day, kwh in values.items():
            row = db.get(DailyEnergy, day)
            if row:
                row.kwh, row.updated = kwh, now
            else:
                db.add(DailyEnergy(day=day, kwh=kwh, updated=now))
        db.commit()


def sync_month_history(settings, now: datetime) -> None:
    """Trae de SolisCloud la producción diaria del mes (y del anterior, la 1.ª vez)."""
    from sqlalchemy import func, select
    local = now.astimezone(LOCAL_TZ)
    months = [local.strftime("%Y-%m")]
    with SessionLocal() as db:
        if (db.scalar(select(func.count()).select_from(DailyEnergy)) or 0) < 25:
            prev = (local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            months.insert(0, prev)
    for m in months:
        records = fetch_inverter_month(settings, m)
        if records:
            log.info("inverterMonth %s: %d días, ejemplo %s", m, len(records), records[0])
        save_daily(parse_month(records), now)


MAX_GAP = timedelta(minutes=30)


def _accumulate_energy(db, now: datetime, readings: list[dict]) -> None:
    """Suma a input_daily la energía desde la lectura anterior (regla del trapecio)."""
    from sqlalchemy import select
    prev = db.scalars(select(Snapshot).order_by(Snapshot.ts.desc()).limit(1)).first()
    if not prev:
        return
    pts = prev.ts if prev.ts.tzinfo else prev.ts.replace(tzinfo=timezone.utc)
    dt = now - pts
    day = now.astimezone(LOCAL_TZ).date().isoformat()
    if not (timedelta(0) < dt <= MAX_GAP) or pts.astimezone(LOCAL_TZ).date().isoformat() != day:
        return
    p0 = {r.string_id: r.power_w or 0.0 for r in
          db.scalars(select(StringReading).where(StringReading.snapshot_id == prev.id))}
    hours = dt.total_seconds() / 3600
    for r in readings:
        if r["string_id"] not in p0:
            continue
        kwh = (p0[r["string_id"]] + (r["power_w"] or 0.0)) / 2 * hours / 1000
        row = db.get(InputDaily, (day, r["string_id"]))
        if row:
            row.kwh += kwh
        else:
            db.add(InputDaily(day=day, string_id=r["string_id"], tracker=r["tracker"], kwh=kwh))


def sync_alerts(db, now: datetime, result: dict, data: dict | None = None,
                raw: dict | None = None) -> None:
    """Aplica les regles d'alarma i en desa el resultat al registre.

    Una alarma nova queda "pending" fins que la regla es compleix les lectures
    seguides que demana (min_readings); si abans deixa de complir-se, s'esborra.
    """
    from sqlalchemy import select
    from .db import AlertRule
    from .rules import evaluate
    plant = get_plant()
    data = data or {}
    hours = plant.analysis.get("alarm_hours")
    if hours:
        h = now.astimezone(LOCAL_TZ)
        h = h.hour + h.minute / 60
        if not (float(hours[0]) <= h < float(hours[1])):
            return   # fora de l'horari d'alarmes: no s'obre ni es confirma res
    seen: set[int] = set()
    opened: list[Alert] = []
    for rule in db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))):
        for hit in evaluate(rule, result, data, raw, now, plant):
            kind = f"rule:{rule.id}"
            q = select(Alert).where(Alert.state.in_(("open", "pending")), Alert.source == "auto",
                                    Alert.kind == kind)
            q = q.where(Alert.tracker == hit["tracker"]) if hit["tracker"] else \
                q.where(Alert.tracker.is_(None))
            q = q.where(Alert.string_id == hit["string_id"]) if hit["string_id"] else \
                q.where(Alert.string_id.is_(None))
            a = db.scalars(q).first()
            title = f"{rule.name}: {hit['text']}"[:200]
            need = max(1, rule.min_readings or 1)
            if a:
                a.last_seen, a.count, a.title, a.severity = now, a.count + 1, title, rule.severity
                if a.state == "pending" and a.count >= need:
                    a.state = "open"
                    opened.append(a)
            else:
                a = Alert(created=now, last_seen=now, source="auto", kind=kind,
                          severity=rule.severity, tracker=hit["tracker"],
                          string_id=hit["string_id"], title=title, count=1,
                          state="open" if need <= 1 else "pending")
                db.add(a)
                db.flush()
                if a.state == "open":
                    opened.append(a)
            seen.add(a.id)
    for a in db.scalars(select(Alert).where(Alert.state == "pending")):
        if a.id not in seen:
            db.delete(a)
    if opened:
        from . import notify
        for a in opened:
            notify.send(notify.alert_text(plant.name, a.severity, a.title))


def fetch_reading(now: datetime | None = None) -> tuple[datetime, dict, dict | None, str]:
    """Lee el inversor (o el simulador) sin tocar la base de datos."""
    settings, plant = get_settings(), get_plant()
    now = now or datetime.now(timezone.utc)
    if settings.use_simulator:
        return now, simulate(plant, now), None, "sim"
    if not settings.has_solis_credentials:
        raise RuntimeError("Faltan SOLIS_KEY_ID / SOLIS_KEY_SECRET / SOLIS_INVERTER_SN")
    raw = fetch_inverter_detail(settings)
    return now, parse_detail(raw, plant), raw, "solis"


def collect_once(now: datetime | None = None, keep_night: bool = False) -> int | None:
    """Lee el inversor y guarda. Devuelve el id de la lectura o None si no se guarda."""
    reading = fetch_reading(now)
    return store_reading(*reading, keep_night=keep_night)


def store_reading(now: datetime, data: dict, raw: dict | None, source: str,
                  keep_night: bool = False) -> int | None:
    settings, plant = get_settings(), get_plant()
    if _is_night(data) and not keep_night and source == "solis":
        log.info("Inversor sin producción: no se guarda la lectura")
        return None
    result = analyze(data["strings"], plant)
    with SessionLocal() as db:
        if source == "solis" and not keep_night:
            # dues vies de lectura (cron extern i GitHub Actions): evita duplicats
            last = db.scalar(select(Snapshot.ts).order_by(Snapshot.ts.desc()).limit(1))
            if last is not None:
                last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < 240:
                    log.info("Ja hi ha una lectura de fa menys de 4 min: no es guarda")
                    return None
        _accumulate_energy(db, now, data["strings"])
        if raw is not None:
            # solo se guarda la última respuesta cruda, para no llenar la base de datos
            db.execute(update(Snapshot).where(Snapshot.raw.is_not(None)).values(raw=None))
        snap = Snapshot(ts=now, source=source, ac_power_w=data["ac_power_w"],
                        day_energy_kwh=data["day_energy_kwh"], raw=raw)
        db.add(snap)
        db.flush()
        sync_alerts(db, now, result, data, raw)
        for t in result["trackers"]:
            for s in t["strings"]:
                db.add(StringReading(snapshot_id=snap.id, ts=now, string_id=s["string_id"],
                                     tracker=s["tracker"], voltage_v=s["voltage_v"],
                                     current_a=s["current_a"], power_w=s["power_w"],
                                     deviation_pct=s["deviation_pct"], status=s["status"]))
        db.commit()
        log.info("Lectura %s guardada (%s), estado %s", snap.id, source, result["status"])
        snap_id = snap.id
    if data.get("day_energy_kwh") is not None:
        save_daily({now.astimezone(LOCAL_TZ).date().isoformat(): data["day_energy_kwh"]}, now)
    if source == "solis":
        try:
            sync_month_history(settings, now)
        except Exception:  # noqa: BLE001 - el histórico es secundario
            log.exception("No se pudo leer la producción mensual de SolisCloud")
    return snap_id


def _rebuild_input_daily(db, day: str) -> None:
    """Recalcula input_daily d'un dia a partir de totes les lectures guardades."""
    from sqlalchemy import delete
    start = datetime.fromisoformat(day).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    snaps = list(db.scalars(select(Snapshot).where(Snapshot.ts >= start, Snapshot.ts < end)
                            .order_by(Snapshot.ts)))
    totals: dict[str, list] = {}
    prev_ts, prev_p = None, {}
    for sn in snaps:
        ts = sn.ts if sn.ts.tzinfo else sn.ts.replace(tzinfo=timezone.utc)
        rows = list(db.scalars(select(StringReading).where(StringReading.snapshot_id == sn.id)))
        cur = {r.string_id: (r.power_w or 0.0, r.tracker) for r in rows}
        if prev_ts is not None and timedelta(0) < ts - prev_ts <= MAX_GAP:
            h = (ts - prev_ts).total_seconds() / 3600
            for sid, (p1, trk) in cur.items():
                if sid in prev_p:
                    t = totals.setdefault(sid, [0.0, trk])
                    t[0] += (prev_p[sid] + p1) / 2 * h / 1000
        prev_ts, prev_p = ts, {k: v[0] for k, v in cur.items()}
    db.execute(delete(InputDaily).where(InputDaily.day == day))
    for sid, (kwh, trk) in totals.items():
        db.add(InputDaily(day=day, string_id=sid, tracker=trk, kwh=kwh))


def backfill_day(day: str) -> int:
    """Omple els forats d'un dia amb les dades de 5 min de SolisCloud (inverterDay).

    No toca les lectures que ja hi són (deixa 4 min de marge) ni genera alarmes.
    """
    from .solis import fetch_inverter_day
    settings, plant = get_settings(), get_plant()
    points = fetch_inverter_day(settings, day)
    log.info("SolisCloud: %d punts el %s", len(points), day)
    if points:
        log.info("Camps d'un punt: %s", sorted(points[0].keys()))
    init_db()
    added = 0
    with SessionLocal() as db:
        have = [t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                for t in db.scalars(select(Snapshot.ts))]
        for pt in sorted(points, key=lambda p: float(p.get("dataTimestamp") or 0)):
            try:
                ts = datetime.fromtimestamp(float(pt["dataTimestamp"]) / 1000, timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if ts.astimezone(LOCAL_TZ).date().isoformat() != day:
                continue
            data = parse_detail(pt, plant)
            # a inverterDay, "pac" ve en W encara que pacStr digui kW
            if data["ac_power_w"] and data["ac_power_w"] > 2e6:
                data["ac_power_w"] /= 1000
            if _is_night(data) or any(abs((ts - h).total_seconds()) < 240 for h in have):
                continue
            result = analyze(data["strings"], plant)
            snap = Snapshot(ts=ts, source="solis", ac_power_w=data["ac_power_w"],
                            day_energy_kwh=data["day_energy_kwh"])
            db.add(snap)
            db.flush()
            for t in result["trackers"]:
                for s_ in t["strings"]:
                    db.add(StringReading(snapshot_id=snap.id, ts=ts, string_id=s_["string_id"],
                                         tracker=s_["tracker"], voltage_v=s_["voltage_v"],
                                         current_a=s_["current_a"], power_w=s_["power_w"],
                                         deviation_pct=s_["deviation_pct"], status=s_["status"]))
            have.append(ts)
            added += 1
        db.flush()
        _rebuild_input_daily(db, day)
        db.commit()
    log.info("Afegides %d lectures del %s", added, day)
    return added


def backfill_simulation(days: float = 2) -> int:
    """En modo simulado, rellena histórico para que el dashboard tenga curvas."""
    from sqlalchemy import func, select
    with SessionLocal() as db:
        if db.scalar(select(func.count(Snapshot.id))):
            return 0
    step = timedelta(minutes=get_settings().poll_minutes)
    now = datetime.now(timezone.utc)
    t, n = now - timedelta(days=days), 0
    while t < now:
        collect_once(t)
        t += step
        n += 1
    import random
    rnd = random.Random(7)
    today = now.astimezone(LOCAL_TZ).date()
    save_daily({(today - timedelta(days=k)).isoformat(): round(rnd.uniform(120, 330), 1)
                for k in range(0, 45)}, now)
    return n


def safe_collect() -> None:
    try:
        collect_once()
    except Exception:  # noqa: BLE001 - el planificador no debe morir
        log.exception("Error leyendo el inversor")


def run_batch(readings: int = 1, interval_s: float = 300) -> int:
    """Toma varias lecturas separadas `interval_s` y las guarda todas al final.

    GitHub Actions arranca cada 10 min y toma 2 lecturas separadas 5 min: así hay
    un dato cada 5 min pero la base de datos (Neon, plan gratuito con horas de
    cómputo limitadas) solo se despierta una vez cada 10 min. Si todas las
    lecturas son de noche, ni se conecta.
    """
    import time
    batch = []
    for k in range(max(1, readings)):
        if k:
            time.sleep(interval_s)
        try:
            batch.append(fetch_reading())
        except Exception:  # noqa: BLE001 - si falla una, se guardan las demás
            log.exception("Error leyendo el inversor")
    batch = [b for b in batch if not (b[3] == "solis" and _is_night(b[1]))]
    if not batch:
        log.info("Nada que guardar (noche o sin lecturas)")
        return 0
    init_db()
    for b in batch:
        store_reading(*b)
    return len(batch)


if __name__ == "__main__":  # python -m app.poller [lecturas] [segundos]  (GitHub Actions)
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 2 and sys.argv[1] == "--dia":   # python -m app.poller --dia 2026-11-23
        for d in sys.argv[2].split(","):
            backfill_day(d.strip())
        sys.exit(0)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 300
    run_batch(n, gap)
