"""Configuración: variables de entorno + plant.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class StringDef:
    """Un punto de medida: una entrada PV del inversor.

    Puede llevar uno o varios strings en paralelo (p. ej. S1+S2), y entonces su
    corriente es la suma. Para comparar se usa la corriente por string.
    """
    id: str            # p. ej. "114-S1+S2"
    tracker: str       # "114"
    mppt: int          # 1..10
    input: int         # entrada PV del inversor, 1..20
    names: list[str] = field(default_factory=lambda: ["S1"])

    @property
    def n_strings(self) -> int:
        return len(self.names)

    @property
    def label(self) -> str:
        return "+".join(self.names)


@dataclass
class TrackerDef:
    id: str
    mppt: int
    x: float
    y: float
    strings: list[StringDef] = field(default_factory=list)


@dataclass
class Plant:
    name: str
    panel_wp: float
    panels_per_string: int
    trackers: list[TrackerDef]
    analysis: dict
    voltage_by: str = "mppt"

    @property
    def strings(self) -> list[StringDef]:
        return [s for t in self.trackers for s in t.strings]

    @property
    def string_wp(self) -> float:
        return self.panel_wp * self.panels_per_string

    @property
    def total_strings(self) -> int:
        return sum(s.n_strings for s in self.strings)


def load_plant(path: str | Path | None = None) -> Plant:
    path = Path(path or os.getenv("PLANT_FILE", ROOT / "plant.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    trackers = []
    for t in raw["trackers"]:
        td = TrackerDef(id=str(t["id"]), mppt=int(t["mppt"]),
                        x=float(t.get("x", 0)), y=float(t.get("y", 0)))
        n = 0
        for inp in t["inputs"]:
            # admite "3" (un string) o {pv: 1, strings: [S1, S2]} (paralelo)
            if isinstance(inp, dict):
                pv = int(inp["pv"])
                names = [str(x) for x in inp.get("strings") or []]
                if not names:
                    names = [f"S{n + 1}"]
            else:
                pv, names = int(inp), [f"S{n + 1}"]
            n += len(names)
            td.strings.append(StringDef(id=f"{td.id}-{'+'.join(names)}", tracker=td.id,
                                        mppt=td.mppt, input=pv, names=names))
        trackers.append(td)
    p = raw.get("plant", {})
    analysis = {"min_median_current_a": 1.0, "warning_pct": 10, "alarm_pct": 25,
                "voltage_warning_pct": 5}
    analysis.update(raw.get("analysis") or {})
    return Plant(name=p.get("name", "Planta FV"), panel_wp=float(p.get("panel_wp", 210)),
                 panels_per_string=int(p.get("panels_per_string", 15)),
                 trackers=trackers, analysis=analysis,
                 voltage_by=str((raw.get("solis") or {}).get("voltage_by", "mppt")))


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass
class Settings:
    solis_key_id: str = ""
    solis_key_secret: str = ""
    solis_api_url: str = "https://www.soliscloud.com:13333"
    solis_inverter_sn: str = ""
    solis_inverter_id: str = ""
    database_url: str = ""
    poll_minutes: float = 5
    simulate: bool = False
    run_scheduler: bool = True
    dashboard_user: str = "rafael"
    dashboard_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.getenv
        return cls(
            solis_key_id=e("SOLIS_KEY_ID", "").strip(),
            solis_key_secret=e("SOLIS_KEY_SECRET", "").strip(),
            solis_api_url=(e("SOLIS_API_URL") or "https://www.soliscloud.com:13333").rstrip("/"),
            solis_inverter_sn=e("SOLIS_INVERTER_SN", "").strip(),
            solis_inverter_id=e("SOLIS_INVERTER_ID", "").strip(),
            database_url=e("DATABASE_URL", "").strip(),
            poll_minutes=float(e("POLL_MINUTES") or 5),
            simulate=_bool(e("SIMULATE", "false")),
            run_scheduler=_bool(e("RUN_SCHEDULER", "true")),
            dashboard_user=e("DASHBOARD_USER") or "rafael",
            dashboard_password=e("DASHBOARD_PASSWORD", ""),
        )

    @property
    def has_solis_credentials(self) -> bool:
        return bool(self.solis_key_id and self.solis_key_secret and
                    (self.solis_inverter_sn or self.solis_inverter_id))

    @property
    def use_simulator(self) -> bool:
        # Solo si se pide expresamente: así la web en Render (que no lleva las
        # claves de Solis) nunca mete datos falsos en la base de datos real.
        return self.simulate


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache
def get_plant() -> Plant:
    return load_plant()
