"""Run the demo script through /chat and save responses for demo-safe mode.

    docker compose run --rm ml python -m demo.build_cache

Uses the configured LLM provider when keys are set (otherwise the offline assistant).
Responses are keyed by (storm step, normalized message), the same key Assistant uses.
"""
import json
import time

from fastapi.testclient import TestClient

import main
from demo.script import BEATS, DEMO_DIR, SESSION

PAUSE_S = 3  # stay under free-tier tokens-per-minute limits when a live LLM is configured


def run() -> dict:
    cache = {}
    with TestClient(main.app) as c:
        assistant = main.STATE["assistant"]
        premise = main.STATE["storm"].config["premise"]
        for beat in BEATS:
            for action in beat["storm"]:
                c.post(f"/storm/{action}")
            if not beat["message"]:
                continue
            body = {"message": beat["message"], "session_id": beat.get("session", SESSION),
                    "premise": beat.get("premise", premise)}
            r = c.post("/chat", json=body).json()
            time.sleep(PAUSE_S)
            cache[assistant.cache_key(beat["message"])] = r
            print(f"beat {beat['beat']} [{r['mode']}] {beat['title']}: tools={r['tools_used']}")
        c.post("/storm/reset")
    (DEMO_DIR / "cache.json").write_text(json.dumps(cache, indent=2))
    print(f"Saved {len(cache)} cached responses to demo/cache.json")
    return cache


if __name__ == "__main__":
    run()
