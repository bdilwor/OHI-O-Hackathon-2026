# AI Outage Concierge: HackOHI/O 2026 (AEP challenge)

An AI assistant that answers a customer's storm questions ("Will I lose power?", "How long could it take?", "What should I do?", "Do I qualify for alerts?"). Every number comes from AEP data and our models, never from the LLM.

## Status

| Part | State |
|---|---|
| Data → PostGIS (ARCOS, GIS, topology, STORMS labor) | ✅ Done |
| Outage-risk model (trained on 35 past storms, calibrated, explainable) | ✅ Done |
| Restoration estimator (STORMS labor × AEP structure drawings × Monte Carlo) | ✅ Done |
| FastAPI tools, privacy filter, audit log, demo storm timeline | ✅ Done |
| LLM assistant (Groq → Gemini fallback, grounding check, offline mode) | ✅ Done, live-tested with Groq |
| Live weather forecast → storm size for the model (Open-Meteo) | ✅ Done |
| React frontend: forecast-driven 2D/3D map (generic houses, trees, poles, equipment) + chat | ✅ Done |
| Storm test + Outage test areas, outage details panel, notifications | ✅ Done |
| Tests | ✅ 134 backend tests + scripted browser walkthrough |

## Quick start

You need **Docker Desktop**, with at least 6 GB of memory. You also need the challenge files.

1. **Add the data.** Put the challenge files in `data/`. They are gitignored and provided separately. The folder should contain:
   - `data/Data1/`
   - `data/Data2/`
   - `data/matched_upstream.txt`
   - `data/Install_Labor_HackOHIO.xlsx`
   - `data/structure_drawings.pdf`
2. **Optional: add an LLM key.**
   ```bash
   cp .env.example .env      # then paste your GROQ_API_KEY (free at console.groq.com)
   ```
   Without a key, the assistant runs in **offline mode**: a rules-based router that uses the same tools and answers in the same format.
3. **Start everything:**
   ```bash
   docker compose up -d --build
   ```
   On the first run this:
   - starts PostGIS
   - loads the data (~1–2 min)
   - publishes the per-asset risk table from the committed model (~30 s)
   - starts the API
   - installs and starts the web app

   Later runs skip the loading and are ready in seconds.
4. **Check it's up.** The API is ready once `/health` returns `{"ok": true, ...}`:
   ```bash
   docker compose ps                           # loader: "Exited (0)", api: "running"
   curl localhost:8000/health
   ```
   Interactive API docs: http://localhost:8000/docs
5. **Open the app:** http://localhost:5173. Click **try the demo address**, then **Yes, this is my home**.

## Test it

**A. Full automated suite** (~30 s):
```bash
docker compose run --rm ml pytest -q tests
```
Expected: `134 passed`. If no `GROQ_API_KEY` is set, the live-LLM test is reported as skipped. The suite covers:
- data loading and row counts
- training quality and no data leakage between storms
- the model's link to the database
- restoration behavior (crews, delays, extra damage)
- every API endpoint
- privacy (no Life Support/THREAT flag, IDs or coordinates in any output)
- the assistant's routing, refusals, fallbacks and grounding check
- the full 8-beat demo
- outages: affected area from the real topology, ETR after the report time, map clicks resolved to equipment, nothing written

**B. Try the main calls by hand:**
```bash
curl "localhost:8000/lookup?address=7110%20Rabbit%20Run,%20Roanoke"

curl -X POST localhost:8000/tools/get_outage_risk -H 'content-type: application/json' \
  -d '{"premise":"982542862"}'

curl -X POST localhost:8000/tools/estimate_restoration -H 'content-type: application/json' \
  -d '{"premise":"982542862","crew_arrival_delay_hrs":24}'

curl -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session_id":"me","premise":"982542862","message":"Will I lose power?"}'
```

**C. Walk the full demo** (storm timeline, proactive messages, what-ifs, escalation, refusals, fallback): follow [backend/demo/DEMO_SCRIPT.md](backend/demo/DEMO_SCRIPT.md). Add `"demo_safe": true` to any `/chat` call to serve the pre-recorded answers in `backend/demo/cache.json` if the network or the LLM fails during a presentation.

**D. Click through the app** at http://localhost:5173:
1. Use the demo address.
2. Look at the 3D map for today's real forecast (usually mostly green), then drag the forecast day slider. The "No outages reported at your home" chip shows there's no current outage.
3. Tap **Will I lose power?** You get an answer, what could change it, and a confidence meter.
4. Tap **How long would repairs take?** on a far-out forecast day. Confidence drops below 60%, and the missing data is listed.
5. Switch between **2D** and **3D**.
6. Open **Storm test**, drag the intensity slider to 5 and watch homes, poles and wires turn orange and red. Toasts appear and fade after about 4 s.
7. Open **Outage test**, press **Start outage**, then click a pole or wire elsewhere on the map. The outage panel shows where it is, the area affected, when it was reported, the estimated fix (day, month, year) and why it takes that long. Ask **How long until my power is back?**, then press **Restore power**.

**E. See the controls:** `curl "localhost:8000/audit?limit=10"` shows every tool call, with a masked input, the fields returned and the fields redacted.

## Three areas: My home, Storm test and Outage test

- **My home** (`#/`) is what a customer sees. It shows the live 7-day forecast, the outage model's prediction for the selected day, and the chat. If the outage feed reports an outage at the home (in this project, the demo storm timeline after `POST /storm/advance`), the **outage panel** appears; otherwise a small "No outages reported at your home" chip shows.
- **Storm test** (`#/storm-test`; `#/sandbox` still works) has sliders for **storm intensity (1–5)**, **available crews (1–30)**, **storm duration (1–5 days)** and the **affected area** (metres or yards, centered on the home).
  - **Play storm** ramps the storm up and back down.
  - The map, the notifications and the chat all react to the sliders.
  - It uses the real model read-only; nothing is saved (a test checks this).
- **Outage test** (`#/outage-test`) simulates a real outage. Pick where the fault is from the equipment on your supply path (transformer, fuses, reclosers) **or click any pole, wire or piece of equipment on the map**. Choose a cause (tree, broken pole, vehicle, transformer failure, lightning, equipment failure), how long ago it was reported (0–24 h) and the crews (1–30), then **Start outage** / **Restore power**.
  - The map draws the affected area (red polygon), the fault pin, and turns affected homes red with dark windows.
  - The chat knows the power is out and answers repair-time questions from this outage.
  - Test outages stay in this tab and are never saved.
- **The outage panel** (same in My home and Outage test) shows the cause, **where** the fault is, the **area affected** (homes, km², streets), when it was **reported** and the **estimated fix** with its likely range (dates like "Sat, 24 Oct 2026, 10:18 pm"), progress so far, and **why it will take about X hours** (repair work, crews and arrival, upstream-first order, travel/safety factor).
- **Notifications** pop up when the forecast, your outage risk, the repair estimate, the number of nearby predicted outages or an outage changes. Real outage events also arrive over SSE. Toasts disappear after 4 s (hovering pauses that), at most 3 show at once, and a repeat of the same alert replaces the earlier one. A bell keeps the history, and desktop alerts are optional.

## Frontend: forecast-driven 2D/3D map + chat

The map is on the left and the chat on the right. Everything on the map is the **outage model's prediction for one day of the live weather forecast** (Open-Meteo, from today through 6 days out). Use **Storm test** to see the map under a storm on a calm week.

| Control | What it does |
|---|---|
| **2D / 3D** | 2D: flat homes, equipment and lines. 3D: generic low-poly models (built in code, no model files) |
| **Forecast day slider** | Today … +6 days; each day's peak gust and conditions set the storm size the model uses |
| **Layers** | Homes · poles, equipment & lines · trees · your supply path |

Colors run from **green (good) to red (bad)**:
- **Homes:** chance of an outage in that area. Your house is the large one and uses the full premise model.
- **Trees:** colored like the nearest home. They are placed only inside real OpenStreetMap wooded and park areas from the basemap; the challenge data has no vegetation.
- **Poles, transformers, fuses, reclosers and wires:** chance that item is damaged. Overhead wires are drawn at pole height in 3D.

The supply path (home → transformer → fuse → reclosers) shows flowing current, and the restoration bottleneck is labeled.

**Chat answers** have three parts:
1. **A direct answer** from the backend's tools: the live LLM, or the rules-based fallback.
2. **"What could change this"** with 2–3 items, for outage-chance and repair-time questions.
3. **One confidence meter.** Below 60%, it lists the 2–3 missing data points that would make the answer more certain.

The chat uses the same day or storm as the map. Confidence is a documented data-support score, not a statistical probability (see [ASSUMPTIONS.md](backend/ml/ASSUMPTIONS.md)).

Run the frontend on its own (outside Docker): `cd frontend && npm install && npm run dev`. It needs Node 18+ and expects the API on :8000.

## Common tasks

| Task | Command |
|---|---|
| Stop everything (data is kept) | `docker compose down` |
| Wipe the database and start fresh | `docker compose down -v && docker compose up -d --build` |
| Force a data reload | `FORCE_RELOAD=1 docker compose up loader` |
| Retrain the model (~5 min) | `docker compose run --rm ml` |
| Regenerate the demo cache (uses the LLM if a key is set) | `docker compose run --rm ml python -m demo.build_cache` |
| Score one premise from the CLI | `docker compose run --rm ml python -m ml.score 982542862 --severity high` |
| API logs | `docker compose logs -f api` |
| Web app logs | `docker compose logs -f web` |
| Production build of the web app | `cd frontend && npm run build` (outputs `frontend/dist/`) |

## Troubleshooting

| Problem | Fix |
|---|---|
| `port is already allocated` (5432, 8000 or 5173) | Set `POSTGRES_PORT`, `API_PORT` or `WEB_PORT` in `.env` |
| The map is blank | WebGL must be enabled in your browser; the basemap needs internet access (CARTO tiles) |
| The loader exits with an error | Check `data/` has all the files listed above; then run `docker compose logs loader` |
| `/chat` replies end with "LLM unavailable … answered with the rules-based assistant" | Your key, model name or quota is the problem. Groq retires models: list yours via `https://api.groq.com/openai/v1/models`, then set `GROQ_MODEL` in `.env` |
| You changed `requirements.txt` | `docker compose build` |
| A container is killed or loading is very slow | Give Docker Desktop more memory (≥ 6 GB) |

## Architecture

```mermaid
flowchart LR
  subgraph Data["PostGIS (docker: db)"]
    RAW[(Challenge data\nARCOS, GIS, topology,\nSTORMS labor)]
    PRIV[(customer_private\nlife-support / threat\nbackend-only)]
    RISK[(asset_risk\nper-asset P(damage))]
  end
  ML["ml/ pipeline\nbuild_dataset → train →\nduration → score_assets"] --> RISK
  RAW --> ML
  subgraph API["FastAPI (docker: api)"]
    TOOLS["tools.py\nget_outage_risk · estimate_restoration\nprep_guidance · check_alert_eligibility\ndraft_message"]
    PRIVACY["privacy.py\nredact + audit log"]
    ASSIST["assistant.py\nLLM tool loop · grounding check\nanswer card · offline / demo-safe"]
    STORM["storm.py\ndemo timeline + SSE"]
  end
  RISK --> TOOLS
  RAW --> TOOLS
  PRIV -. escalation priority only .-> TOOLS
  TOOLS --> PRIVACY --> ASSIST
  ASSIST <--> LLM["Groq → Gemini fallback\n(OpenAI-compatible)"]
  STORM --> TOOLS
  CLIENT["React web app\n3D risk map + chat\n(docker: web)"] <--> API
```

- **Risk:**
  - Calibrated logistic regression trained on 35 past storms; P(damage) for 333k assets.
  - Rolled up along each premise's network path.
  - Details: [ASSUMPTIONS.md](backend/ml/ASSUMPTIONS.md), [METRICS.md](backend/ml/artifacts/METRICS.md).
- **Restoration:**
  - STORMS labor hours × repair bundles from AEP structure drawings × an indirect-labor multiplier of 4.5.
  - 2,000-run Monte Carlo with upstream-first step restoration gives a P10/P50/P90 range in hours. No ETA model is trained.
- **Grounding and privacy:**
  - The LLM never sees the premise number or any private field.
  - Every number in its reply must appear in a tool result, or the reply is replaced with the deterministic answer.
  - The answer card is built from tool results, not LLM text.
  - Escalations are always stated by the backend, never left to the LLM.
- **Demo timeline:** the scripted storm's confirmed facts apply only to the demo home (premise `982542862`). Every other address just sees "a storm is forecast".

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` (falls back to Gemini), `gemini`, or `offline` |
| `GROQ_API_KEY` / `GROQ_MODEL` | – / `openai/gpt-oss-120b` | Groq free tier |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-2.0-flash` | Gemini fallback |
| `POSTGRES_PORT`, `API_PORT`, `WEB_PORT` | `5432`, `8000`, `5173` | Host ports |
| `FORCE_RELOAD` | `0` | `1` makes the loader reload all data |

## API

| Endpoint | Purpose |
|---|---|
| `GET /lookup?address=` · `?lat=&lon=` | "Is this your home?" candidates (masked address, overhead/underground) |
| `POST /tools/get_outage_risk` | Risk band, probability, confidence, factors, assumptions |
| `POST /tools/estimate_restoration` | P10/P50/P90 hours, bottleneck, drivers. What-ifs: `crews`, `crew_arrival_delay_hrs`, `extra_damage`, `access_blocked`, `storm_severity` |
| `POST /tools/prep_guidance` | Labeled EMERGENCY / SAFETY / PREPARE actions for **declared** needs |
| `POST /tools/check_alert_eligibility` | Which documented alert rules fire (channel, timing, escalation) |
| `POST /tools/draft_message` | `pre_storm`, `outage_confirmed`, `estimate_updated`, `restored` SMS (≤320 chars) |
| `POST /storm/advance` · `/storm/reset` · `GET /storm/state` · `/storm/events` (SSE) · `/storm/messages` | Demo storm timeline and proactive messages |
| `POST /chat` | `{reply, question_type, what_could_change, confidence: {score, label, missing}, storm, tools_used, mode}`; send `storm: {mode, day}` to match the map and `outage: {...}` during an Outage test |
| `GET /audit` | Tool-call audit log |
| `GET /map/context?premise=&day=` · `&mode=sandbox&intensity=&crews=&duration_days=&radius_m=` | Map payload: homes, poles, equipment, 3D lines, supply path, forecast, your risk (Storm test: simulated storm + restoration) |
| `GET /outages/current?premise=` | The current outage at this home from the outage feed (demo timeline), or `null` |
| `GET /outage-test/devices?premise=` | Equipment on the home's supply path to pick as a fault location |
| `GET /outage-test?premise=&device=` · `&lat=&lon=` · `&cause=&reported_hours_ago=&crews=` | Simulated outage: location, affected area, reported time, ETR range, why it takes that long (read-only, audited) |
| `GET /forecast?premise=` | Live 7-day forecast and the storm size each day implies |
| `GET /health` | Readiness |

## Repository layout
```
backend/
  db/             data loader (shapefiles, topology, labor workbook → PostGIS)
  ml/             risk model pipeline, scorer, duration helper, ASSUMPTIONS.md, artifacts/
  main.py         FastAPI app              tools.py       tool implementations (+ memo cache)
  restoration.py  labor + Monte Carlo      guidance.py    prep / alert rules / messages
  privacy.py      redaction + audit        assistant.py   LLM middleware
  storm.py        demo timeline            lookup.py      address / map lookup
  mapdata.py      map payload            forecast.py    live forecast -> storm size
  outages.py      outage records: fault location, affected area, ETR and why
  confidence.py   confidence score, missing data, what could change the answer
  demo/           scenario.json, DEMO_SCRIPT.md, cache.json, build_cache.py
  tests/          pytest suite
frontend/         React app (Vite)
  src/            RiskMap (deck.gl 2D/3D), meshes.js (generic models), trees.js (OSM trees), ChatPanel, ConfidenceMeter,
                  SandboxPanel (Storm test), OutageTestPanel, OutageCard, Notifications
```

## Known limitations
- **Wire repairs are a lower bound.** The STORMS data has no conductor-restringing hours.
- **The demo storm is scripted.** Sessions and the storm state are in memory, so run a single API worker.
- **LLM wording varies between runs.** Numbers are still grounded (enforced), and safety and escalation lines are added by the backend.
