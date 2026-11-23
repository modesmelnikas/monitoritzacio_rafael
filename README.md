# Monitorización de strings FV · Instalación Rafael

Web app que lee el inversor **Solis 100 kW** vía **SolisCloud API**, guarda la
tensión/corriente/potencia de cada string cada 5 minutos y **compara los
strings entre sí** para detectar los que rinden menos. El dashboard está pensado
para el móvil y muestra los seguidores sobre un plano.

## Instalación modelada (`plant.yaml`)

| | |
|---|---|
| Seguidores (2 ejes) | 114, 117, 119, 120, 121, 122, 123, 124, 125, 126 |
| Por seguidor | 30 paneles de 210 W en 3 strings de 10 (S1, S2, S3) → 6,3 kWp |
| Total | 30 strings · 63 kWp |
| Inversor | Solis 100 kW, 10 MPPT, 20 entradas PV |

**Cableado**: cada seguidor va a una MPPT, por orden (114 → MPPT 1 … 126 → MPPT 10).
En la entrada impar van **S1 y S2 en paralelo**; en la par, **S3** solo
(MPPT 1: PV1 = 114-S1+S2, PV2 = 114-S3).

El inversor no distingue S1 de S2: ve su suma. Para comparar, la app usa la
**corriente por string** (corriente de la entrada ÷ strings que lleva). Si uno
de los dos del paralelo se corta, esa entrada cae un ~50 % y salta una alarma
específica; un fallo parcial de S1 o S2 se ve a medias (un 20 % menos en un
string es un 10 % menos en la entrada).

**Suposiciones a confirmar** (se cambian en `plant.yaml`, sin tocar código):

1. SolisCloud da la tensión por MPPT (`uPv1..uPv10`) y la corriente por entrada
   (`iPv1..iPv20`). Si da la tensión por entrada, poner `solis.voltage_by: input`.
   Tras la primera lectura real, `/api/raw` enseña la respuesta cruda para comprobarlo.
2. Plano: los 10 seguidores en una fila (10 × 1), en el orden de la lista (`x`, `y`).

## Qué detecta

Todos los strings son iguales y todos los seguidores siguen al sol, así que en
el mismo instante deben dar casi la misma corriente por string. Con radiación suficiente
(mediana > 1 A):

- **Aviso**: corriente por string un 10 % por debajo de la mediana; **alarma**: un 25 %.
- **Alarma**: entrada a 0 A mientras las demás producen (fusible, conector).
- **Alarma**: entrada en paralelo a la mitad → uno de S1/S2 no da corriente.
- **Seguidor**: si todos sus strings están bajos a la vez → probable fallo de
  seguimiento (no orienta, parado, sombra).
- **Tensión** de la MPPT un 5 % por debajo de la mediana → paneles en bypass.
- **Rendimiento diario** (kWh/kWp por entrada, 7 días) para ver degradaciones lentas.

Los umbrales están en `plant.yaml` → `analysis`.

## Probar en local

```bash
pip install -r requirements.txt
SIMULATE=true uvicorn app.main:app --reload
# http://localhost:8000
```

Sin credenciales funciona en **modo simulado** (rellena 2 días de histórico, con
el S3 del 121 al 70 % y un string del paralelo del 124 cortado).
Tests: `pytest`.

## Desplegar en Render

1. En Render: **New → Blueprint** y elegir este repositorio (usa `render.yaml`).
2. Rellenar las variables secretas: `SOLIS_KEY_ID`, `SOLIS_KEY_SECRET`,
   `SOLIS_INVERTER_SN` (o `SOLIS_INVERTER_ID`) y `DASHBOARD_PASSWORD`.
3. Abrir la URL; usuario `rafael` y la contraseña puesta.

Las credenciales de API se piden a Solis (en SolisCloud: *Account → Basic
Settings → API Management*, o vía soporte de Solis).

**Coste**: el plan *free* de Render se duerme tras 15 min sin visitas y deja de
leer el inversor, y su Postgres gratuito caduca a los 30 días. El blueprint usa
*starter* (≈7 $/mes) + Postgres *basic* (≈6 $/mes). Alternativa barata: web en
*free* + Postgres gratuito de Neon en `DATABASE_URL` + un ping externo cada 10 min
(p. ej. cron-job.org a `/healthz`) o un Cron Job de Render con `python -m app.poller`
y `RUN_SCHEDULER=false`.

## API

| Ruta | Qué da |
|---|---|
| `GET /api/status` | Última lectura analizada por seguidor y string |
| `GET /api/history?hours=24&tracker=114` | Serie temporal por string |
| `GET /api/daily?days=7` | kWh y kWh/kWp por string y día |
| `GET /api/raw` | Respuesta cruda de SolisCloud (para revisar el mapeo) |
| `POST /api/collect` | Fuerza una lectura |

## Estructura

```
app/solis.py      cliente SolisCloud (firma HMAC-SHA1) y mapeo de campos
app/analysis.py   comparación entre strings
app/poller.py     lectura periódica y guardado
app/main.py       API + planificador (FastAPI)
static/           dashboard (HTML/CSS/JS sin dependencias)
plant.yaml        seguidores, strings, posiciones y umbrales
```
