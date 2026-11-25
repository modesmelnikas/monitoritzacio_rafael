"""Resum diari de producció per correu (en català).

Es dispara des d'un cron extern a /api/resum-diari?key=CRON_KEY (p. ex. a les
21:30). Envia amb l'API HTTP de Brevo (gratuïta, 300 correus/dia), perquè Render
en pla gratuït bloqueja l'SMTP. Si no hi ha BREVO_API_KEY però sí SMTP_HOST,
prova per SMTP (útil en local o des de GitHub Actions).

Variables:
  EMAIL_TO        destinataris, separats per comes
  EMAIL_FROM      remitent (a Brevo, un correu verificat com a "sender")
  BREVO_API_KEY   clau API de Brevo (SMTP & API > API keys)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD   alternativa per SMTP
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from statistics import median
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select

from .config import get_plant
from .db import Alert, DailyEnergy, InputDaily, SessionLocal

log = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Europe/Madrid")
WARN_PCT = 10.0     # un seguidor o una entrada per sota de la mitjana més d'això es marca
MESOS = ["gener", "febrer", "març", "abril", "maig", "juny", "juliol", "agost",
         "setembre", "octubre", "novembre", "desembre"]
DIES = ["dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"]


def default_day(now: datetime | None = None) -> date:
    """El dia del resum: avui; però si s'envia de matinada (abans de les 6), ahir."""
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    return (now - timedelta(days=1)).date() if now.hour < 6 else now.date()


def _pct(v: float, ref: float) -> float | None:
    return round((v / ref - 1) * 100, 1) if ref > 0 else None


def build_summary(day: date) -> dict:
    plant = get_plant()
    d = day.isoformat()
    yday = (day - timedelta(days=1)).isoformat()
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with SessionLocal() as db:
        inputs = {r.string_id: r.kwh for r in
                  db.scalars(select(InputDaily).where(InputDaily.day == d))}
        prev_inputs = {r.string_id: r.kwh for r in
                       db.scalars(select(InputDaily).where(InputDaily.day == yday))}
        ac = db.get(DailyEnergy, d)
        ac_prev = db.get(DailyEnergy, yday)
        month = db.scalars(select(DailyEnergy).where(DailyEnergy.day.like(d[:7] + "%"),
                                                     DailyEnergy.day <= d)).all()
        alerts = db.scalars(
            select(Alert).where(Alert.state.in_(("open", "closed")),
                                or_(Alert.created.between(start, end),
                                    Alert.last_seen.between(start, end)))
            .order_by(Alert.created)).all()
        open_total = len(db.scalars(select(Alert.id).where(Alert.state == "open")).all())

    trackers = []
    for t in plant.trackers:
        kwp = plant.string_wp * sum(s.n_strings for s in t.strings) / 1000
        kwh = sum(inputs.get(s.id, 0.0) for s in t.strings)
        prev = sum(prev_inputs.get(s.id, 0.0) for s in t.strings)
        trackers.append({"id": t.id, "kwh": round(kwh, 1), "kwp": kwp,
                         "kwh_kwp": round(kwh / kwp, 2) if kwp else None,
                         "prev_kwh": round(prev, 1) if prev_inputs else None})
    yields = [t["kwh_kwp"] for t in trackers if t["kwh_kwp"]]
    ref = median(yields) if yields else 0
    for t in trackers:
        t["dev_pct"] = _pct(t["kwh_kwp"] or 0, ref) if inputs else None
        t["flag"] = t["dev_pct"] is not None and t["dev_pct"] <= -WARN_PCT
    ranked = sorted((t for t in trackers if inputs), key=lambda t: t["kwh"], reverse=True)
    dc = sum(inputs.values())
    return {
        "plant": plant.name, "day": d, "has_data": bool(inputs) or ac is not None,
        "ac_kwh": round(ac.kwh, 1) if ac else None,
        "ac_prev_kwh": round(ac_prev.kwh, 1) if ac_prev else None,
        "dc_kwh": round(dc, 1) if inputs else None,
        "month_kwh": round(sum(r.kwh for r in month), 1),
        "kwp": round(plant.string_wp * plant.total_strings / 1000, 1),
        "trackers": trackers,
        "best": ranked[0]["id"] if ranked else None,
        "worst": ranked[-1]["id"] if ranked else None,
        "alerts": [{"time": a.created.astimezone(LOCAL_TZ).strftime("%H:%M")
                    if a.created.tzinfo else a.created.strftime("%H:%M"),
                    "severity": a.severity, "state": a.state,
                    "title": a.title if not a.tracker or a.tracker in a.title
                    else f"Seguidor {a.tracker} · {a.title}"}
                   for a in alerts],
        "open_alerts": open_total,
    }


def _num(v: float | None, dec: int = 1) -> str:
    if v is None:
        return "–"
    return f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")


def _signed(v: float | None) -> str:
    return "–" if v is None else ("+" if v > 0 else "") + _num(v) + " %"


def day_label(d: str) -> str:
    x = date.fromisoformat(d)
    mes = MESOS[x.month - 1]
    de = "d'" if mes[0] in "aeiouà" else "de "
    return f"{DIES[x.weekday()]} {x.day} {de}{mes} de {x.year}"


def subject(s: dict) -> str:
    kwh = s["ac_kwh"] if s["ac_kwh"] is not None else s["dc_kwh"]
    flags = sum(t["flag"] for t in s["trackers"])
    tail = f" · ⚠️ {flags} seguidor{'s' if flags != 1 else ''} a revisar" if flags else " · tot correcte"
    if not s["has_data"]:
        tail = " · sense dades"
    return f"☀️ {s['plant']} · {date.fromisoformat(s['day']).strftime('%d/%m')}: {_num(kwh)} kWh{tail}"


def render_text(s: dict) -> str:
    lines = [f"{s['plant']} — resum de {day_label(s['day'])}", ""]
    if not s["has_data"]:
        return "\n".join(lines + ["Avui no hi ha lectures guardades de l'inversor."])
    lines += [f"Producció inversor (AC): {_num(s['ac_kwh'])} kWh"
              + (f" (ahir {_num(s['ac_prev_kwh'])} kWh)" if s["ac_prev_kwh"] is not None else ""),
              f"Suma seguidors (DC): {_num(s['dc_kwh'])} kWh",
              f"Acumulat del mes: {_num(s['month_kwh'])} kWh", "",
              "Seguidor  kWh    kWh/kWp  vs mitjana"]
    for t in s["trackers"]:
        lines.append(f"{'⚠ ' if t['flag'] else '  '}{t['id']:<7} {_num(t['kwh']):>6} "
                     f"{_num(t['kwh_kwp'], 2):>8}  {_signed(t['dev_pct']):>9}")
    lines += ["", f"Alarmes del dia: {len(s['alerts'])} · obertes ara: {s['open_alerts']}"]
    lines += [f"  {a['time']} {a['title']}" + (" (tancada)" if a["state"] == "closed" else "")
              for a in s["alerts"]]
    return "\n".join(lines)


def _color(dev: float | None) -> str:
    if dev is None:
        return "#64748b"
    if dev <= -25:
        return "#dc2626"
    if dev <= -WARN_PCT:
        return "#d97706"
    return "#16a34a"


def render_html(s: dict, dashboard_url: str = "") -> str:
    e = html.escape
    head = (f"<h2 style='margin:0 0 4px;font-size:20px'>☀️ {e(s['plant'])}</h2>"
            f"<div style='color:#64748b;margin-bottom:16px'>Resum de {e(day_label(s['day']))}</div>")
    if not s["has_data"]:
        body = "<p>Avui no hi ha lectures guardades de l'inversor.</p>"
        return _wrap(head + body, dashboard_url)
    diff = ""
    if s["ac_kwh"] is not None and s["ac_prev_kwh"]:
        p = _pct(s["ac_kwh"], s["ac_prev_kwh"])
        diff = f"<div style='font-size:12px;color:{_color(p)}'>{_signed(p)} vs ahir</div>"

    def kpi(label, value, extra=""):
        return (f"<td style='padding:10px;background:#f1f5f9;border-radius:8px;width:33%'>"
                f"<div style='font-size:12px;color:#64748b'>{label}</div>"
                f"<div style='font-size:20px;font-weight:700'>{value}</div>{extra}</td>")
    kpis = ("<table role='presentation' width='100%' cellspacing='6' style='margin:0 -6px 12px'><tr>"
            + kpi("Inversor (AC)", f"{_num(s['ac_kwh'])} kWh", diff)
            + kpi("Seguidors (DC)", f"{_num(s['dc_kwh'])} kWh")
            + kpi("Acumulat mes", f"{_num(s['month_kwh'], 0)} kWh") + "</tr></table>")
    best = max((t["kwh"] for t in s["trackers"]), default=0) or 1
    rows = []
    for t in s["trackers"]:
        w = max(2, round(t["kwh"] / best * 100))
        bg = "#fef3c7" if t["flag"] else "#ffffff"
        rows.append(
            f"<tr style='background:{bg};border-top:1px solid #e2e8f0'>"
            f"<td style='padding:8px 6px;font-weight:700'>{'⚠️ ' if t['flag'] else ''}{e(t['id'])}</td>"
            f"<td style='padding:8px 6px'><div style='font-weight:600'>{_num(t['kwh'])} kWh</div>"
            f"<div style='background:#e2e8f0;border-radius:3px;height:6px'>"
            f"<div style='background:{_color(t['dev_pct'])};width:{w}%;height:6px;border-radius:3px'></div></div>"
            f"<div style='font-size:12px;color:{_color(t['dev_pct'])}'>{_signed(t['dev_pct'])} vs mitjana · "
            f"{_num(t['kwh_kwp'], 2)} kWh/kWp</div></td></tr>")
    table = ("<table width='100%' cellspacing='0' style='border-collapse:collapse;font-size:14px'>"
             "<tr style='color:#64748b;font-size:12px;text-align:left'><th style='padding:4px 6px'>Seguidor</th>"
             "<th style='padding:4px 6px'>Producció</th></tr>"
             + "".join(rows) + "</table>")
    sev = {"alarm": "🔴", "warning": "🟠", "info": "🔵"}
    if s["alerts"]:
        items = "".join(f"<li>{a['time']} {sev.get(a['severity'], '⚠️')} {e(a['title'])}"
                        f"{' <i>(tancada)</i>' if a['state'] == 'closed' else ''}</li>"
                        for a in s["alerts"])
        al = f"<ul style='padding-left:18px;margin:4px 0'>{items}</ul>"
    else:
        al = "<p style='margin:4px 0'>Cap alarma avui ✅</p>"
    alarms = (f"<h3 style='font-size:16px;margin:16px 0 4px'>Alarmes del dia ({len(s['alerts'])})</h3>{al}"
              f"<p style='font-size:12px;color:#64748b'>Alarmes obertes ara: {s['open_alerts']}</p>")
    return _wrap(head + kpis + table + alarms, dashboard_url)


def _wrap(inner: str, url: str) -> str:
    link = (f"<p style='margin-top:20px'><a href='{html.escape(url)}' style='background:#f59e0b;"
            "color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600'>"
            "Obrir el tauler</a></p>") if url else ""
    return ("<!doctype html><html><body style='margin:0;background:#f8fafc'>"
            "<div style='max-width:640px;margin:0 auto;padding:16px;font-family:-apple-system,"
            "Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;background:#fff'>"
            f"{inner}{link}</div></body></html>")


# ---- enviament ----

def recipients() -> list[str]:
    return [x.strip() for x in os.getenv("EMAIL_TO", "").replace(";", ",").split(",") if x.strip()]


def enabled() -> bool:
    return bool(recipients() and os.getenv("EMAIL_FROM") and
                (os.getenv("BREVO_API_KEY") or os.getenv("SMTP_HOST")))


def send_email(subj: str, html_body: str, text_body: str,
               client: httpx.Client | None = None) -> str:
    """Envia el correu. Retorna el mètode usat ("brevo" o "smtp")."""
    to, sender = recipients(), os.getenv("EMAIL_FROM", "").strip()
    if not enabled():
        raise RuntimeError("Falten variables: EMAIL_TO, EMAIL_FROM i BREVO_API_KEY (o SMTP_HOST)")
    name = os.getenv("EMAIL_FROM_NAME", "Monitorització FV")
    if os.getenv("BREVO_API_KEY"):
        client = client or httpx.Client(timeout=20)
        r = client.post("https://api.brevo.com/v3/smtp/email",
                        headers={"api-key": os.environ["BREVO_API_KEY"].strip(),
                                 "accept": "application/json"},
                        json={"sender": {"email": sender, "name": name},
                              "to": [{"email": x} for x in to], "subject": subj,
                              "htmlContent": html_body, "textContent": text_body})
        if r.status_code >= 300:
            raise RuntimeError(f"Brevo {r.status_code}: {r.text[:300]}")
        return "brevo"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subj, f"{name} <{sender}>", ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    port = int(os.getenv("SMTP_PORT") or 587)
    host = os.environ["SMTP_HOST"]
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) if port == 465 else \
        smtplib.SMTP(host, port, timeout=30)
    with smtp:
        if port != 465:
            smtp.starttls(context=ctx)
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return "smtp"


def send_daily(day: date | None = None, dashboard_url: str = "") -> dict:
    s = build_summary(day or default_day())
    subj = subject(s)
    method = send_email(subj, render_html(s, dashboard_url), render_text(s))
    log.info("Resum diari %s enviat (%s) a %s", s["day"], method, recipients())
    return {"ok": True, "day": s["day"], "subject": subj, "to": recipients(), "via": method}
