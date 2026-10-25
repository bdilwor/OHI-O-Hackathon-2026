# Demo script

## In the browser (http://localhost:5173)

Two portals (top left): **Customer** and **AEP Operations**. The judging story is: the customer gets a personal, friendly answer; AEP staff see the whole area and get a professional briefing from the same model.

| # | Beat | What to do | What judges see |
|---|---|---|---|
| 0 | Find the home | **try the demo address** → **Yes, this is my home** | Colorful 3D neighborhood: houses with lit windows, trees, poles, sagging wires; supply path with flowing current |
| 1 | Live forecast (**My home**) | Drag the forecast day slider; ask "Will I lose power?" | Real forecast drives the model; a friendly answer + "what could change this" + confidence meter |
| 2 | My plan | Open **My plan**; tap *Powered medical device* in the chat | Labeled EMERGENCY / SAFETY / PREPARE steps update; alert eligibility (pre-storm SMS + check-in call) |
| 3 | Outside AEP's area | **Change** → search "Columbus, Ohio" → **Get a regional estimate** | Regional estimate from the live forecast there and similar AEP-area homes, Low confidence, and an honest "outside AEP's data" note |
| 4 | **How it works** | Click the tab | Pipeline, metrics, questions we asked, what didn't work (incl. the FNO and physics tests), 10x scale |
| 5 | **AEP Operations → Operations** | Switch portal; **Storm** tab → **Simulated storm**, intensity → 5; area crews 300 | KPIs jump (expected damage, customers out, high-risk circuits, crew-hours, crews for 24/72 h, facilities at risk); circuit risk map; hospitals / fire / ambulance with suggested actions; a "Priority facilities" toast; SMS previews + alert rules |
| 6 | 3D + dark | Map **3D**, then **☾ Dark** | Circuits become columns sized by expected damage; the basemap goes dark on every map |
| 7 | **Test a home** | **Test a home →** (the demo home is already chosen) | Flies into the home: rain, houses orange/red, windows dark with red rings, under the same storm |
| 8 | Two voices | **Chat** tab: "What is the outage risk for this premise?" | A concise professional briefing, same numbers as the customer's answer in beat 1 |
| 9 | Crews, duration, area | **Storm** tab: crews → 20, duration → 3 days, area → 300 m; **▶ Play storm** | Repair estimate moves; only the nearby circle stays red; the storm ramps up and down |
| 10 | **Outage test** tab | Cause "Tree on the power line"; **Start outage** | Red affected-area polygon + fault pin; outage panel: where, ~N homes / km² / streets, reported and estimated fix (day, month, year), why it takes ~X h |
| 11 | Move the fault | Pick the recloser, then click a pole elsewhere on the map | The affected area grows with the recloser; a pole on another branch may leave the home unaffected |
| 12 | Chat + restore | Ask "How long would restoration take?"; drag crews; **Restore power**; **← Back to area** | Briefing uses the outage's dates; more crews → earlier fix; "Power restored" toast, path turns green |
| 13 | Refusal / medical | "Where are the crews right now?" · "I use an oxygen concentrator" | Polite refusal; tailored safety steps + callback eligibility |

**Point out:** the 🔔 notification history, the **Privacy & audit log** (simulated-storm calls are tagged `sandbox`, outage-test calls `outage_test`, area calls `ops`), and switching tabs is instant, and **Demo-safe mode**.

**Real outage feed on My home:** `curl -X POST localhost:8000/storm/advance` (the outage is confirmed on the next beat; `/storm/reset` clears it). The outage panel appears on **My home** with the timeline's damage and crews.

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
