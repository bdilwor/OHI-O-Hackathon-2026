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
| Tests | ✅ 95 tests (data, model, DB link, restoration, API, privacy, assistant, demo, live LLM) |
| React frontend | ⏳ Next |

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

   Later runs skip the loading and are ready in seconds.
4. **Check it's up.** The API is ready once `/health` returns `{"ok": true, ...}`:
   ```bash
   docker compose ps                           # loader: "Exited (0)", api: "running"
   curl localhost:8000/health
   ```
   Interactive API docs: http://localhost:8000/docs

## Test it

**A. Full automated suite** (~30 s):
```bash
docker compose run --rm ml pytest -q tests
```
Expected: `95 passed`. If no `GROQ_API_KEY` is set, the live-LLM test is reported as skipped. The suite covers:
- data loading and row counts
- training quality and no data leakage between storms
- the model's link to the database
- restoration behavior (crews, delays, extra damage)
- every API endpoint
- privacy (no Life Support/THREAT flag, IDs or coordinates in any output)
- the assistant's routing, refusals, fallbacks and grounding check
- the full 8-beat demo

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

**D. See the controls:** `curl "localhost:8000/audit?limit=10"` shows every tool call, with a masked input, the fields returned and the fields redacted.

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

## Troubleshooting

| Problem | Fix |
|---|---|
| `port is already allocated` (5432 or 8000) | Set `POSTGRES_PORT=5433` or `API_PORT=8001` in `.env` |
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
  CLIENT["Client (curl now,\nReact frontend next)"] <--> API
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
| `POSTGRES_PORT`, `API_PORT` | `5432`, `8000` | Host ports |
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
| `POST /chat` | `{reply, card: {confirmed, estimate, actions}, tools_used, fallback, mode}` |
| `GET /audit` | Tool-call audit log |
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
  demo/           scenario.json, DEMO_SCRIPT.md, cache.json, build_cache.py
  tests/          pytest suite
frontend/         (next)
```

## Known limitations
- **Wire repairs are a lower bound.** The STORMS data has no conductor-restringing hours.
- **The demo storm is scripted.** Sessions and the storm state are in memory, so run a single API worker.
- **LLM wording varies between runs.** Numbers are still grounded (enforced), and safety and escalation lines are added by the backend.
