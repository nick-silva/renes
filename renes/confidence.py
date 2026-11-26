"""Evidence and confidence (backlog item 8).

Two numbers per annotated routine and RAM label:

  evidence   how much of it the tools have checked (below). Shown next to the claim so
             overconfidence is visible, and tells the loop what to verify next.
  effective  the confidence used to rank review: the annotator's claim, discounted when the name
             is incoherent (placeholder parts, or a role only unlabeled RAM could hold), raised to
             0.9 when verified and 1.0 when approved.

Measured on the benchmark game against its reference (token similarity, 452 routine and 211 RAM names): the
claim does rank routine names (r = 0.27; matches 2% in the bottom third vs 14% in the top), the
evidence score alone does not (r = 0.11; "ran"/"traced" say nothing about the name), and the
coherence discount helps (r = 0.33). For RAM the evidence's top third (mostly verified labels)
matches more often than the claim's (56% vs 47%). Hence two numbers rather than one.

Routine (max 0.9 before human review):
  ran       0.20 x share of its instructions executed during play
  traced    0.10  one real call recorded instruction by instruction
  knocked   0.05  knocked out (patched to return) in the evidence run
  effect    0.10  ...and the knock-out changed the screen or RAM
  verified  0.15  touches RAM whose meaning an experiment verified
  context   0.10 x share of callers and callees annotated
  coherent  0.10  name has no placeholder parts and claims no role only unlabeled RAM could hold
  pictured  0.10  in a jump table whose states were probed on screen
RAM label (max 0.9):
  experiment 0.40 a passed experiment (a failed one subtracts 0.20)
  used       0.10 read and written during play
  writers    0.15 x share of the routines that write it annotated
  coherent   0.15 x share of the annotated routines using it whose summary names it
  pictured   0.10 probed: its values were shown on screen
Verified labels score at least 0.9, approved ones 1.0.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from .analyze import PRG_BANK
from .annotate import (ABSOLUTE_MODES, IND, ZEROPAGE_MODES, Context, _instruction_starts, contexts, is_ram,
                       ram_writers, unbacked_roles)
from .kb import KB, NON_ROM
from .opcodes import OPCODES, operand_value

ROUTINE_WEIGHTS = {"ran": 0.20, "traced": 0.10, "knocked": 0.05, "effect": 0.10, "verified": 0.15,
                   "context": 0.10, "coherent": 0.10, "pictured": 0.10}
RAM_WEIGHTS = {"experiment": 0.40, "used": 0.10, "writers": 0.15, "coherent": 0.15, "pictured": 0.10}
FAILED_EXPERIMENT = -0.20
CAP = 0.9
LOW = 0.3            # below this: "low evidence"
OVERCONFIDENT = 0.4  # claimed confidence this far above the evidence
INCOHERENT = 0.6     # factor on the claim for a routine name that isn't coherent


def ram_access(ctx: Context) -> dict[int, set[int]]:
    """RAM address -> routines whose code reads or writes it (static operands)."""
    access: dict[int, set[int]] = defaultdict(set)
    for entry, (last, _) in ctx.routines.items():
        for pc in _instruction_starts(ctx, entry, last):
            op = OPCODES[ctx.prg[pc - ctx.base]]
            value = operand_value(op, ctx.prg, pc - ctx.base)
            if value is not None and op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES and op.mode != IND and is_ram(value):
                access[value].add(entry)
    return access


def _score(factors: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights[k] * v for k, v in factors.items() if k in weights)


def routine_factors(ctx: Context, entry: int, *, labels, ram_labels, traced, knockouts, touched,
                    pictured) -> dict[str, float]:
    from .rework import placeholder_parts

    last = ctx.routines[entry][0]
    starts = _instruction_starts(ctx, entry, last)
    callers = {c for c, _, k in ctx.callers.get(entry, []) if k != "fall"}
    callees = {c for c, _, _ in ctx.callees.get(entry, [])}

    def share(group: set[int]) -> float:
        return sum(1 for c in group if c in ctx.notes) / len(group) if group else 1.0

    name = labels[entry].name if entry in labels else ""
    claims, _ = unbacked_roles(ctx, entry, name, ram_labels)
    ko = knockouts.get(entry)
    return {
        "ran": round(sum(ctx.executed(pc) for pc in starts) / len(starts), 2) if starts else 0.0,
        "traced": float(entry in traced),
        "knocked": float(ko is not None),
        "effect": float(bool(ko)),
        "verified": float(any(ram_labels[a].status in ("verified", "approved") for a in touched.get(entry, ())
                              if a in ram_labels)),
        "context": round((share(callers) + share(callees)) / 2, 2),
        "coherent": float(bool(name) and not placeholder_parts(name) and not claims),
        "pictured": float(entry in pictured),
    }


def ram_factors(notes: dict, addr: int, label, *, access, writers, experiments, reads, writes, floor,
                probed) -> dict[str, float]:
    """notes, access and writers are keyed by routine as (bank, entry): RAM is shared by every bank."""
    runs = experiments.get(addr, [])
    users = [e for e in access.get(addr, ()) if e in notes]
    who = writers.get(addr, set())
    return {
        "experiment": 1.0 if any(runs) else (FAILED_EXPERIMENT / RAM_WEIGHTS["experiment"] if runs else 0.0),
        "used": float(addr < len(reads) and reads[addr] > floor and writes[addr] > 0),
        "writers": round(sum(1 for e in who if e in notes) / len(who), 2) if who else 0.0,
        "coherent": round(sum(1 for e in users if label.name in notes[e]) / len(users), 2) if users else 0.0,
        "pictured": float(addr in probed),
    }


def _final(score: float, status: str) -> float:
    score = min(CAP, max(0.0, score))
    if status == "approved":
        return 1.0
    if status == "verified":
        return max(score, CAP)
    return round(score, 2)


def effective(claimed: float | None, status: str, coherent: bool = True) -> float:
    """Confidence for ranking: the claim (0.5 if none), discounted when incoherent, raised by review."""
    if status == "approved":
        return 1.0
    if status == "verified":
        return CAP
    value = 0.5 if claimed is None else claimed
    return round(min(CAP, value * (1.0 if coherent else INCOHERENT)), 2)


def compute(kb: KB, ctx: Context | None = None) -> list[tuple[int, int, float, float | None, float, dict]]:
    """(bank, addr, score, claimed, effective, factors) for every annotated routine and candidate+ RAM label."""
    ram_labels = {l.addr: l for l in kb.labels(NON_ROM) if l.kind == "ram"}
    out = []
    access: dict[int, set[tuple[int, int]]] = defaultdict(set)
    writers: dict[int, set[tuple[int, int]]] = defaultdict(set)
    notes = {}
    for c in ([ctx] if ctx else contexts(kb)):
        out += _routine_rows(kb, c, ram_labels)
        for addr, who in ram_access(c).items():
            access[addr] |= {(c.bank, e) for e in who}
        for addr, who in ram_writers(c).items():
            writers[addr] |= {(c.bank, e) for e in who}
        notes.update({(c.bank, e): s for e, s in c.notes.items()})
    experiments: dict[int, list[bool]] = defaultdict(list)
    for _, _, bank, addr, _, passed in kb.experiments():
        if bank == NON_ROM and addr is not None:
            experiments[addr].append(bool(passed))
    reads = kb.counters("nesInternalRam", "readCount") or []
    writes = kb.counters("nesInternalRam", "writeCount") or []
    floor = min(reads[:0x800]) if reads else 0
    probed = {a for (a,) in kb.db.execute("SELECT DISTINCT addr FROM media WHERE kind='probe'")}

    for addr, label in sorted(ram_labels.items()):
        if label.status in ("builtin", "auto"):
            continue
        f = ram_factors(notes, addr, label, access=access, writers=writers, experiments=experiments, reads=reads,
                        writes=writes, floor=floor, probed=probed)
        out.append((NON_ROM, addr, _final(_score(f, RAM_WEIGHTS), label.status), label.confidence,
                    effective(label.confidence, label.status), f))
    return out


def _routine_rows(kb: KB, ctx: Context, ram_labels: dict) -> list[tuple]:
    """compute() rows for the annotated routines of one bank."""
    from . import visual

    bank = ctx.bank
    labels = {l.addr: l for l in kb.labels(bank)}
    claimed = {e: c for e, _, _, _, c in kb.routine_notes(bank)}
    traced = {e for (e,) in kb.db.execute("SELECT entry FROM traces WHERE bank=?", (bank,))}
    knockouts = {}
    for entry, differs, effect in kb.db.execute("SELECT entry, screen_differs, effect FROM knockouts WHERE bank=? "
                                                "ORDER BY id", (bank,)):
        e = json.loads(effect)
        knockouts[entry] = bool(differs or e.get("labeled") or e.get("other_bytes"))
    touched: dict[int, set[int]] = defaultdict(set)
    for addr, who in ram_access(ctx).items():
        for e in who:
            touched[e].add(addr)
    probed_sites = visual.probed_sites(kb)
    pictured = set()
    for d in visual.dispatches(ctx):
        if d.site in probed_sites:
            pictured |= set(d.handlers) | {d.owner}
    out = []
    for entry in sorted(e for e in ctx.notes if e in ctx.routines):   # notes can outlive a re-analysis
        f = routine_factors(ctx, entry, labels=labels, ram_labels=ram_labels, traced=traced, knockouts=knockouts,
                            touched=touched, pictured=pictured)
        status = labels[entry].status if entry in labels else "candidate"
        out.append((bank, entry, _final(_score(f, ROUTINE_WEIGHTS), status), claimed.get(entry),
                    effective(claimed.get(entry), status, bool(f["coherent"])), f))
    return out


def refresh(kb: KB, ctx: Context | None = None) -> dict:
    """Recompute and store every score (table `evidence`). Returns a summary for printing."""
    rows = compute(kb, ctx)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kb.db.execute("DELETE FROM evidence")
    kb.db.executemany("INSERT INTO evidence (bank, addr, score, claimed, effective, factors, updated) "
                      "VALUES (?,?,?,?,?,?,?)", [(b, a, s, c, e, json.dumps(f), now) for b, a, s, c, e, f in rows])
    scores = [r[2] for r in rows]
    return {"scored": len(rows),
            "low": sum(1 for s in scores if s < LOW),
            "overconfident": sum(1 for _, _, s, c, _, _ in rows if c is not None and c - s >= OVERCONFIDENT),
            "incoherent": sum(1 for b, *_, f in rows if b != NON_ROM and not f["coherent"]),
            "mean": round(sum(scores) / len(scores), 2) if scores else None}


def scores(kb: KB, bank: int) -> dict[int, tuple[float, float | None, float, dict]]:
    """addr -> (evidence, claimed, effective, factors) as last stored; empty before the first refresh."""
    return {a: (s, c, e, json.loads(f)) for a, s, c, e, f in
            kb.db.execute("SELECT addr, score, claimed, effective, factors FROM evidence WHERE bank=?", (bank,))}


def describe(factors: dict[str, float]) -> str:
    """Short text of the factors that are present, for work units and the dashboard."""
    parts = [f"{k} failed" if v < 0 else k if v == 1 else f"{k} {v:.0%}" for k, v in factors.items() if v]
    return ", ".join(parts) or "no evidence"
