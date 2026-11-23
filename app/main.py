"""Web app de monitorización de strings (FastAPI)."""
from __future__ import annotations

import base64
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .analysis import analyze
from .auth import COOKIE, MAX_AGE, check_password, hash_password, make_token, read_token, users_exist
from .config import ROOT, get_plant, get_settings
from .db import Alert, AlertRule, DailyEnergy, InputDaily, SessionLocal, Snapshot, StringReading, init_db
from .solis import _scaled
from .poller import collect_once, safe_collect

logging.basicConfig(level=logging.INFO)
LOCAL_TZ = ZoneInfo("Europe/Madrid")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    if settings.simulate:
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
HEALTH_PATHS = {"/healthz", "/health", "/api/health"}  # sin contraseña, para el health check de Render


PUBLIC_PATHS = HEALTH_PATHS | {"/login", "/api/login", "/api/logout", "/manifest.webmanifest", "/api/cron"}


def _basic_user(request: Request, db):
    """Accés amb usuari i contrasenya a la capçalera (per a scripts)."""
    from .db import User
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        name, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    u = db.scalars(select(User).where(User.username == name.strip().lower())).first()
    return u if u and check_password(pwd, u.pw_hash) else None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    with SessionLocal() as db:
        if not users_exist(db):          # sense comptes (ús local): obert
            return await call_next(request)
        user = read_token(request.cookies.get(COOKIE), db) or _basic_user(request, db)
        if user:
            request.state.user = {"id": user.id, "username": user.username, "is_admin": user.is_admin}
    if not request.state.user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Cal iniciar sessió"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def _current(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def _require_admin(request: Request) -> None:
    u = _current(request)
    if u is not None and not u["is_admin"]:
        raise HTTPException(403, "Només l'administrador pot gestionar comptes")


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "static" / "login.html")


@app.post("/api/login")
def login(payload: dict = Body(...)):
    from .db import User
    name = str(payload.get("username", "")).strip().lower()
    pwd = str(payload.get("password", ""))
    with SessionLocal() as db:
        u = db.scalars(select(User).where(User.username == name)).first()
        if not u or not check_password(pwd, u.pw_hash):
            raise HTTPException(401, "Usuari o contrasenya incorrectes")
        resp = JSONResponse({"ok": True, "username": u.username})
        resp.set_cookie(COOKIE, make_token(u), max_age=MAX_AGE, httponly=True,
                        samesite="lax", secure=get_settings().cookie_secure)
        return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(request: Request):
    u = _current(request)
    return u or {"id": None, "username": None, "is_admin": True, "open": True}


def _valid_username(name: str) -> str:
    import re
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", name):
        raise HTTPException(400, "L'usuari ha de tenir 3-40 caràcters: lletres, números, punt, guió")
    return name


def _valid_password(pwd: str) -> str:
    if len(pwd) < 6:
        raise HTTPException(400, "La contrasenya ha de tenir almenys 6 caràcters")
    return pwd


def _user_json(u) -> dict:
    return {"id": u.id, "username": u.username, "is_admin": u.is_admin, "created": _iso(u.created)}


@app.get("/api/users")
def list_users(request: Request):
    from .db import User
    _require_admin(request)
    with SessionLocal() as db:
        return [_user_json(u) for u in db.scalars(select(User).order_by(User.username))]


@app.post("/api/users")
def create_user(request: Request, payload: dict = Body(...)):
    from .db import User
    _require_admin(request)
    name = _valid_username(str(payload.get("username", "")))
    pwd = _valid_password(str(payload.get("password", "")))
    with SessionLocal() as db:
        if db.scalars(select(User).where(User.username == name)).first():
            raise HTTPException(400, "Aquest usuari ja existeix")
        u = User(username=name, pw_hash=hash_password(pwd), is_admin=bool(payload.get("is_admin")),
                 created=datetime.now(timezone.utc))
        db.add(u)
        db.commit()
        return _user_json(u)


@app.put("/api/users/{user_id}")
def update_user(user_id: int, request: Request, payload: dict = Body(...)):
    """L'administrador pot canviar-ho tot; cada usuari, la seva contrasenya."""
    from .db import User
    me_ = _current(request)
    if me_ is not None and not me_["is_admin"] and me_["id"] != user_id:
        raise HTTPException(403, "Només pots canviar la teva contrasenya")
    with SessionLocal() as db:
        u = db.get(User, user_id)
        if not u:
            raise HTTPException(404, "No existeix")
        if payload.get("password"):
            u.pw_hash = hash_password(_valid_password(str(payload["password"])))
        if "is_admin" in payload and (me_ is None or me_["is_admin"]):
            if not payload["is_admin"] and u.is_admin and _admin_count(db) <= 1:
                raise HTTPException(400, "Ha de quedar almenys un administrador")
            u.is_admin = bool(payload["is_admin"])
        db.commit()
        resp = JSONResponse(_user_json(u))
        if me_ and me_["id"] == u.id:   # renova la galeta si canvia la pròpia contrasenya
            resp.set_cookie(COOKIE, make_token(u), max_age=MAX_AGE, httponly=True,
                            samesite="lax", secure=get_settings().cookie_secure)
        return resp


def _admin_count(db) -> int:
    from sqlalchemy import func
    from .db import User
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, request: Request):
    from .db import User
    _require_admin(request)
    me_ = _current(request)
    with SessionLocal() as db:
        u = db.get(User, user_id)
        if not u:
            raise HTTPException(404, "No existeix")
        if me_ and me_["id"] == u.id:
            raise HTTPException(400, "No pots esborrar el teu propi compte")
        if u.is_admin and _admin_count(db) <= 1:
            raise HTTPException(400, "Ha de quedar almenys un administrador")
        db.delete(u)
        db.commit()
        return {"ok": True}


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:  # SQLite devuelve fechas sin zona
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


@app.get("/healthz")
@app.get("/api/health")
@app.get("/health")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    plant = get_plant()
    with SessionLocal() as db:
        snap = db.scalars(select(Snapshot).order_by(Snapshot.ts.desc()).limit(1)).first()
        readings = []
        if snap:
            readings = db.scalars(select(StringReading)
                                  .where(StringReading.snapshot_id == snap.id)).all()
    energy = {"today_kwh": snap.day_energy_kwh if snap else None,
              "month_kwh": None, "year_kwh": None, "total_kwh": None}
    raw = snap.raw if snap and snap.raw else None
    if raw:
        energy["month_kwh"] = _scaled(raw.get("eMonth"), raw.get("eMonthStr", "kWh"), "kWh")
        energy["year_kwh"] = _scaled(raw.get("eYear"), raw.get("eYearStr", "kWh"), "kWh")
        energy["total_kwh"] = _scaled(raw.get("eTotal"), raw.get("eTotalStr", "kWh"), "kWh")
    elif snap:  # simulado: se suman los días guardados
        today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date().isoformat()
        with SessionLocal() as db:
            rows = db.scalars(select(DailyEnergy)).all()
        energy["today_kwh"] = next((r.kwh for r in rows if r.day == today), None)
        energy["month_kwh"] = round(sum(r.kwh for r in rows if r.day[:7] == today[:7]), 1)
        energy["year_kwh"] = round(sum(r.kwh for r in rows if r.day[:4] == today[:4]), 1)
    strings = [{"string_id": r.string_id, "tracker": r.tracker, "voltage_v": r.voltage_v,
                "current_a": r.current_a, "power_w": r.power_w} for r in readings]
    result = analyze(strings, plant) if snap else analyze([], plant)
    return {
        "plant": {"name": plant.name, "string_wp": plant.string_wp,
                  "kwp": round(plant.string_wp * plant.total_strings / 1000, 2),
                  "strings": plant.total_strings},
        "simulated": bool(snap and snap.source == "sim"),
        "snapshot": None if not snap else {
            "ts": _iso(snap.ts), "source": snap.source, "ac_power_w": snap.ac_power_w,
            "day_energy_kwh": snap.day_energy_kwh},
        "energy": energy,
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


@app.get("/api/power")
def power_curve(date: str | None = None):
    """Corba de potència d'un dia (hora local): AC de l'inversor i DC sumada."""
    from sqlalchemy import func
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date()
    try:
        day = datetime.strptime((date or today.isoformat())[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "Data incorrecta")
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with SessionLocal() as db:
        snaps = db.execute(select(Snapshot.id, Snapshot.ts, Snapshot.ac_power_w)
                           .where(Snapshot.ts >= start, Snapshot.ts < end)
                           .order_by(Snapshot.ts)).all()
        dc = dict(db.execute(select(StringReading.snapshot_id, func.sum(StringReading.power_w))
                             .where(StringReading.ts >= start, StringReading.ts < end)
                             .group_by(StringReading.snapshot_id)).all())
        de = db.get(DailyEnergy, day.isoformat())
    pts = [{"ts": _iso(ts), "ac_kw": None if ac is None else round(ac / 1000, 2),
            "dc_kw": None if dc.get(i) is None else round(dc[i] / 1000, 2)}
           for i, ts, ac in snaps]
    return {"date": day.isoformat(), "start": _iso(start), "end": _iso(end),
            "day_kwh": round(de.kwh, 1) if de else None, "points": pts}


@app.get("/api/production")
def production(days: int = Query(30, gt=0, le=400)):
    """Producción diaria total del inversor (kWh), últimos N días."""
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    with SessionLocal() as db:
        rows = db.scalars(select(DailyEnergy).where(DailyEnergy.day >= since)
                          .order_by(DailyEnergy.day)).all()
    return {"days": [{"day": r.day, "kwh": round(r.kwh, 1)} for r in rows]}


@app.get("/api/trackers/energy")
def trackers_energy(period: str = Query("day", pattern="^(day|month|year)$"),
                    date: str | None = None):
    """kWh DC por seguidor y por entrada en un día, mes o año (hora local)."""
    plant = get_plant()
    today = datetime.now(timezone.utc).astimezone(LOCAL_TZ).date().isoformat()
    date = (date or today)[:10]
    key = {"day": date, "month": date[:7], "year": date[:4]}[period]
    with SessionLocal() as db:
        q = select(InputDaily)
        q = q.where(InputDaily.day == key) if period == "day" else \
            q.where(InputDaily.day.like(key + "%"))
        rows = db.scalars(q).all()
        ac_q = select(DailyEnergy)
        ac_q = ac_q.where(DailyEnergy.day == key) if period == "day" else \
            ac_q.where(DailyEnergy.day.like(key + "%"))
        ac = sum(r.kwh for r in db.scalars(ac_q))
    by_input: dict[str, float] = {}
    for r in rows:
        by_input[r.string_id] = by_input.get(r.string_id, 0.0) + r.kwh
    trackers = []
    for t in plant.trackers:
        inputs = [{"string_id": s.id, "label": s.label, "n_strings": s.n_strings,
                   "kwh": round(by_input.get(s.id, 0.0), 2)} for s in t.strings]
        kwh = sum(i["kwh"] for i in inputs)
        kwp = plant.string_wp * sum(s.n_strings for s in t.strings) / 1000
        trackers.append({"id": t.id, "kwh": round(kwh, 2), "kwp": kwp,
                         "kwh_per_kwp": round(kwh / kwp, 3) if kwp else None,
                         "inputs": inputs})
    return {"period": period, "key": key, "has_data": bool(rows),
            "inverter_ac_kwh": round(ac, 1) if ac else None, "trackers": trackers}


def _alert_json(a: Alert) -> dict:
    return {"id": a.id, "created": _iso(a.created), "last_seen": _iso(a.last_seen),
            "source": a.source, "kind": a.kind, "severity": a.severity, "tracker": a.tracker,
            "string_id": a.string_id, "title": a.title, "notes": a.notes, "count": a.count,
            "state": a.state, "closed": _iso(a.closed) if a.closed else None}


@app.get("/api/alerts")
def list_alerts(state: str = Query("open", pattern="^(open|closed|all)$"),
                limit: int = Query(200, gt=0, le=1000)):
    q = select(Alert).order_by(Alert.last_seen.desc()).limit(limit)
    if state != "all":
        q = q.where(Alert.state == state)
    with SessionLocal() as db:
        return {"alerts": [_alert_json(a) for a in db.scalars(q)]}


@app.post("/api/alerts")
def create_alert(payload: dict = Body(...)):
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Cal un títol")
    severity = payload.get("severity") if payload.get("severity") in ("warning", "alarm") \
        else "warning"
    tracker = str(payload.get("tracker") or "").strip() or None
    if tracker and tracker not in {t.id for t in get_plant().trackers}:
        raise HTTPException(422, "Seguidor desconegut")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        a = Alert(created=now, last_seen=now, source="manual", kind="manual", severity=severity,
                  tracker=tracker, string_id=(str(payload.get("string_id") or "").strip() or None),
                  title=title[:200], notes=(str(payload.get("notes") or "").strip() or None),
                  count=1, state="open")
        db.add(a)
        db.commit()
        return _alert_json(a)


@app.post("/api/alerts/{alert_id}/{action}")
def change_alert(alert_id: int, action: str, payload: dict | None = Body(None)):
    if action not in ("close", "reopen"):
        raise HTTPException(404)
    with SessionLocal() as db:
        a = db.get(Alert, alert_id)
        if not a:
            raise HTTPException(404, "No existeix")
        a.state = "closed" if action == "close" else "open"
        a.closed = datetime.now(timezone.utc) if action == "close" else None
        note = str((payload or {}).get("notes") or "").strip()
        if note:
            a.notes = (a.notes + "\n" if a.notes else "") + note
        db.commit()
        return _alert_json(a)


def _rule_json(r: AlertRule) -> dict:
    return {"id": r.id, "name": r.name, "rtype": r.rtype, "threshold": r.threshold,
            "min_readings": r.min_readings, "tracker": r.tracker, "severity": r.severity,
            "enabled": r.enabled}


def _rule_from_payload(r: AlertRule, p: dict) -> None:
    from .rules import RULE_TYPES
    if "rtype" in p:
        if p["rtype"] not in RULE_TYPES:
            raise HTTPException(422, "Tipus de regla desconegut")
        r.rtype = p["rtype"]
    if "name" in p:
        r.name = (str(p["name"] or "").strip() or RULE_TYPES[r.rtype]["name"])[:120]
    if "threshold" in p:
        try:
            r.threshold = None if p["threshold"] in (None, "") else float(p["threshold"])
        except (TypeError, ValueError):
            raise HTTPException(422, "Llindar incorrecte")
    if "min_readings" in p:
        try:
            r.min_readings = max(1, min(50, int(p["min_readings"])))
        except (TypeError, ValueError):
            raise HTTPException(422, "Lectures incorrectes")
    if "tracker" in p:
        t = str(p["tracker"] or "").strip() or None
        if t and t not in {x.id for x in get_plant().trackers}:
            raise HTTPException(422, "Seguidor desconegut")
        r.tracker = t
    if "severity" in p:
        r.severity = p["severity"] if p["severity"] in ("warning", "alarm") else "warning"
    if "enabled" in p:
        r.enabled = bool(p["enabled"])


@app.get("/api/rules")
def list_rules():
    from .rules import RULE_TYPES
    with SessionLocal() as db:
        rules = [_rule_json(r) for r in db.scalars(select(AlertRule).order_by(AlertRule.id))]
    return {"rules": rules, "types": RULE_TYPES}


@app.post("/api/rules")
def create_rule(payload: dict = Body(...)):
    from .rules import RULE_TYPES
    if payload.get("rtype") not in RULE_TYPES:
        raise HTTPException(422, "Tipus de regla desconegut")
    r = AlertRule(rtype=payload["rtype"], name="", min_readings=3, severity="warning",
                  enabled=True, threshold=RULE_TYPES[payload["rtype"]]["default"])
    _rule_from_payload(r, {"name": "", **payload})
    with SessionLocal() as db:
        db.add(r)
        db.commit()
        return _rule_json(r)


@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, payload: dict = Body(...)):
    with SessionLocal() as db:
        r = db.get(AlertRule, rule_id)
        if not r:
            raise HTTPException(404, "No existeix")
        _rule_from_payload(r, payload)
        db.commit()
        return _rule_json(r)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    with SessionLocal() as db:
        r = db.get(AlertRule, rule_id)
        if not r:
            raise HTTPException(404, "No existeix")
        db.delete(r)
        db.commit()
        return {"ok": True}


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
        return {"snapshot_id": collect_once(keep_night=True)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo leer el inversor: {e}") from e


@app.api_route("/api/cron", methods=["GET", "POST"])
def cron(key: str = ""):
    """Per a un cron extern (p. ex. cron-job.org) cada 10 min.

    Llegeix l'inversor i guarda, i de pas manté despert el servidor de Render.
    Protegit amb CRON_KEY (paràmetre ?key=).
    """
    import os
    expected = os.getenv("CRON_KEY", "")
    if not expected or not hmac.compare_digest(key, expected):
        raise HTTPException(403, "Clau incorrecta")
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
