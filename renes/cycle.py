"""The work loop's decision: which stage should run next (used by the /renes-cycle skill).

The rules are deterministic, so a long `/loop /renes-cycle` behaves predictably:

  research    no game brief yet (once per game: manual, walkthroughs, mechanics)
  benchmark   a reference is configured and 40+ routines were annotated since the last benchmark
  evidence    20+ annotated routines have no traces/knock-out screenshots for the review page
  experiment  25+ routines annotated since the last experiment and 5+ untested RAM candidates
  annotate    routines are ready (all their callees are annotated)
  explore     nothing is ready and the remaining routines never ran: coverage is the blocker
  review      nothing left the loop can do alone: the human reviews candidates and open questions

Each round is recorded (`round start` / `round end`) with the project metrics before and after, so
the loop also stops when it stalls: the same stage made no progress twice in a row, or no round
made progress three times in a row. A human review decision (or `round resume`) lifts the stop.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import annotate
from .kb import KB, NON_ROM

BENCHMARK_EVERY = 40
EXPERIMENT_EVERY = 25
MIN_UNTESTED = 5
EVIDENCE_BACKLOG = 20
STALL_SAME = 2         # rounds of one stage without progress before the loop stops
STALL_ANY = 3          # rounds of any stages without progress before the loop stops

# Metrics compared before/after a round; any increase counts as progress.
METRICS = {"annotated": "routines annotated", "touched": "annotations revised", "code_bytes": "code bytes executed",
           "ram_verified": "RAM labels verified", "experiments": "experiments run", "evidence": "routines with evidence",
           "benchmarks": "benchmarks run", "brief": "research facts", "approved": "labels approved",
           "probes": "jump tables probed", "tiles": "tile sheets exported", "audits": "audits recorded"}


@dataclass
class Status:
    annotated: int
    routines: int
    ready: int
    since_experiment: int
    since_benchmark: int | None          # None = never benchmarked
    untested: list[tuple[str, int, int]] = field(default_factory=list)   # (name, addr, accesses)
    unreached_routines: int = 0
    researched: bool = True
    brief_pending: int = 0                # research facts the human has not reviewed yet
    without_evidence: int = 0
    claim: str = "unclaimed"                            # which agent runs this project's loop (claims.py)
    finish: list | None = None                          # finishing checks, once annotation is complete
    unprobed: list[str] = field(default_factory=list)   # jump tables with a simple index never shown on screen
    tiles_missing: bool = False                         # CHR ROM tiles or metatiles not exported as images yet
    unreached_ready_blockers: int = 0
    idle_rounds: list[str] = field(default_factory=list)   # stages of the latest rounds without progress, newest first
    recommendation: str = ""
    reason: str = ""


def _count_since(kb: KB, when: str | None) -> int:
    if when is None:
        return kb.db.execute("SELECT count(*) FROM routine_notes WHERE bank>=0").fetchone()[0]
    return kb.db.execute("SELECT count(*) FROM routine_notes WHERE bank>=0 AND updated > ?", (when,)).fetchone()[0]


def status(kb: KB, has_reference: bool = False) -> Status:
    p = annotate.progress(kb)
    last_exp = kb.db.execute("SELECT max(created) FROM experiments").fetchone()[0]
    benches = kb.benchmarks()
    since_bench = _count_since(kb, benches[-1][1]) if benches else None

    tested = {row[0] for row in kb.db.execute("SELECT DISTINCT addr FROM experiments WHERE bank=?", (NON_ROM,))}
    reads = kb.counters("nesInternalRam", "readCount") or []
    writes = kb.counters("nesInternalRam", "writeCount") or []
    untested = []
    for l in kb.labels(NON_ROM):
        if l.kind == "ram" and l.status == "candidate" and l.addr not in tested and l.addr < len(reads) \
                and not l.name.startswith("Scratch"):
            untested.append((l.name, l.addr, reads[l.addr] + writes[l.addr]))
    # Variables many routines write come first: names across the code depend on them.
    ctxs = annotate.contexts(kb)
    writers = {}
    for c in ctxs:
        for addr, who in annotate.ram_writers(c).items():
            writers.setdefault(addr, set()).update((c.bank, e) for e in who)
    untested.sort(key=lambda t: (-len(writers.get(t[1], ())), -t[2]))

    rom = kb.rom()
    unreached = [(c.bank, e) for c in ctxs for e in c.routines
                 if e not in c.notes and c.cdl is not None and not c.executed(e)]

    s = Status(p["annotated"], p["routines"], p["ready_now"], _count_since(kb, last_exp), since_bench,
               untested, len(unreached))
    s.researched = bool(kb.db.execute("SELECT count(*) FROM brief").fetchone()[0] or kb.setting("research_done"))
    s.brief_pending = kb.db.execute("SELECT count(*) FROM brief WHERE status IS NULL OR status='proposed'").fetchone()[0]
    with_evidence = set(kb.db.execute("SELECT DISTINCT bank, entry FROM knockouts WHERE bank>=0"))
    # the evidence playback never reaches these: another evidence round can't help them
    from .evidence import unreached_entries
    unreachable = unreached_entries(kb)
    s.without_evidence = sum(1 for c in ctxs for e in c.notes if (c.bank, e) not in with_evidence
                             and (c.bank, e) not in unreachable and c.executed(e))
    from . import visual
    done = visual.probed_sites(kb) | set(visual.skipped_sites(kb))
    names = {l.addr: l.name for l in kb.labels(NON_ROM)}
    # Indexed tables (object types) too: `visual probe` picks a slot that runs them.
    s.unprobed = [f"${d.site:04X} ({names.get(d.index_addr, f'${d.index_addr:04X}')}"
                  + (f",{d.indexed}" if d.indexed else "") + f", {len(d.handlers)} entries)"
                  for c in ctxs for d in visual.dispatches(c) if d.index_addr is not None and d.site not in done]
    s.tiles_missing = len(rom.chr) >= 0x2000 and (not kb.setting("tiles_exported") or kb.setting("metatiles") is None)
    from . import claims
    s.claim = claims.describe(claims.owner(kb))
    s.idle_rounds = idle_rounds(kb)
    if s.annotated >= s.routines or (not s.ready and not s.unreached_routines):
        from . import finish
        s.finish = finish.checklist(kb, s)
    s.recommendation, s.reason = decide(s, has_reference)
    return s


def decide(s: Status, has_reference: bool) -> tuple[str, str]:
    stage, reason = _choose(s, has_reference)
    if stage in ("review", "done"):
        return stage, reason
    same = 0
    for st in s.idle_rounds:
        if st != stage:
            break
        same += 1
    if same >= STALL_SAME:
        return "review", (f"stopped: the last {same} {stage} rounds made no progress ({reason}). "
                          "Check why, then review something or run `round resume`")
    if len(s.idle_rounds) >= STALL_ANY:
        return "review", (f"stopped: the last {len(s.idle_rounds)} rounds made no progress "
                          f"({', '.join(s.idle_rounds)}). Check why, then review something or run `round resume`")
    return stage, reason


def _choose(s: Status, has_reference: bool) -> tuple[str, str]:
    if not s.researched:
        return "research", "no game brief yet: research the game once (manual, walkthroughs)"
    if s.brief_pending:
        return "review", (f"{s.brief_pending} research facts await your review (dashboard, Research tab) "
                          "before they enter the loop")
    if s.tiles_missing or s.unprobed:
        return "probe", (("tile sheets or metatiles not exported; " if s.tiles_missing else "")
                         + (f"{len(s.unprobed)} jump tables never shown on screen" if s.unprobed else "")).strip("; ")
    if has_reference and (s.since_benchmark is None or s.since_benchmark >= BENCHMARK_EVERY):
        return "benchmark", ("never benchmarked" if s.since_benchmark is None
                             else f"{s.since_benchmark} routines annotated since the last benchmark")
    if s.since_experiment >= EXPERIMENT_EVERY and len(s.untested) >= MIN_UNTESTED:
        return "experiment", (f"{s.since_experiment} routines annotated since the last experiment; "
                              f"{len(s.untested)} untested RAM candidates")
    if s.without_evidence >= EVIDENCE_BACKLOG:
        return "evidence", f"{s.without_evidence} annotated routines have no screenshots/traces for review"
    if s.ready:
        return "annotate", f"{s.ready} routines ready"
    if s.annotated < s.routines and s.unreached_routines:
        return "explore", f"nothing ready; {s.unreached_routines} unannotated routines never ran"
    if s.finish is not None:                    # annotation complete: work through the finishing checks
        failing = next((c for c in s.finish if not c.ok), None)
        if failing is None:
            return "done", "every finishing check passes: export the project and hand it over"
        if failing.todo in ("experiment", "evidence", "audit", "annotate", "probe"):
            return failing.todo, f"finishing ({failing.name}): {failing.detail}"
        return "review", f"finishing ({failing.name}) needs a person: {failing.detail}"
    return "review", "nothing the loop can do alone: review candidates, failed experiments and open questions"


def format_status(s: Status) -> str:
    out = [f"loop claim: {s.claim}",
           f"annotated {s.annotated}/{s.routines} routines, {s.ready} ready now",
           f"since last experiment: {s.since_experiment} routines annotated",
           f"since last benchmark: {'never' if s.since_benchmark is None else f'{s.since_benchmark} routines annotated'}",
           f"unannotated routines that never ran: {s.unreached_routines}",
           f"annotated routines without review evidence: {s.without_evidence}",
           f"game brief: {'not researched yet' if not s.researched else f'{s.brief_pending} facts awaiting review' if s.brief_pending else 'reviewed'}",
           f"jump tables to probe: {', '.join(s.unprobed) or 'none'}" + ("; tile sheets or metatiles not exported" if s.tiles_missing else ""),
           f"untested RAM candidates: {len(s.untested)}"]
    for name, addr, n in s.untested[:8]:
        out.append(f"    ${addr:04X} {name}  ({n} accesses)")
    if s.finish is not None:
        from .finish import format_checklist
        out += ["finishing checks:", format_checklist(s.finish).rstrip()]
    out += ["", f"NEXT: {s.recommendation} ({s.reason})"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------
# Rounds: what each pass of the loop did, how long it took and what it changed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot(kb: KB) -> dict:
    one = lambda sql: kb.db.execute(sql).fetchone()[0]
    cdl = kb.coverage("nesPrgRom", "cdl") or b""
    return {"annotated": one("SELECT count(*) FROM routine_notes WHERE bank>=0"),
            "code_bytes": sum(1 for f in cdl if f & 1),
            "ram_verified": sum(1 for l in kb.labels(NON_ROM) if l.kind == "ram" and l.status == "verified"),
            "experiments": one("SELECT count(*) FROM experiments"),
            "evidence": one("SELECT count(DISTINCT entry) FROM knockouts"),
            "benchmarks": one("SELECT count(*) FROM benchmarks"),
            "brief": one("SELECT count(*) FROM brief"),
            "approved": one("SELECT count(*) FROM labels WHERE status='approved'"),
            "probes": one("SELECT count(DISTINCT ref) FROM media WHERE kind='probe'"),
            "tiles": 1 if kb.setting("tiles_exported") else 0,
            "audits": _count_audits(kb)}


def _count_audits(kb: KB) -> int:
    from .audit import SCHEMA
    kb.db.execute(SCHEMA)
    return kb.db.execute("SELECT count(*) FROM audits").fetchone()[0]


def gains(before: dict, after: dict) -> dict:
    """Metrics that went up during a round (the round's progress)."""
    return {k: after[k] - before.get(k, 0) for k in METRICS if after.get(k, 0) > before.get(k, 0)}


def end_round(kb: KB, note: str = "") -> dict | None:
    """Close the open round: record the metrics after it. Returns the round, or None if none was open."""
    row = kb.db.execute("SELECT id, started, before FROM rounds WHERE finished IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    from . import confidence, rework
    rework.scan(kb)                   # experiments and renames this round may make weak names fixable
    confidence.refresh(kb)
    after = snapshot(kb)
    # Re-annotating routines (rework) keeps the count but is progress too: count updates beyond the new ones.
    updated = kb.db.execute("SELECT count(*) FROM routine_notes WHERE bank>=0 AND updated > ?", (row[1],)).fetchone()[0]
    after["touched"] = max(0, updated - (after["annotated"] - json.loads(row[2])["annotated"]))
    finished = _now()
    kb.db.execute("UPDATE rounds SET finished=?, after=?, note=? WHERE id=?", (finished, json.dumps(after), note, row[0]))
    from . import maps
    maps.record_snapshot(kb, row[0], finished)    # per-byte map state, for replaying the maps later
    kb.commit()
    return rounds(kb, 1)[0]


def start_round(kb: KB, s: Status, agent: str | None = None) -> int:
    """Record a new round for the stage `s` recommends. An unclosed earlier round is closed first."""
    end_round(kb, "not closed by the loop")
    before = snapshot(kb)
    before["touched"] = 0
    rid = kb.db.execute("INSERT INTO rounds (stage, reason, started, before, agent) VALUES (?,?,?,?,?)",
                        (s.recommendation, s.reason, _now(), json.dumps(before), agent)).lastrowid
    kb.commit()
    return rid


def rounds(kb: KB, limit: int = 20) -> list[dict]:
    out = []
    for rid, stage, reason, started, finished, before, after, note in kb.db.execute(
            "SELECT id, stage, reason, started, finished, before, after, note FROM rounds ORDER BY id DESC LIMIT ?",
            (limit,)):
        seconds = None
        if finished:
            seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
        out.append({"id": rid, "stage": stage, "reason": reason, "started": started, "finished": finished,
                    "seconds": seconds, "note": note,
                    "gains": gains(json.loads(before), json.loads(after)) if after else None})
    return out


def idle_rounds(kb: KB) -> list[str]:
    """Stages of the latest finished rounds that made no progress, newest first.

    Review rounds don't count (the loop stops there anyway). A human review decision or
    `round resume` after a round wipes the slate: the human has changed something.
    """
    marks = [kb.db.execute("SELECT max(created) FROM reviews").fetchone()[0], kb.setting("loop_resumed")]
    since = max((m for m in marks if m), default="")
    idle = []
    for r in rounds(kb, 12):
        if r["finished"] is None or r["stage"] == "review":
            continue
        if r["gains"] or r["started"] < since:
            break
        idle.append(r["stage"])
    return idle


def format_round(r: dict) -> str:
    took = "open" if r["seconds"] is None else f"{r['seconds'] / 60:.1f} min"
    if r["gains"] is None:
        made = "in progress"
    else:
        made = ", ".join(f"+{n} {METRICS[k]}" for k, n in r["gains"].items()) or "no progress"
    return f"#{r['id']} {r['stage']:<10} {took:>9}  {made}" + (f"  ({r['note']})" if r["note"] else "")
