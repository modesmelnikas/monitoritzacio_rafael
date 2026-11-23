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


def test_plant_has_10_trackers_30_strings():
    assert [t.id for t in plant.trackers] == ["114", "117", "119", "120", "121", "122",
                                              "123", "124", "125", "126"]
    assert len(plant.strings) == 30
    assert plant.string_wp == 2100


def test_signature_matches_spec():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    h = sign_headers("KEY", "SECRET", "/v1/api/inverterDetail", '{"sn":"X"}', now)
    md5 = base64.b64encode(hashlib.md5(b'{"sn":"X"}').digest()).decode()
    assert h["Content-MD5"] == md5
    assert h["Date"] == "Mon, 23 Nov 2026 12:00:00 GMT"
    to_sign = f"POST\n{md5}\napplication/json;charset=UTF-8\n{h['Date']}\n/v1/api/inverterDetail"
    sig = base64.b64encode(hmac.new(b"SECRET", to_sign.encode(), hashlib.sha1).digest()).decode()
    assert h["Authorization"] == f"API KEY:{sig}"


def test_parse_detail_maps_mppt_voltage_and_input_current():
    detail = {"pac": 55.2, "pacStr": "kW", "eToday": 210, "eTodayStr": "kWh"}
    for m in range(1, 11):
        detail[f"uPv{m}"] = 500 + m
    for i in range(1, 31):
        detail[f"iPv{i}"] = 7.0
    out = parse_detail(detail, plant)
    assert out["ac_power_w"] == 55200
    s = {x["string_id"]: x for x in out["strings"]}
    assert s["117-A"]["voltage_v"] == 502 and s["117-C"]["voltage_v"] == 502
    assert s["126-C"]["current_a"] == 7.0


def _strings(currents, v=500):
    return [{"string_id": s.id, "tracker": s.tracker, "voltage_v": v,
             "current_a": c, "power_w": v * c} for s, c in zip(plant.strings, currents)]


def test_analysis_flags_low_and_dead_strings():
    cur = [7.0] * 30
    cur[1] = 6.0    # 114-B  -14 % -> aviso
    cur[6] = 0.0    # 119-A  sin corriente -> alarma
    r = analyze(_strings(cur), plant)
    s = {x["string_id"]: x for t in r["trackers"] for x in t["strings"]}
    assert s["114-A"]["status"] == "ok"
    assert s["114-B"]["status"] == "warning"
    assert s["119-A"]["status"] == "alarm"
    assert r["status"] == "alarm"


def test_analysis_tracker_both_low_suggests_tracking_problem():
    cur = [7.0] * 30
    cur[9] = cur[10] = cur[11] = 4.0   # seguidor 120
    r = analyze(_strings(cur), plant)
    t = \
        next(t for t in r["trackers"] if t["id"] == "120")
    assert any("seguimiento" in m for m in t["messages"])


def test_analysis_idle_at_night():
    r = analyze(_strings([0.05] * 30), plant)
    assert not r["active"]
    assert all(x["status"] == "idle" for t in r["trackers"] for x in t["strings"])


def test_simulator_noon_has_faults():
    noon = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)  # 13:00 en Madrid
    r = analyze(simulate(plant, noon, seed=1)["strings"], plant)
    s = {x["string_id"]: x["status"] for t in r["trackers"] for x in t["strings"]}
    assert s["124-A"] == "alarm" and s["121-B"] == "alarm"
    assert s["114-A"] == "ok"


def test_api_end_to_end():
    from app.main import app
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        assert st["simulated"] is True
        assert len(st["trackers"]) == 10
        assert st["snapshot"] is not None
        assert c.get("/api/history?hours=24&tracker=114").json()["series"]
        d = c.get("/api/daily?days=3").json()
        assert d["days"]
        assert c.post("/api/collect").status_code == 200
        assert c.get("/").status_code == 200
