"""Lectura periódica: SolisCloud (o simulador) -> análisis -> base de datos."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from .analysis import analyze
from .config import get_plant, get_settings
from .db import (Alert, DailyEnergy, InputDaily, SessionLocal, Snapshot, StringReading,
                 init_db)
from .simulator import LOCAL_TZ, simulate
from .solis import fetch_inverter_detail, fetch_inverter_month, parse_detail, parse_month

log = logging.getLogger("poller")


def _is_night(data: dict) -> bool:
    currents = [s["current_a"] or 0 for s in data["strings"]]
    return max(currents, default=0) < 0.05 and not (data["ac_power_w"] or 0) > 0


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


def sync_alerts(db, now: datetime, result: dict) -> None:
    """Desa al registre les alarmes de l'anàlisi (una oberta per problema)."""
    from sqlalchemy import select
    if not result.get("active"):
        return
    for t in result["trackers"]:
        for it in t.get("issues", []):
            q = select(Alert).where(Alert.state == "open", Alert.source == "auto",
                                    Alert.kind == it["kind"], Alert.tracker == t["id"])
            q = q.where(Alert.string_id == it["string_id"]) if it["string_id"] else \
                q.where(Alert.string_id.is_(None))
            a = db.scalars(q).first()
            if a:
                a.last_seen, a.count, a.title = now, a.count + 1, it["text"]
                if it["severity"] == "alarm":
                    a.severity = "alarm"
            else:
                db.add(Alert(created=now, last_seen=now, source="auto", kind=it["kind"],
                             severity=it["severity"], tracker=t["id"],
                             string_id=it["string_id"], title=it["text"], count=1,
                             state="open"))


def collect_once(now: datetime | None = None, keep_night: bool = False) -> int | None:
    """Lee el inversor y guarda. Devuelve el id de la lectura o None si no se guarda."""
    settings, plant = get_settings(), get_plant()
    now = now or datetime.now(timezone.utc)
    if settings.use_simulator:
        data, raw, source = simulate(plant, now), None, "sim"
    else:
        if not settings.has_solis_credentials:
            raise RuntimeError("Faltan SOLIS_KEY_ID / SOLIS_KEY_SECRET / SOLIS_INVERTER_SN")
        raw = fetch_inverter_detail(settings)
        data, source = parse_detail(raw, plant), "solis"
    if _is_night(data) and not keep_night and source == "solis":
        log.info("Inversor sin producción: no se guarda la lectura")
        return None
    result = analyze(data["strings"], plant)
    with SessionLocal() as db:
        _accumulate_energy(db, now, data["strings"])
        if raw is not None:
            # solo se guarda la última respuesta cruda, para no llenar la base de datos
            db.execute(update(Snapshot).where(Snapshot.raw.is_not(None)).values(raw=None))
        snap = Snapshot(ts=now, source=source, ac_power_w=data["ac_power_w"],
                        day_energy_kwh=data["day_energy_kwh"], raw=raw)
        db.add(snap)
        db.flush()
        sync_alerts(db, now, result)
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


if __name__ == "__main__":  # python -m app.poller  (lo usa GitHub Actions)
    logging.basicConfig(level=logging.INFO)
    init_db()
    collect_once()
