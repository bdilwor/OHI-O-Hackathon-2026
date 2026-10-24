# Demo script: API version

This walks the 8 demo beats with `curl` against the running API. A UI version will follow with the frontend.

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
