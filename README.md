# AI Outage Concierge: HackOHI/O 2026 (AEP challenge)

An AI assistant that helps AEP customers before and during a storm, plus an internal operations portal for AEP staff. A customer finds their home on a 3D map and asks plain questions:
- "Will I lose power?"
- "How long until my power is back?"
- "What should I do?"
- "Do I qualify for alerts?"

Every number in an answer comes from AEP's data and our models, never from the LLM.

**What it does**
- **Outage-risk model:** a calibrated, explainable logistic regression trained on 35 past storms (ARCOS damage reports). It scores 333k poles, wires and devices, and rolls the scores up along each home's real supply path.
- **Live forecast:** each day of the Open-Meteo 7-day forecast is turned into a storm size through a gust curve fitted on ERA5 weather for every past storm, and the map shows the model's prediction for that day.
- **Restoration estimates:** STORMS labor hours × repair bundles from AEP structure drawings × a 2,000-run Monte Carlo. The result is a P10/P50/P90 range in hours.
- **Chat assistant:** Groq (with a Gemini fallback, or a fully offline rules mode). Each answer is built from tool results:
  - a direct answer
  - 2–3 things that could change it
  - one confidence meter that lists missing data when confidence is below 60%
- **Two portals:**
  - **Customer:**
    - **My home:** the live forecast map, a friendly chat, a **My plan** card (labeled emergency, safety and prepare steps, plus alert eligibility), and an outage panel when the feed reports an outage at your home.
    - **How it works:** the pipeline, metrics, research, what didn't work and what changed, who it helps, and honest limits.
    - **Outside AEP's data?** Any address or map point gets a clearly labeled, low-confidence **regional estimate** from the live forecast and how similar AEP-area homes fare.
  - **AEP Operations (internal):**
    - **Operations** (one map-first page): one storm scenario (a forecast day or a simulated storm) drives everything.
      - **Area view:** expected damage, customers likely out, high-risk circuits, crew-hours and crews needed, and priority facilities (hospitals, fire, ambulance) with suggested actions. Shown on a circuit map (2D dots or 3D columns), with proactive message previews and the alert rules below.
      - **Test a home:** flies into the detailed 3D map of one home. The side panel has three tabs:
        - **Storm:** intensity, crews, duration, affected area, ▶ Play storm.
        - **Outage test:** start a simulated outage at a piece of equipment or anywhere you click. You see where it is, the area affected, when it was reported, the estimated fix and why it takes that long.
        - **Chat:** the staff-voice assistant.
  - **The chat adapts its voice:** warm customer service on the customer side, a concise professional briefing on the operations side. Both use the same rules and the same numbers.
- **Privacy:**
  - Life Support and THREAT flags never reach the model or the LLM.
  - IDs and exact coordinates are redacted.
  - Every tool call is audit-logged.
- **Maps:** every map switches between 2D and 3D, and between a light and a dark basemap (remembered per browser). Switching tabs is instant: visited views stay loaded.
- **Notifications:** toasts appear when the forecast, your risk, the repair estimate or an outage changes, and disappear after 4 s. A bell keeps the history.

## Prerequisites

- **Docker Desktop** with at least 6 GB of memory.
- **The challenge data** (about 379 MB). It is provided separately and is not in git.
- *Optional:* a free **Groq API key** ([console.groq.com](https://console.groq.com)). Without one, the assistant runs in offline mode: the same tools and answer format, with rules-based wording.
- *Optional, only to run the frontend outside Docker:* Node 18+.

## Start it

1. **Add the data.** Put the challenge files in `data/` so it contains:
   ```
   data/Data1/
   data/Data2/
   data/matched_upstream.txt
   data/Install_Labor_HackOHIO.xlsx
   data/structure_drawings.pdf
   ```
2. **Add your LLM key (optional):**
   ```bash
   cp .env.example .env      # then set GROQ_API_KEY (and GEMINI_API_KEY for the fallback)
   ```
3. **Start everything:**
   ```bash
   docker compose up -d --build
   ```
   This starts four services:

   | Service | What it does |
   |---|---|
   | `db` | PostGIS 16 |
   | `loader` | Loads the data (~1–2 min) and scores every asset with the committed model (~30 s), then exits. Later runs skip both steps. |
   | `api` | FastAPI on http://localhost:8000. Interactive docs are at `/docs`. |
   | `web` | The React app on http://localhost:5173 |

4. **Check it's ready:**
   ```bash
   docker compose ps              # loader: "Exited (0)", api and web: "running"
   curl localhost:8000/health     # {"ok": true, ...}
   ```
5. **Open http://localhost:5173.** Click **try the demo address**, then **Yes, this is my home**.

Stop everything with `docker compose down`. The database is kept in a Docker volume.

## Use it

Switch portals at the top left: **Customer** or **AEP Operations**.

1. **My home (Customer):** drag the forecast day slider and switch between **2D** and **3D**. Tap **Will I lose power?** or **How long until my power is back?** Open **My plan** for your steps and alert eligibility, and tap a need (for example *Powered medical device*) to tailor it.
2. **Regional estimate:** click **Change**, search a place outside the area (for example *Columbus, Ohio*), then **Get a regional estimate for this location**.
3. **Operations (AEP Operations):** on the **Storm** tab, choose **Simulated storm** and move the intensity to 5. Watch the KPIs, the circuit map and the priority facilities update. Switch the map to **3D** or **☾ Dark**, and enter area crews to see hours to restore.
4. **Test a home:** press **Test a home →**. Homes, poles and wires near it turn red under the same storm. Change crews, duration and the affected area, or press **▶ Play storm**. On **Chat**, ask the same question as on My home to compare the two voices.
5. **Outage test tab:** pick a cause and press **Start outage**. Click a pole or wire on the map to move the fault, ask the chat when power will be back, then press **Restore power**.
6. **See a real outage on My home:** run `curl -X POST localhost:8000/storm/advance` to step the scripted demo storm forward, and `curl -X POST localhost:8000/storm/reset` to clear it.

For a scripted presentation (browser and curl), see [backend/demo/DEMO_SCRIPT.md](backend/demo/DEMO_SCRIPT.md).

## Run the tests

The backend suite runs inside Docker against the loaded database (start the stack first):

```bash
docker compose run --rm ml pytest -q tests                        # all tests (~60 s): 152 passed
docker compose run --rm ml pytest -q tests/test_outages.py        # one file
docker compose run --rm ml pytest -q tests -k restoration         # tests matching a name
```

If no `GROQ_API_KEY` is set, or the free tier is rate-limited, the live-LLM test is skipped.

| Test file | Covers |
|---|---|
| `test_database.py`, `test_db_link.py` | Data loading, row counts, and the model's link to the database |
| `test_training.py`, `test_scoring.py` | Model quality, no leakage between storms, scoring |
| `test_restoration.py` | Crews, delays and extra damage move the estimate the right way |
| `test_forecast.py` | Forecast → storm size (uses a fixture, no network) |
| `test_api.py`, `test_map.py` | Every endpoint and the map payload |
| `test_privacy.py` | No Life Support/THREAT flags, IDs or coordinates in any output |
| `test_assistant.py` | Routing, refusals, fallbacks, the grounding check, and the live LLM (if a key is set) |
| `test_sandbox.py`, `test_outages.py` | Simulated storms and the outage test, including that neither writes to the database |
| `test_ops.py` | Operations dashboard: KPIs rise with storm size, every facility listed, no customer-level data, read-only |
| `test_regional.py` | Regional estimates: low confidence and labeled, grow with storm size, no AEP IDs, in-area lookups unchanged |
| `test_demo.py` | The full 8-beat demo storm |

**Frontend checks:**
```bash
cd frontend && npm install && npm run build && npm audit
```

**Manual API calls:**
```bash
curl "localhost:8000/lookup?address=7110%20Rabbit%20Run,%20Roanoke"
curl -X POST localhost:8000/tools/get_outage_risk -H 'content-type: application/json' -d '{"premise":"982542862"}'
curl -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session_id":"me","premise":"982542862","message":"Will I lose power?"}'
curl "localhost:8000/outage-test?premise=982542862&device=1&cause=tree"
curl "localhost:8000/audit?limit=10"     # every tool call: masked input, fields returned and redacted
```

## Common tasks

| Task | Command |
|---|---|
| Stop (data kept) | `docker compose down` |
| Wipe the database and start fresh | `docker compose down -v && docker compose up -d --build` |
| Force a data reload | `FORCE_RELOAD=1 docker compose up loader` |
| Retrain the model (~5 min) | `docker compose run --rm ml` |
| Score one home from the CLI | `docker compose run --rm ml python -m ml.score 982542862 --severity high` |
| Rebuild the demo-safe chat cache | `docker compose run --rm ml python -m demo.build_cache` |
| Refit the gust → storm-size curve (historical weather) | `docker compose run --rm ml python -m ml.weather` |
| Rebuild the regional-estimate profile (after retraining) | `docker compose run --rm ml python -m regional` |
| Benchmark candidate models (writes `BENCHMARK.md`) | `docker compose run --rm ml python -m ml.benchmark` |
| Neural-operator (FNO) feasibility test | `docker compose run --rm ml python -m ml.benchmark --dump-baseline` then `docker compose --profile research run --rm research` (~3 h on CPU) |
| Follow the logs | `docker compose logs -f api` · `docker compose logs -f web` |
| Run the frontend outside Docker | `cd frontend && npm install && npm run dev` (expects the API on :8000) |

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` (falls back to Gemini), `gemini`, or `offline` |
| `GROQ_API_KEY` / `GROQ_MODEL` | – / `openai/gpt-oss-120b` | Groq |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-2.0-flash` | Gemini fallback |
| `POSTGRES_PORT`, `API_PORT`, `WEB_PORT` | `5432`, `8000`, `5173` | Host ports |
| `FORCE_RELOAD` | `0` | `1` makes the loader reload all data |

## Troubleshooting

| Problem | Fix |
|---|---|
| `port is already allocated` | Set `POSTGRES_PORT`, `API_PORT` or `WEB_PORT` in `.env` |
| The map is blank | Enable WebGL in your browser; the basemap also needs internet access (CARTO tiles) |
| The loader exits with an error | Check `data/` has every file listed above, then run `docker compose logs loader` |
| Chat says "LLM unavailable … answered with the rules-based assistant" | Check your key, model name or quota. Groq retires models: list yours at `https://api.groq.com/openai/v1/models` and set `GROQ_MODEL` |
| A container is killed, or loading is very slow | Give Docker Desktop more memory (≥ 6 GB) |
| You changed `requirements.txt` | `docker compose build` |

## File structure

```
.
├── docker-compose.yml        db, loader, api, web (+ ml profile for training/tests, research profile for the FNO test)
├── .env.example              copy to .env for API keys and ports
├── data/                     challenge data (not in git)
├── backend/                  Python 3 · FastAPI · PostGIS
│   ├── Dockerfile, requirements.txt, pytest.ini
│   ├── main.py               FastAPI app and endpoints
│   ├── tools.py              assistant tools: risk, restoration, guidance, alerts, messages, storm test
│   ├── assistant.py          LLM middleware: tool loop, grounding check, offline mode
│   ├── confidence.py         confidence score, missing data, "what could change this"
│   ├── restoration.py        labor bundles + Monte Carlo restoration estimate
│   ├── outages.py            outage records: fault location, affected area, ETR and why
│   ├── mapdata.py            map payload (homes, equipment, wires, supply path)
│   ├── forecast.py           Open-Meteo forecast → storm size (fitted gust curve)
│   ├── ops.py                AEP Operations dashboard: circuit KPIs, crews, priority facilities
│   ├── regional.py           regional estimates for locations outside AEP's data
│   ├── guidance.py           prep guidance, alert rules, SMS drafts
│   ├── privacy.py            redaction and the audit log (written to backend/logs/)
│   ├── lookup.py             address and map-click lookup
│   ├── storm.py              scripted demo storm timeline + SSE events
│   ├── db/                   data loader: shapefiles, topology, labor workbook → PostGIS
│   ├── ml/                   risk model: build_dataset → train → duration → score_assets; score.py scorer
│   │   ├── weather.py        ERA5 weather per storm + the fitted gust → storm-size curve
│   │   ├── benchmark.py      candidate models vs production (same storm hold-out) → BENCHMARK.md
│   │   ├── ASSUMPTIONS.md    every modelling assumption, documented
│   │   └── artifacts/        trained model, metrics, plots, BENCHMARK.md (intermediate .parquet files not in git)
│   ├── research/             FNO (neuraloperator) feasibility test; own Docker image, not used by the app
│   ├── demo/                 scenario.json, DEMO_SCRIPT.md, demo-safe cache.json, build_cache.py
│   └── tests/                pytest suite (+ fixtures/)
└── frontend/                 React 18 · Vite · deck.gl · MapLibre
    ├── index.html, package.json, vite.config.js
    └── src/
        ├── App.jsx           portals (Customer / AEP Operations), tabs, per-tab state, lazy-loaded views
        ├── api.js            API client
        ├── basemap.js        shared MapLibre setup
        ├── notify.js         what changed between map snapshots → notifications
        ├── format.js         risk colors and safe markdown for replies
        ├── meshes.js         generic 3D models (houses, trees, poles, equipment) built in code
        ├── trees.js          tree placement from OpenStreetMap areas and around homes
        ├── styles.css
        └── components/
            ├── Workspace.jsx         useWorkspace (map, outage and chat state) + the My home view
            ├── PlanCard.jsx          My plan: actions and alert eligibility
            ├── OpsDashboard.jsx      AEP Operations page: area KPIs and map, home drill-down, Storm / Outage test / Chat tabs
            ├── AboutPage.jsx         How it works
            ├── RiskMap.jsx           2D/3D deck.gl map
            ├── ChatPanel.jsx         chat, with ConfidenceMeter.jsx
            ├── SandboxPanel.jsx      simulated-storm sliders
            ├── OutageTestPanel.jsx   Outage test controls
            ├── OutageCard.jsx        outage panel (where, area, reported, estimated fix, why)
            ├── Notifications.jsx     toasts and the notification bell
            ├── LocationStep.jsx      "Is this your home?" and the regional-estimate option
            └── AuditDrawer.jsx       privacy & audit log view
```

## How it works

```mermaid
flowchart LR
  subgraph Data["PostGIS (db)"]
    RAW[(Challenge data\nARCOS, GIS, topology,\nSTORMS labor)]
    PRIV[(customer_private\nlife-support / threat\nbackend-only)]
    RISK[(asset_risk\nper-asset P(damage))]
  end
  ML["ml/ pipeline"] --> RISK
  RAW --> ML
  subgraph API["FastAPI (api)"]
    TOOLS["tools.py · restoration.py\noutages.py · mapdata.py"]
    PRIVACY["privacy.py\nredact + audit"]
    ASSIST["assistant.py\ntool loop · grounding check"]
  end
  RISK --> TOOLS
  RAW --> TOOLS
  PRIV -. escalation priority only .-> TOOLS
  FC["Open-Meteo forecast"] --> TOOLS
  TOOLS --> PRIVACY --> ASSIST
  ASSIST <--> LLM["Groq → Gemini\n(or offline)"]
  WEB["React app (web)\nmap + chat"] <--> API
```

- **Grounding:**
  - The LLM never sees the premise number or any private field.
  - Every number in its reply must appear in a tool result. If one doesn't, the deterministic answer is used instead.
  - Safety and escalation lines are always added by the backend.
- **Confidence** is a documented data-support score, not a statistical probability.
- **Assumptions and results:** [backend/ml/ASSUMPTIONS.md](backend/ml/ASSUMPTIONS.md) has every assumption, including the forecast bridge, the repair bundles, the Storm test and the Outage test. [METRICS.md](backend/ml/artifacts/METRICS.md) has model performance.

## API

| Endpoint | Purpose |
|---|---|
| `GET /lookup?address=` · `?lat=&lon=` | "Is this your home?" candidates |
| `POST /chat` | Assistant reply + what could change it + confidence. Accepts `storm` (match the map), `outage` (Outage test) and `audience` (`customer` or `operations` voice) |
| `POST /tools/{get_outage_risk, estimate_restoration, prep_guidance, check_alert_eligibility, draft_message}` | The assistant's tools, callable directly |
| `GET /map/context?premise=&day=` · `&mode=sandbox&intensity=&crews=&duration_days=&radius_m=` | Map payload for a forecast day or a simulated storm |
| `GET /forecast?premise=` | 7-day forecast and the storm size each day implies |
| `GET /outages/current?premise=` | Current outage at this home (demo feed), or `null` |
| `GET /outage-test/devices?premise=` · `GET /outage-test?premise=&device=\|lat=&lon=&cause=&reported_hours_ago=&crews=` | Outage test: fault locations, and the simulated outage record |
| `POST /storm/advance` · `/storm/reset` · `GET /storm/state` · `/storm/events` (SSE) | Scripted demo storm |
| `GET /ops/overview?mode=forecast&day=` · `?mode=sandbox&intensity=&duration_days=` · `&crews=` | AEP Operations dashboard: KPIs, riskiest circuits, priority facilities (internal, read-only) |
| `GET /regional/locate?address=` · `?lat=&lon=` | A regional-estimate location outside AEP's data; use its `region:` key as the premise in every other endpoint |
| `GET /audit` · `GET /health` | Audit log · readiness |

## Known limitations

- **Wire repairs are a lower bound.** The STORMS data has no hours for restringing conductor.
- **The demo storm is scripted** and applies only to the demo home (premise `982542862`). Sessions and storm state live in memory, so run a single API worker.
- **Outage test fault mapping is simplified.** A pole or wire fault is assumed to trip the nearest fuse or recloser on the same circuit within 400 m.
- **LLM wording varies between runs.** The numbers are still checked against tool results.
- **The AEP Operations portal has no login.** It's labeled internal; a production deployment would put it behind AEP single sign-on.
- **Regional estimates aren't location-specific.** Outside AEP's data, the answer is a typical value for similar AEP-area homes. Location-specific accuracy elsewhere needs that utility's grid data (GIS, topology, damage history), or a model rebuilt on nationwide proxies (tree canopy, OpenStreetMap power lines) and validated on outage records from several regions.
