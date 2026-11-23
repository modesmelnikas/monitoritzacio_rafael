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

1. Confirmado con la primera lectura real: SolisCloud da tensión, corriente y
   potencia por entrada (`uPv1..20`, `iPv1..20`, `pow1..20`).
2. Plano: los 10 seguidores en una fila (10 × 1), en el orden de la lista (`x`, `y`).

## Qué detecta

Todos los strings son iguales y todos los seguidores siguen al sol, así que en
el mismo instante deben dar casi la misma corriente por string. Con radiación suficiente
(mediana > 1 A):

- Cada entrada se compara con las de **su mismo tipo** (las S1+S2 entre sí, las S3
  entre sí), porque en la práctica las S3 dan algo más por string que las S1+S2.
- **Aviso**: corriente por string un 10 % por debajo de la mediana de su tipo; **alarma**: un 25 %.
- **Alarma**: entrada a 0 A mientras las demás producen (fusible, conector).
- **Alarma**: entrada en paralelo a la mitad → uno de S1/S2 no da corriente.
- **Seguidor**: si todos sus strings están bajos a la vez → probable fallo de
  seguimiento (no orienta, parado, sombra).
- **Tensión** de la MPPT un 5 % por debajo de la mediana → paneles en bypass.
- **Rendimiento diario** (kWh/kWp por entrada, 7 días) para ver degradaciones lentas.

Los umbrales están en `plant.yaml` → `analysis`.

## Cómo funciona (montaje gratuito)

```
GitHub Actions (cada 10 min de día) ──► SolisCloud API
          │
          ▼
   Neon (Postgres gratis) ◄── Render (web gratis, solo dashboard)
```

- **Lecturas**: `.github/workflows/leer-inversor.yml` ejecuta `python -m app.poller`
  cada 10 minutos de 5:00 a 19:50 UTC. (Con `python -m app.poller 2 300` tomaría
  2 lecturas separadas 5 min por ejecución sin despertar Neon más veces.) De noche (inversor sin producción) no
  guarda nada. Gratis porque el repositorio es público.
- **Base de datos**: Neon, proyecto `monitoritzacio-rafael`, base de datos `fv`
  (plan gratis: 0,5 GB, no caduca). Las tablas se crean solas en la primera lectura.
  Solo se guarda la última respuesta cruda de SolisCloud, para ocupar poco
  (≈ 100 MB al año, unos 5 años en el plan gratis).
- **Web**: Render plan *free*. Si nadie la abre en 15 min se duerme y la siguiente
  visita tarda ~30 s en cargar; los datos no se pierden.

### Puesta en marcha

1. **Neon** → proyecto `monitoritzacio-rafael` → *Connect* → copiar la
   *connection string* (`postgresql://...`).
2. **GitHub** → *Settings → Secrets and variables → Actions → New repository secret*:
   `DATABASE_URL` (la de Neon), `SOLIS_KEY_ID`, `SOLIS_KEY_SECRET`, `SOLIS_INVERTER_SN`.
3. Fusionar el PR a `main` (las tareas programadas solo corren desde `main`).
4. **Actions → Leer inversor → Run workflow** para probar ya. Si sale en verde, ya
   está guardando.
5. **Render** → *New → Blueprint* → este repositorio. Rellenar `DATABASE_URL` (la
   misma de Neon) y `DASHBOARD_PASSWORD`.
6. Abrir `https://<tu-app>.onrender.com/api/raw` para revisar la respuesta de
   SolisCloud y confirmar el mapeo de tensiones (ver suposiciones).

Las credenciales de API se piden en SolisCloud (*Account → Basic Settings → API
Management*) o al soporte de Solis.

**Ojo**: GitHub desactiva las tareas programadas si el repositorio pasa 60 días sin
cambios; avisa por correo y se reactiva con un clic en *Actions*.

## Probar en local

```bash
pip install -r requirements.txt
SIMULATE=true uvicorn app.main:app --reload
# http://localhost:8000
```

Con `SIMULATE=true` rellena 2 días de histórico simulado (el S3 del 121 al 70 %
y un string del paralelo del 124 cortado, para ver las alarmas). Sin
`SIMULATE=true` nunca genera datos falsos. Tests: `pytest`.

## Dashboard (en catalán)

- **Plànol**: potencia AC y kWh de hoy, mes y año (de SolisCloud), plano de los
  seguidores (5 × 2 en el móvil, 10 × 1 en pantalla ancha) y curva de potencia
  del día (dato cada 10 min) con flechas para ver días anteriores.
- **Strings**: una tarjeta por seguidor con S1, S2 y S3 en barras; Δ es la
  diferencia entre el mejor y el peor string del seguidor. S1 y S2 (en paralelo)
  muestran la mitad de su entrada cada uno.
- **Seguidors**: kWh DC por seguidor por día, mes o año (tabla `input_daily`).
- **Alarmes**: lo que detecta la última lectura, el registro de alarmas y el
  **creador de reglas** (`app/rules.py`): desviación de un string, string sin
  corriente, diferencia dentro de un seguidor, desviación de un seguidor, tensión
  baja, potencia AC baja y datos antiguos. Cada regla tiene umbral, lecturas
  seguidas necesarias, seguidor (o todos) y gravedad. También se pueden crear
  alarmas a mano.

### Avisos al móvil (Telegram)

Cuando una alarma automática se confirma, el lector manda un mensaje por Telegram.
1. En Telegram, habla con **@BotFather**, `/newbot`, y copia el token.
2. Escribe cualquier cosa a tu bot y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates`: el número `chat.id` es tu id.
3. En GitHub → Settings → Secrets → Actions, añade `TELEGRAM_BOT_TOKEN` y
   `TELEGRAM_CHAT_ID` (varios ids separados por comas para avisar a más personas).

## API

| Ruta | Qué da |
|---|---|
| `GET /api/status` | Última lectura analizada por seguidor y string |
| `GET /api/history?hours=24&tracker=114` | Serie temporal por string |
| `GET /api/daily?days=7` | kWh y kWh/kWp por string y día |
| `GET /api/production?days=30` | kWh diarios del inversor |
| `GET /api/trackers/energy?period=day\|month\|year&date=` | kWh por seguidor |
| `GET/POST /api/alerts`, `POST /api/alerts/{id}/close\|reopen` | Registro de alarmas |
| `GET/POST /api/rules`, `PUT/DELETE /api/rules/{id}` | Reglas de alarma |
| `GET /api/power?date=` | Curva de potencia de un día |
| `GET /api/raw` | Respuesta cruda de SolisCloud (para revisar el mapeo) |
| `POST /api/collect` | Fuerza una lectura (solo si el servidor tiene las claves de Solis) |

## Estructura

```
app/solis.py      cliente SolisCloud (firma HMAC-SHA1) y mapeo de campos
app/analysis.py   comparación entre strings
app/poller.py     lectura y guardado (lo lanza GitHub Actions)
app/main.py       API + dashboard (FastAPI)
.github/workflows leer-inversor.yml (cada 10 min) y tests.yml
static/           dashboard (HTML/CSS/JS sin dependencias)
plant.yaml        seguidores, strings, posiciones y umbrales
```
