"""Project claims: one work loop per project at a time (several agents share one machine).

An agent claims a project when it starts a loop round (`renes round start <kb> --agent NAME`, or
the RENES_AGENT environment variable). While the claim is fresh (seen within TTL), another agent's
round start is refused unless it passes --force; a stale claim is taken over with a note. Rounds
and activity rows record the agent, so the dashboard shows who did what.

Reading a project (status, next, the dashboard) never needs a claim; only the loop does.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from .kb import KB

TTL = timedelta(hours=2)
SETTING = "claim"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def agent_name(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("RENES_AGENT") or None


def owner(kb: KB) -> dict | None:
    """{"agent", "since", "seen", "fresh", "age_minutes"} or None when unclaimed."""
    raw = kb.setting(SETTING)
    if not raw:
        return None
    c = json.loads(raw)
    age = _now() - datetime.fromisoformat(c["seen"])
    return {**c, "fresh": age < TTL, "age_minutes": int(age.total_seconds() // 60)}


def claim(kb: KB, agent: str | None, force: bool = False) -> tuple[bool, str]:
    """Take or refresh the claim. (ok, message); not ok when another agent holds a fresh claim."""
    current = owner(kb)
    now = _now().isoformat(timespec="seconds")
    if current and current["agent"] != agent and current["fresh"] and not force:
        return False, (f"this project is claimed by {current['agent']} (last seen {current['age_minutes']} min ago). "
                       f"Pick another project, or pass --force if that agent has stopped.")
    if agent is None:
        if current and current["fresh"]:
            return True, f"warning: running unnamed while {current['agent']} holds the claim (pass --agent NAME)"
        return True, "warning: no agent name (pass --agent NAME or set RENES_AGENT) so the project stays unclaimed"
    note = ""
    if current and current["agent"] != agent:
        note = (f" (took over from {current['agent']}, " + ("forced" if current["fresh"] else
                f"idle {current['age_minutes']} min") + ")")
    since = current["since"] if current and current["agent"] == agent else now
    kb.set_setting(SETTING, json.dumps({"agent": agent, "since": since, "seen": now}))
    return True, f"claimed by {agent}{note}"


def release(kb: KB, agent: str | None, force: bool = False) -> tuple[bool, str]:
    current = owner(kb)
    if current is None:
        return True, "not claimed"
    if current["agent"] != agent and not force:
        return False, f"claimed by {current['agent']}, not {agent or '(unnamed)'}: pass --force to release it"
    kb.db.execute("DELETE FROM settings WHERE key=?", (SETTING,))
    return True, f"released (was {current['agent']})"


def describe(c: dict | None) -> str:
    if c is None:
        return "unclaimed"
    return f"{c['agent']}, last seen {c['age_minutes']} min ago" + ("" if c["fresh"] else " (stale: free to take)")
