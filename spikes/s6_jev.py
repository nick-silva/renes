"""M0 spike S6: one Jev call with a mock game state and a macro-action menu.

Uses plain HTTP (no SDK) so there are no dependencies. Logs latency and token usage.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
URL = "https://api.typesafe.ai/v1/systemone"


def api_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("TYPESAFE_API_KEY missing from .env")


def main() -> None:
    body = {
        "model": "jev-latest",
        "state": {
            "goal": "Explore the game to reach code and screens not seen before. Winning is not required.",
            "player": {"x": 284, "y": 176, "on_ground": True},
            "nearest_enemy": {"kind": "unknown_walker", "dx": 60, "dy": 0, "moving": "toward_player"},
            "obstacle_ahead": {"kind": "wall", "dx": 96, "height_tiles": 2},
            "recent_actions": ["walk_right", "walk_right", "run_right"],
            "new_code_bytes_last_action": 38,
        },
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": "Pick the macro-action most likely to survive and reach new parts of the game.",
                "criteria": {
                    "walk_right": "Hold right for 30 frames",
                    "run_jump_right": "Hold right+B and press A, a long running jump",
                    "short_hop": "Tap A for a small jump in place",
                    "walk_left": "Hold left for 30 frames",
                    "wait": "Press nothing for 30 frames",
                    "restore_checkpoint": "Return to the last savestate",
                },
            },
            "is_stuck": {
                "type": "noul",
                "instructions": "Is the explorer repeating itself without making progress?",
                "criteria": {"true": "Same actions with no new code reached", "false": "Still reaching new code"},
            },
        },
    }
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
    )
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read().decode(errors="replace")
    ms = (time.perf_counter() - t) * 1000

    print(f"HTTP {status} in {ms:.0f} ms")
    print(json.dumps(payload, indent=2) if isinstance(payload, dict) else payload)
    OUT.mkdir(exist_ok=True)
    (OUT / "s6_jev.json").write_text(json.dumps({"status": status, "ms": ms, "response": payload}, indent=2))


if __name__ == "__main__":
    main()
