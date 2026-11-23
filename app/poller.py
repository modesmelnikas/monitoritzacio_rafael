"""Lectura periódica: SolisCloud (o simulador) -> análisis -> base de datos."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .analysis import analyze
from .config import get_plant, get_settings
from .db import SessionLocal, Snapshot, StringReading, init_db
from .simulator import simulate
from .solis import fetch_inverter_detail, parse_detail

log = logging.getLogger("poller")


def collect_once(now: datetime | None = None) -> int:
    settings, plant = get_settings(), get_plant()
    now = now or datetime.now(timezone.utc)
    if settings.use_simulator:
        data, raw, source = simulate(plant, now), None, "sim"
    else:
        raw = fetch_inverter_detail(settings)
        data, source = parse_detail(raw, plant), "solis"
    result = analyze(data["strings"], plant)
    with SessionLocal() as db:
        snap = Snapshot(ts=now, source=source, ac_power_w=data["ac_power_w"],
                        day_energy_kwh=data["day_energy_kwh"], raw=raw)
        db.add(snap)
        db.flush()
        for t in result["trackers"]:
            for s in t["strings"]:
                db.add(StringReading(snapshot_id=snap.id, ts=now, string_id=s["string_id"],
                                     tracker=s["tracker"], voltage_v=s["voltage_v"],
                                     current_a=s["current_a"], power_w=s["power_w"],
                                     deviation_pct=s["deviation_pct"], status=s["status"]))
        db.commit()
        log.info("Lectura %s guardada (%s), estado %s", snap.id, source, result["status"])
        return snap.id


def backfill_simulation(days: float = 2) -> int:
    """En modo simulado, rellena histórico para que el dashboard tenga curvas."""
    from datetime import timedelta
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
    return n


def safe_collect() -> None:
    try:
        collect_once()
    except Exception:  # noqa: BLE001 - el planificador no debe morir
        log.exception("Error leyendo el inversor")


if __name__ == "__main__":  # python -m app.poller  (útil como Cron Job de Render)
    logging.basicConfig(level=logging.INFO)
    init_db()
    collect_once()
