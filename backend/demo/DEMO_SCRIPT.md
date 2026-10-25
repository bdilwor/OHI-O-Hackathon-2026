# Demo script

## In the browser (http://localhost:5173)

| # | Beat | What to do | What judges see |
|---|---|---|---|
| 0 | Find the home | **try the demo address** → **Yes, this is my home** | Colorful 3D neighborhood: houses with lit windows, trees, poles, sagging wires; supply path with flowing current |
| 1 | Live forecast (**My home**) | Drag the forecast day slider; ask "Will I lose power?" | Real forecast drives the model; answer + "what could change this" + confidence meter |
| 2 | Switch to **Sandbox** | Click the Sandbox tab | Purple SANDBOX banner: simulated, nothing saved |
| 3 | Big storm | Intensity slider → 5 | Rain, houses turn orange/red, windows go dark with red rings; notifications "Outage likely at your home", "Repair estimate longer" |
| 4 | Crews and duration | Crews → 20, then duration → 3 days | Repair estimate shortens, then lengthens (toasts + readout) |
| 5 | Affected area | Shrink the area to 300 m (toggle m/yd) | Only the nearby circle stays red; the rest calms down |
| 6 | Play storm | **▶ Play storm** | The storm ramps up and down over ~15 s, firing notifications |
| 7 | Simulated outage | **Simulate outage at my home** | Outage alert toast + proactive chat message with the repair range |
| 8 | Chat reacts | Ask "How long would repairs take?" | Answer for the simulated storm, what could change it, confidence |
| 9 | Refusal / medical | "Where are the crews right now?" · "I use an oxygen concentrator" | Polite refusal; tailored safety steps + callback eligibility |
| 10 | 2D / 3D | Toggle **2D** | Flat map of the same prediction |

**Point out:** the 🔔 notification history, the **Privacy & audit log** (sandbox calls are tagged `sandbox`), and **Demo-safe mode**.

## API version (curl)

This walks the same beats with `curl` against the running API.

- **Demo home:** premise `982542862`, "7*** RABBIT RUN, ROANOKE". It has overhead service and 2 reclosers plus a fuse upline. The line serving it has 124 past damage records, and there are 41 damage reports within 500 m.
- **Storm:** modeled on the February 2025 storm (`demo/scenario.json`).
- **Automated replay:** `tests/test_demo.py` runs the same beats.

```bash
docker compose up -d --build         # everything; API on http://localhost:8000  (docs: /docs)
API=localhost:8000
chat() { curl -s -X POST $API/chat -H 'content-type: application/json' -d "$1" | python3 -m json.tool; }
```

In a second terminal, keep the proactive-message stream open for the whole demo:

```bash
curl -N localhost:8000/storm/events
```

## 0. Find the home ("Is this your home?")
```bash
curl -s "$API/lookup?address=7110%20Rabbit%20Run,%20Roanoke"
```

## 1. Pre-storm warning (proactive)
```bash
curl -s -X POST $API/storm/reset          # pushes the pre_storm SMS to /storm/events
curl -s $API/storm/messages
```

## 2. "Will I lose power?"
```bash
chat '{"session_id":"demo","premise":"982542862","message":"Will I lose power?"}'
```
**Shows:** a risk band with its probability and confidence, the plain-English factors, and the three answer sections.

## 3. Outage confirmed
```bash
curl -s -X POST $API/storm/advance        # outage -> outage_confirmed SMS with a P10–P90 range
chat '{"session_id":"demo","message":"How long could it last?"}'
```

## 4. Judge what-if: crews delayed
```bash
chat '{"session_id":"demo","message":"What if crews cannot arrive until tomorrow?"}'
```
**Shows:** a new range, plus how much later the most likely time is than the previous estimate. For other what-ifs, call the tool directly:
```bash
curl -s -X POST $API/tools/estimate_restoration -H 'content-type: application/json' \
  -d '{"premise":"982542862","crews":8,"extra_damage":{"Pole":3},"access_blocked":true}'
```

## 5. Declared medical device → escalation
```bash
chat '{"session_id":"demo","message":"I use an oxygen concentrator"}'
```
**Shows:** tailored EMERGENCY guidance, and alert rule R5 firing (a human callback). The assistant doesn't promise to schedule anything.

## 6. "Where are the crews?" → polite refusal
```bash
chat '{"session_id":"demo","message":"Where are the crews right now?"}'
```

## 7. Unknown location → fallback
```bash
chat '{"session_id":"other","premise":"999","message":"Will I lose power?"}'
```

## 8. Restored
```bash
curl -s -X POST $API/storm/advance; curl -s -X POST $API/storm/advance; curl -s -X POST $API/storm/advance
chat '{"session_id":"demo","message":"Is my power back?"}'
```

## Show the controls
```bash
curl -s "$API/audit?limit=10"     # every tool call: masked input, returned fields, redacted fields
```

## Demo-safe mode
Add `"demo_safe": true` to any `/chat` body to serve the cached responses in `demo/cache.json`. Rebuild the cache with `docker compose run --rm ml python -m demo.build_cache`. It uses the live LLM when keys are set, otherwise the offline assistant.
