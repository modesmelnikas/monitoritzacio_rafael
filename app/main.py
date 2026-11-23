"""Web app de monitorización de strings (FastAPI)."""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .analysis import analyze
from .config import ROOT, get_plant, get_settings
from .db import SessionLocal, Snapshot, StringReading, init_db
from .poller import collect_once, safe_collect

logging.basicConfig(level=logging.INFO)
LOCAL_TZ = ZoneInfo("Europe/Madrid")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    if settings.use_simulator:
        from .poller import backfill_simulation
        backfill_simulation()
    scheduler = None
    if settings.run_scheduler:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(safe_collect, "interval", minutes=settings.poll_minutes,
                          next_run_time=datetime.now(timezone.utc), max_instances=1,
                          coalesce=True)
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Monitorización strings FV", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    s = get_settings()
    if s.dashboard_password and request.url.path != "/healthz":
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
                ok = (secrets.compare_digest(user, s.dashboard_user) and
                      secrets.compare_digest(pwd, s.dashboard_password))
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Acceso restringido", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="FV"'})
    return await call_next(request)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:  # SQLite devuelve fechas sin zona
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    plant, settings = get_plant(), get_settings()
    with SessionLocal() as db:
        snap = db.scalars(select(Snapshot).order_by(Snapshot.ts.desc()).limit(1)).first()
        readings = []
        if snap:
            readings = db.scalars(select(StringReading)
                                  .where(StringReading.snapshot_id == snap.id)).all()
    strings = [{"string_id": r.string_id, "tracker": r.tracker, "voltage_v": r.voltage_v,
                "current_a": r.current_a, "power_w": r.power_w} for r in readings]
    result = analyze(strings, plant) if snap else analyze([], plant)
    return {
        "plant": {"name": plant.name, "string_wp": plant.string_wp,
                  "kwp": round(plant.string_wp * plant.total_strings / 1000, 2),
                  "strings": plant.total_strings},
        "simulated": settings.use_simulator,
        "snapshot": None if not snap else {
            "ts": _iso(snap.ts), "source": snap.source, "ac_power_w": snap.ac_power_w,
            "day_energy_kwh": snap.day_energy_kwh},
        **result,
    }


@app.get("/api/history")
def history(hours: float = Query(24, gt=0, le=24 * 31), tracker: str | None = None):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = select(StringReading).where(StringReading.ts >= since).order_by(StringReading.ts)
    if tracker:
        q = q.where(StringReading.tracker == tracker)
    series: dict[str, list] = {}
    with SessionLocal() as db:
        for r in db.scalars(q):
            series.setdefault(r.string_id, []).append(
                {"ts": _iso(r.ts), "i": r.current_a, "v": r.voltage_v, "p": r.power_w,
                 "dev": r.deviation_pct, "st": r.status})
    return {"hours": hours, "series": series}


@app.get("/api/daily")
def daily(days: int = Query(7, gt=0, le=90)):
    """Energía DC estimada por string y día (integrando la potencia)."""
    plant = get_plant()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    max_gap = timedelta(minutes=max(15, get_settings().poll_minutes * 3))
    energy: dict[str, dict[str, float]] = {}
    last: dict[str, tuple[datetime, float]] = {}
    with SessionLocal() as db:
        q = (select(StringReading.string_id, StringReading.ts, StringReading.power_w)
             .where(StringReading.ts >= since).order_by(StringReading.ts))
        for sid, ts, p in db.execute(q):
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            p = p or 0.0
            if sid in last:
                t0, p0 = last[sid]
                if ts - t0 <= max_gap:
                    day = ts.astimezone(LOCAL_TZ).date().isoformat()
                    kwh = (p0 + p) / 2 * (ts - t0).total_seconds() / 3600 / 1000
                    energy.setdefault(day, {}).setdefault(sid, 0.0)
                    energy[day][sid] += kwh
            last[sid] = (ts, p)
    kwp = {s.id: plant.string_wp * s.n_strings / 1000 for s in plant.strings}
    out = []
    for day in sorted(energy):
        vals = energy[day]
        out.append({"day": day, "kwh": {k: round(v, 2) for k, v in vals.items()},
                    "kwh_per_kwp": {k: round(v / kwp.get(k, 1), 2) for k, v in vals.items()}})
    return {"days": out, "strings": [s.id for s in plant.strings]}


@app.get("/api/raw")
def raw():
    """Última respuesta cruda de SolisCloud, para comprobar el mapeo de campos."""
    with SessionLocal() as db:
        snap = db.scalars(select(Snapshot).where(Snapshot.source == "solis")
                          .order_by(Snapshot.ts.desc()).limit(1)).first()
    if not snap:
        raise HTTPException(404, "Todavía no hay lecturas reales de SolisCloud")
    return JSONResponse({"ts": _iso(snap.ts), "data": snap.raw})


@app.post("/api/collect")
def collect():
    try:
        return {"snapshot_id": collect_once()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo leer el inversor: {e}") from e


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")
