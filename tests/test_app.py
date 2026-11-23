import base64
import hashlib
import hmac
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.analysis import analyze
from app.config import get_plant
from app.simulator import simulate
from app.solis import parse_detail, sign_headers

plant = get_plant()


def test_plant_wiring():
    assert [t.id for t in plant.trackers] == ["114", "117", "119", "120", "121", "122",
                                              "123", "124", "125", "126"]
    assert len(plant.strings) == 20          # 20 entradas del inversor
    assert plant.total_strings == 30         # 30 strings de 10 paneles
    assert plant.string_wp == 2100
    t = plant.trackers[0]
    assert [(s.id, s.input, s.n_strings) for s in t.strings] == [
        ("114-S1+S2", 1, 2), ("114-S3", 2, 1)]
    t = plant.trackers[-1]
    assert [s.input for s in t.strings] == [19, 20] and t.mppt == 10


def test_signature_matches_spec():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    h = sign_headers("KEY", "SECRET", "/v1/api/inverterDetail", '{"sn":"X"}', now)
    md5 = base64.b64encode(hashlib.md5(b'{"sn":"X"}').digest()).decode()
    assert h["Content-MD5"] == md5
    assert h["Date"] == "Mon, 23 Nov 2026 12:00:00 GMT"
    to_sign = f"POST\n{md5}\napplication/json\n{h['Date']}\n/v1/api/inverterDetail"
    sig = base64.b64encode(hmac.new(b"SECRET", to_sign.encode(), hashlib.sha1).digest()).decode()
    assert h["Authorization"] == f"API KEY:{sig}"


# Respuesta real de SolisCloud (Solis-100K-5G, 23/11/2026 15:37), campos usados
REAL = {"pac": 20.24, "pacStr": "kW", "eToday": 156.5, "eTodayStr": "kWh",
        "uPv1": 388.6, "iPv1": 3.7, "uPv2": 388.6, "iPv2": 1.4, "uPv3": 356.1, "iPv3": 4,
        "uPv4": 356.1, "iPv4": 1.9, "pow1": 1438, "pow2": 544, "pow3": 1424, "pow4": 677,
        "uPv19": 388.9, "iPv19": 4.3, "uPv20": 388.9, "iPv20": 1.8, "pow19": 1672, "pow20": 700}


def test_parse_detail_real_response():
    out = parse_detail(REAL, plant)
    assert out["ac_power_w"] == 20240
    assert out["day_energy_kwh"] == 156.5
    s = {x["string_id"]: x for x in out["strings"]}
    # 117 = MPPT 2 = entradas 3 y 4: su tensión es uPv3/uPv4, no uPv2
    assert s["117-S1+S2"]["voltage_v"] == 356.1 and s["117-S3"]["voltage_v"] == 356.1
    assert s["117-S1+S2"]["current_a"] == 4 and s["117-S3"]["power_w"] == 677
    assert s["126-S1+S2"]["current_a"] == 4.3 and s["126-S3"]["voltage_v"] == 388.9


def _readings(per_string, v=340):
    """per_string: corriente por string de cada entrada (se multiplica por n)."""
    return [{"string_id": s.id, "tracker": s.tracker, "voltage_v": v,
             "current_a": c * s.n_strings, "power_w": v * c * s.n_strings}
            for s, c in zip(plant.strings, per_string)]


def test_parallel_input_is_normalised():
    r = analyze(_readings([7.0] * 20), plant)
    assert r["status"] == "ok"
    s = {x["string_id"]: x for t in r["trackers"] for x in t["strings"]}
    assert s["114-S1+S2"]["current_a"] == 14.0
    assert s["114-S1+S2"]["current_per_string_a"] == 7.0
    assert s["114-S1+S2"]["deviation_pct"] == 0


def test_analysis_flags_low_dead_and_half_parallel():
    cur = [7.0] * 20
    cur[1] = 6.0    # 114-S3      -14 % -> aviso
    cur[5] = 0.0    # 119-S3      sin corriente -> alarma
    cur[6] = 3.5    # 120-S1+S2   mitad -> un string del paralelo cortado
    r = analyze(_readings(cur), plant)
    s = {x["string_id"]: x for t in r["trackers"] for x in t["strings"]}
    assert s["114-S1+S2"]["status"] == "ok"
    assert s["114-S3"]["status"] == "warning"
    assert s["119-S3"]["status"] == "alarm"
    assert s["120-S1+S2"]["status"] == "alarm"
    assert "paral·lel" in s["120-S1+S2"]["message"]
    assert r["status"] == "alarm"


def test_analysis_tracker_all_low_suggests_tracking_problem():
    cur = [7.0] * 20
    cur[8] = cur[9] = 5.0   # seguidor 121, las dos entradas
    r = analyze(_readings(cur), plant)
    t = next(t for t in r["trackers"] if t["id"] == "121")
    assert any("seguiment" in m for m in t["messages"])


def test_analysis_idle_at_night():
    r = analyze(_readings([0.05] * 20), plant)
    assert not r["active"]
    assert all(x["status"] == "idle" for t in r["trackers"] for x in t["strings"])


def test_simulator_noon_has_faults():
    noon = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)  # 13:00 en Madrid
    r = analyze(simulate(plant, noon, seed=1)["strings"], plant)
    s = {x["string_id"]: x["status"] for t in r["trackers"] for x in t["strings"]}
    assert s["124-S1+S2"] == "alarm" and s["121-S3"] == "alarm"
    assert s["114-S1+S2"] == "ok" and s["114-S3"] == "ok"


def test_api_end_to_end():
    from app.main import app
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        assert st["simulated"] is True
        assert st["plant"]["strings"] == 30 and st["plant"]["kwp"] == 63.0
        assert len(st["trackers"]) == 10
        assert st["snapshot"] is not None
        assert c.get("/api/history?hours=24&tracker=114").json()["series"]
        d = c.get("/api/daily?days=3").json()
        assert d["days"]
        assert c.post("/api/collect").status_code == 200
        assert c.get("/").status_code == 200


def test_poller_real_mode_keeps_night_and_only_last_raw(monkeypatch):
    import app.poller as poller
    from app.config import Settings
    from app.db import SessionLocal, Snapshot, init_db
    from sqlalchemy import select

    init_db()
    s = Settings(solis_key_id="k", solis_key_secret="s", solis_inverter_sn="SN", simulate=False)
    monkeypatch.setattr(poller, "get_settings", lambda: s)

    def detail(i):
        d = {"pac": 50 if i else 0, "pacStr": "kW"}
        d.update({f"uPv{m}": 340 for m in range(1, 21)})
        d.update({f"iPv{k}": (14 if k % 2 else 7) * i for k in range(1, 21)})
        return d

    from datetime import timedelta
    t0 = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(poller, "fetch_inverter_detail", lambda _s: detail(0))
    assert poller.collect_once(t0) is not None                 # de nit també es guarda
    monkeypatch.setattr(poller, "fetch_inverter_detail", lambda _s: detail(1))
    a = poller.collect_once(t0 + timedelta(minutes=10))
    assert poller.collect_once(t0 + timedelta(minutes=12)) is None  # duplicada (<4 min)
    b = poller.collect_once(t0 + timedelta(minutes=20))
    with SessionLocal() as db:
        assert db.get(Snapshot, a).raw is None                  # la cruda vieja se borra
        assert db.get(Snapshot, b).raw["pac"] == 50
        assert db.get(Snapshot, b).source == "solis"


def test_no_simulation_without_flag(monkeypatch):
    import pytest
    import app.poller as poller
    from app.config import Settings
    monkeypatch.setattr(poller, "get_settings", lambda: Settings(simulate=False))
    with pytest.raises(RuntimeError):
        poller.collect_once()


def test_parallel_and_single_inputs_compared_within_their_group():
    # S3 da sistemáticamente un 25 % más por string que S1+S2: no es avería
    cur = [2.0 if s.n_strings == 2 else 2.5 for s in plant.strings]
    cur[1] = 1.6    # 114-S3: -36 % frente a las otras S3
    r = analyze(_readings(cur), plant)
    s = {x["string_id"]: x for t in r["trackers"] for x in t["strings"]}
    assert all(s[k]["status"] == "ok" for k in s if k.endswith("S1+S2"))
    assert s["114-S3"]["status"] == "alarm"
    assert s["117-S3"]["status"] == "ok"


def test_login_and_accounts(monkeypatch):
    from app.config import Settings
    import app.auth as auth
    import app.main as main
    from app.db import SessionLocal, User
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(dashboard_password="secret1"))
    monkeypatch.setenv("CRON_KEY", "k123")
    try:
        with TestClient(main.app) as c:
            assert c.get("/healthz").status_code == 200
            assert c.get("/login").status_code == 200
            assert c.get("/api/status").status_code == 401
            assert c.get("/", follow_redirects=False).headers["location"] == "/login"
            assert c.get("/api/cron?key=mal").status_code == 403
            assert c.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 401
            # l'admin es crea amb la contrasenya de DASHBOARD_PASSWORD
            assert c.post("/api/login", json={"username": "Admin", "password": "secret1"}).status_code == 200
            assert c.get("/api/status").status_code == 200
            assert c.get("/api/me").json()["is_admin"] is True
            r = c.post("/api/users", json={"username": "rafael", "password": "abcdef"})
            assert r.status_code == 200
            rid = r.json()["id"]
            assert c.post("/api/users", json={"username": "rafael", "password": "abcdef"}).status_code == 400
            me_id = c.get("/api/me").json()["id"]
            assert c.delete(f"/api/users/{me_id}").status_code == 400   # no t'esborres a tu mateix
            c.post("/api/logout")
            assert c.get("/api/status").status_code == 401
            # un usuari normal entra però no gestiona comptes
            assert c.post("/api/login", json={"username": "rafael", "password": "abcdef"}).status_code == 200
            assert c.get("/api/status").status_code == 200
            assert c.get("/api/users").status_code == 403
            assert c.put(f"/api/users/{me_id}", json={"password": "xxxxxx"}).status_code == 403
            assert c.put(f"/api/users/{rid}", json={"password": "nova123"}).status_code == 200
            assert c.get("/api/status").status_code == 200      # la galeta es renova
            # accés per a scripts amb usuari i contrasenya
            c.cookies.clear()
            assert c.get("/api/status", auth=("rafael", "nova123")).status_code == 200
    finally:
        with SessionLocal() as db:
            db.query(User).delete()
            db.commit()


def test_alert_register_auto_and_manual():
    from app.main import app
    with TestClient(app) as c:
        auto = c.get("/api/alerts").json()["alerts"]
        assert any(a["source"] == "auto" and a["string_id"] == "121-S3" for a in auto)
        # una sola alarma oberta per problema, encara que es repeteixi
        assert len([a for a in auto if a["string_id"] == "121-S3"]) == 1
        r = c.post("/api/alerts", json={"title": "Panell trencat", "tracker": "120",
                                        "severity": "alarm", "notes": "Fila 3"})
        assert r.status_code == 200 and r.json()["source"] == "manual"
        aid = r.json()["id"]
        assert c.post("/api/alerts", json={"title": ""}).status_code == 422
        assert c.post(f"/api/alerts/{aid}/close", json={"notes": "Canviat"}).json()["state"] == "closed"
        assert all(a["id"] != aid for a in c.get("/api/alerts").json()["alerts"])
        assert any(a["id"] == aid for a in c.get("/api/alerts?state=closed").json()["alerts"])


def test_auto_alert_needs_three_readings():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db import Alert, SessionLocal, init_db
    from app.poller import sync_alerts
    init_db()
    cur = [7.0] * 20
    cur[3] = 3.0                       # 117-S3
    bad = analyze(_readings(cur), plant)
    good = analyze(_readings([7.0] * 20), plant)
    t0 = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)

    def state():
        with SessionLocal() as db:
            a = db.scalars(select(Alert).where(Alert.string_id == "117-S3",
                                               Alert.created >= t0)).first()
            return a and a.state

    with SessionLocal() as db:
        sync_alerts(db, t0, bad); db.commit()
    assert state() == "pending"
    with SessionLocal() as db:
        sync_alerts(db, t0 + timedelta(minutes=10), good); db.commit()
    assert state() is None             # ha desaparegut: no es registra
    for k in range(3):
        with SessionLocal() as db:
            sync_alerts(db, t0 + timedelta(minutes=20 + 10 * k), bad); db.commit()
    assert state() == "open"


def test_rules_crud_and_evaluation():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.main import app
    from app.rules import evaluate
    with TestClient(app) as c:
        d = c.get("/api/rules").json()
        assert {r["rtype"] for r in d["rules"]} >= {"string_dev", "string_zero", "tracker_dev"}
        assert "string_spread" in d["types"]
        r = c.post("/api/rules", json={"rtype": "string_spread", "threshold": 25,
                                       "tracker": "124", "severity": "alarm"}).json()
        assert r["name"] == "Diferència dins d'un seguidor" and r["tracker"] == "124"
        assert c.put(f"/api/rules/{r['id']}", json={"enabled": False}).json()["enabled"] is False
        assert c.post("/api/rules", json={"rtype": "nope"}).status_code == 422
        assert c.delete(f"/api/rules/{r['id']}").json()["ok"]

    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    cur = [7.0] * 20
    cur[8] = cur[9] = 5.0                      # seguidor 121 un 29 % menys
    res = analyze(_readings(cur), plant)

    def rule(rtype, thr, tracker=None):
        return SimpleNamespace(rtype=rtype, threshold=thr, tracker=tracker, enabled=True)

    hits = evaluate(rule("tracker_dev", 15), res, {}, None, now, plant)
    assert [h["tracker"] for h in hits] == ["121"]
    assert evaluate(rule("tracker_dev", 15, "114"), res, {}, None, now, plant) == []
    assert evaluate(rule("ac_low", 5), res, {"ac_power_w": 3000}, None, now, plant)
    old = {"dataTimestamp": str((now.timestamp() - 3600) * 1000)}
    assert evaluate(rule("stale_data", 30), res, {}, old, now, plant)


def test_telegram_notify(monkeypatch):
    import httpx
    from app import notify
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.send("x") == 0
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "11, 22")
    calls = []

    def handler(req):
        calls.append((str(req.url), req.content))
        return httpx.Response(200, json={"ok": True})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.send(notify.alert_text("PS", "alarm", "114-S3 baix"), client) == 2
    assert calls[0][0] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert "114-S3 baix" in calls[0][1].decode()


def test_backfill_day_fills_gaps(monkeypatch):
    from datetime import timedelta
    import app.poller as poller
    import app.solis as solis
    from app.config import Settings
    from app.db import InputDaily, SessionLocal, Snapshot
    from sqlalchemy import func, select
    from zoneinfo import ZoneInfo
    s = Settings(solis_key_id="k", solis_key_secret="s", solis_inverter_sn="SN")
    monkeypatch.setattr(poller, "get_settings", lambda: s)
    day = "2030-06-01"
    t0 = datetime(2030, 6, 1, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    pts = []
    for k in range(6):
        p = dict(REAL, pac=6.5)  # el fixture només té 6 entrades (~6,5 kW DC)
        p["dataTimestamp"] = str(int((t0 + timedelta(minutes=5 * k)).timestamp() * 1000))
        pts.append(p)
    night = dict(REAL, pac=0, dataTimestamp=str(int((t0 - timedelta(hours=5)).timestamp() * 1000)))
    night.update({f"iPv{i}": 0 for i in range(1, 21)})
    pts.append(night)
    monkeypatch.setattr(solis, "fetch_inverter_day", lambda _s, d: pts)
    assert poller.backfill_day(day) == 7          # també el punt de nit
    assert poller.backfill_day(day) == 0          # no duplica
    with SessionLocal() as db:
        assert db.scalar(select(func.sum(InputDaily.kwh)).where(InputDaily.day == day)) > 0
        assert db.scalar(select(func.count(Snapshot.id)).where(Snapshot.ts >= t0.astimezone(timezone.utc))) >= 6


def test_no_new_alerts_outside_alarm_hours():
    from sqlalchemy import func, select
    from app.db import Alert, SessionLocal, init_db
    from app.poller import sync_alerts
    init_db()
    cur = [7.0] * 20
    cur[5] = 0.0
    bad = analyze(_readings(cur), plant)
    late = datetime(2031, 1, 1, 17, 30, tzinfo=timezone.utc)   # 18:30 a Madrid
    with SessionLocal() as db:
        before = db.scalar(select(func.count(Alert.id)))
        for k in range(4):
            sync_alerts(db, late, bad)
        db.commit()
        assert db.scalar(select(func.count(Alert.id))) == before


def test_energy_month_and_year():
    from app.main import app
    from app.poller import save_daily
    save_daily({"2029-03-01": 100.0, "2029-03-15": 50.5, "2029-07-02": 300.0})
    with TestClient(app) as c:
        m = c.get("/api/energy?period=month&date=2029-03-10").json()
        assert len(m["bars"]) == 31 and m["bars"][0]["kwh"] == 100.0 and m["total_kwh"] == 150.5
        y = c.get("/api/energy?period=year&date=2029").json()
        assert len(y["bars"]) == 12 and y["bars"][2]["kwh"] == 150.5 and y["bars"][6]["kwh"] == 300.0
