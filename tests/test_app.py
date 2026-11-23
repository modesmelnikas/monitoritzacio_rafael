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
    assert "paralelo" in s["120-S1+S2"]["message"]
    assert r["status"] == "alarm"


def test_analysis_tracker_all_low_suggests_tracking_problem():
    cur = [7.0] * 20
    cur[8] = cur[9] = 5.0   # seguidor 121, las dos entradas
    r = analyze(_readings(cur), plant)
    t = next(t for t in r["trackers"] if t["id"] == "121")
    assert any("seguimiento" in m for m in t["messages"])


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


def test_poller_real_mode_skips_night_and_keeps_only_last_raw(monkeypatch):
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

    monkeypatch.setattr(poller, "fetch_inverter_detail", lambda _s: detail(0))
    assert poller.collect_once() is None                       # de noche no guarda
    monkeypatch.setattr(poller, "fetch_inverter_detail", lambda _s: detail(1))
    a = poller.collect_once()
    b = poller.collect_once()
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


def test_health_is_public_when_password_set(monkeypatch):
    from app.config import Settings
    import app.main as main
    monkeypatch.setattr(main, "get_settings", lambda: Settings(dashboard_password="x"))
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/api/health").status_code == 200
        assert c.get("/health").status_code == 200
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", auth=("rafael", "x")).status_code == 200
