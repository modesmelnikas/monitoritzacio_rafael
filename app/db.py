"""Base de datos: SQLite en local, Postgres en Render (DATABASE_URL)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, DateTime, Float, Integer, String, Text, create_engine,
                        Index)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import ROOT, get_settings


class Base(DeclarativeBase):
    pass


class Snapshot(Base):
    """Una lectura del inversor (cada POLL_MINUTES)."""
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(16))          # solis | sim
    ac_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    day_energy_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class StringReading(Base):
    __tablename__ = "string_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    string_id: Mapped[str] = mapped_column(String(16))
    tracker: Mapped[str] = mapped_column(String(16))
    voltage_v: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    deviation_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")

    __table_args__ = (Index("ix_string_ts", "string_id", "ts"),)


class DailyEnergy(Base):
    """Producción diaria del inversor (kWh), de SolisCloud (inverterMonth / eToday)."""
    __tablename__ = "daily_energy"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)   # "2026-11-23" hora local
    kwh: Mapped[float] = mapped_column(Float)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InputDaily(Base):
    """Energía DC por entrada y día (kWh), integrando la potencia de cada lectura."""
    __tablename__ = "input_daily"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    string_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    tracker: Mapped[str] = mapped_column(String(16), index=True)
    kwh: Mapped[float] = mapped_column(Float, default=0.0)


class Alert(Base):
    """Registre d'alarmes: automàtiques (de l'anàlisi) o creades a mà."""
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(8))            # auto | manual
    kind: Mapped[str] = mapped_column(String(32))             # current | voltage | tracking | manual
    severity: Mapped[str] = mapped_column(String(8))          # warning | alarm
    tracker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    string_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open | closed
    closed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _url() -> str:
    url = get_settings().database_url
    if not url:
        return f"sqlite:///{ROOT / 'data.db'}"
    # Render da "postgres://..."; SQLAlchemy + psycopg3 quiere esto:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
