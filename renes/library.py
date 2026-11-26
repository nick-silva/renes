"""Same code in other games (cross-project hints).

Games from the same developer and era share code: controller reading, NMI housekeeping, BCD
maths, sound drivers, random numbers. When a routine here has the same code as a routine
another project already annotated, its name, summary and RAM usage are a strong hint.

A routine's signature is its instruction sequence with addresses abstracted: opcodes, immediate
values and branch offsets stay; hardware registers ($2000-$401F) stay; RAM operands become
"the n-th RAM address this routine uses"; ROM operands (calls, jumps, tables) become "some ROM".
Matching signatures pair the RAM addresses too, so the hint can say "your $0014 is used like
another game's Joypad_Buttons". Matches are "identical" when the raw bytes are the same as well. Without
a whole match, routines sharing long stretches of code (RUN instructions) are reported as partial.

Measured on four early NROM games: almost no whole routines are shared, and
long shared stretches only in a handful (one pair of games: 4 routines). The payoff depends
on the library: games from one studio and engine should share far more.

Hints are our own earlier work, never a reference. Names a project wrote after its reference was
read (`reference_seen`) are left out, so blind benchmark projects stay blind.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .analyze import PRG_BANK
from .annotate import Context, _instruction_starts, contexts, is_ram
from .kb import KB, NON_ROM
from .opcodes import ABSOLUTE_MODES, IMM, OPCODES, REL, ZEROPAGE_MODES, operand_value

MIN_INSTRUCTIONS = 6
MIN_BYTES = 10
RUN = 12              # partial matches: shared runs of this many instructions (addresses abstracted)
MIN_SHARE = 0.3       # ...covering at least this share of the routine's runs
SCHEMA = """CREATE TABLE IF NOT EXISTS library (
    entry INTEGER NOT NULL, project TEXT NOT NULL, their_entry INTEGER NOT NULL, kind TEXT NOT NULL,
    name TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, confidence REAL, ram TEXT NOT NULL,
    updated TEXT NOT NULL, PRIMARY KEY (entry, project, their_entry))"""


def _token(ctx: Context, pc: int) -> str:
    op = OPCODES[ctx.prg[pc - ctx.base]]
    v = operand_value(op, ctx.prg, pc - ctx.base)
    if v is None or op.mode in (IMM, REL):
        return f"{op.code:02x}"
    if 0x2000 <= v < 0x4020:
        return f"{op.code:02x}H{v:x}"
    return f"{op.code:02x}{'M' if is_ram(v) else 'R'}"


def runs(ctx: Context, entry: int) -> set[tuple[str, ...]]:
    """Every RUN-instruction stretch of the routine, addresses abstracted (constants dropped too)."""
    toks = [_token(ctx, pc) for pc in sorted(_instruction_starts(ctx, entry, ctx.routines[entry][0]))]
    return {tuple(toks[i:i + RUN]) for i in range(len(toks) - RUN + 1)}


def signature(ctx: Context, entry: int) -> tuple[str, bytes, list[int]] | None:
    """(shape hash, raw bytes, RAM addresses in order of first use), or None if too small to match."""
    last = ctx.routines[entry][0]
    starts = sorted(_instruction_starts(ctx, entry, last))
    if len(starts) < MIN_INSTRUCTIONS or last - entry + 1 < MIN_BYTES:
        return None
    ram: list[int] = []
    parts = []
    for pc in starts:
        op = OPCODES[ctx.prg[pc - ctx.base]]
        v = operand_value(op, ctx.prg, pc - ctx.base)
        if v is None or op.mode in (IMM, REL):
            parts.append(f"{op.code:02x}:{'' if v is None else v}")
        elif op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES:
            if 0x2000 <= v < 0x4020:
                parts.append(f"{op.code:02x}:H{v:04x}")
            elif is_ram(v):
                if v not in ram:
                    ram.append(v)
                parts.append(f"{op.code:02x}:M{ram.index(v)}")
            else:
                parts.append(f"{op.code:02x}:R")
    raw = bytes(ctx.prg[entry - ctx.base:last - ctx.base + 1])
    return hashlib.sha1(" ".join(parts).encode()).hexdigest(), raw, ram


def _blind_cutoff(kb: KB) -> str | None:
    """When the reference was first read: an ISO timestamp, or a date (then the whole day counts as
    seen). "2026-10-18T23:31:17+00:00 (user-requested ...)" -> the timestamp."""
    seen = kb.setting("reference_seen")
    return seen.split()[0] if seen else None


def index(kb_path: Path) -> tuple[dict[str, list[dict]], dict[tuple, list[dict]]]:
    """Signature -> annotated routines of one project (with their names and RAM labels), and
    shared-run -> annotated routines containing it (for partial matches)."""
    out: dict[str, list[dict]] = {}
    by_run: dict[tuple, list[dict]] = {}
    with KB(kb_path) as kb:
        cutoff = _blind_cutoff(kb)
        ram_labels = {l.addr: l.name for l in kb.labels(NON_ROM) if l.kind == "ram"}
        banks = [(ctx, {l.addr: l for l in kb.labels(ctx.bank)},
                  {e: (s, st, c, u) for e, s, st, _, c, u in kb.db.execute(
                      "SELECT entry, summary, status, source, confidence, updated FROM routine_notes WHERE bank=?",
                      (ctx.bank,))}) for ctx in contexts(kb)]
    for ctx, labels, notes in banks:          # every bank of the other project is knowledge to reuse
        _index_bank(ctx, labels, notes, ram_labels, cutoff, out, by_run)
    return out, by_run


def _index_bank(ctx: Context, labels, notes, ram_labels, cutoff, out, by_run) -> None:
    for entry, (summary, status, conf, updated) in notes.items():
        if entry not in ctx.routines or entry not in labels or (cutoff and updated[:len(cutoff)] >= cutoff):
            continue
        info = {"entry": entry, "name": labels[entry].name, "summary": summary,
                "status": labels[entry].status, "confidence": conf}
        for r in runs(ctx, entry):
            by_run.setdefault(r, []).append(info)
        sig = signature(ctx, entry)
        if sig is None:
            continue
        h, raw, ram = sig
        out.setdefault(h, []).append({**info, "raw": raw, "ram": [ram_labels.get(a) for a in ram], "ram_addrs": ram})


def refresh(kb_path: Path, projects: dict[str, Path]) -> dict:
    """Match every routine of this project against the other projects; store hints (table `library`)."""
    others = {stem: index(p) for stem, p in projects.items() if stem != kb_path.stem}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, matched = [], set()
    with KB(kb_path) as kb:
        ctx = Context.load(kb)    # the library table has no bank column yet: matches are for bank 0
        for entry in ctx.routines:
            sig = signature(ctx, entry)
            h, raw, ram = sig if sig else (None, b"", [])
            for stem, (idx, by_run) in others.items():
                whole = idx.get(h, []) if h else []
                for m in whole:
                    pairs = [{"ours": f"${a:04X}", "theirs": f"${b:04X}", "name": n}
                             for a, b, n in zip(ram, m["ram_addrs"], m["ram"])]
                    rows.append((entry, stem, m["entry"], "identical" if raw == m["raw"] else "same shape",
                                 m["name"], m["summary"], m["status"], m["confidence"], json.dumps(pairs), now))
                    matched.add(entry)
                if whole:
                    continue
                mine = runs(ctx, entry)                      # no whole match: shared stretches of code
                count: dict[int, list] = {}
                for r in mine:
                    for m in by_run.get(r, []):
                        count.setdefault(m["entry"], [m, 0])[1] += 1
                for m, n in count.values():
                    if mine and n / len(mine) >= MIN_SHARE:
                        rows.append((entry, stem, m["entry"], f"shares {n / len(mine):.0%} of its code",
                                     m["name"], m["summary"], m["status"], m["confidence"], "[]", now))
                        matched.add(entry)
        kb.db.execute(SCHEMA)
        kb.db.execute("DELETE FROM library")
        kb.db.executemany("INSERT OR REPLACE INTO library VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        kb.commit()
        annotated = sum(1 for e in matched if e in ctx.notes)
    return {"projects": sorted(others), "matches": len(rows), "routines": len(matched),
            "unannotated": len(matched) - annotated}


def hints(kb: KB, entry: int) -> list[dict]:
    kb.db.execute(SCHEMA)
    return [{"project": p, "their_entry": t, "kind": k, "name": n, "summary": s, "status": st, "confidence": c,
             "ram": json.loads(r)} for p, t, k, n, s, st, c, r in kb.db.execute(
        "SELECT project, their_entry, kind, name, summary, status, confidence, ram FROM library WHERE entry=? "
        "ORDER BY kind='identical' DESC, status IN ('verified','approved') DESC, project", (entry,))]


def section(kb: KB, entry: int) -> list[str]:
    """Work-unit lines: the same code in other games, with their names and RAM meanings."""
    found = hints(kb, entry)
    if not found:
        return []
    out = ["", "### Same code in other games (our earlier annotations there: hints, not facts)",
           "Check the code still does the same here (callers and data can differ); reuse the name only if it fits.",
           "Where the RAM matches, the other game's label is a strong hint for this game's address."]
    for h in found[:4]:
        out.append(f"- {h['project']} ${h['their_entry']:04X} {h['name']} [{h['kind']}, {h['status']}"
                   + (f" {h['confidence']:.2f}" if h["confidence"] is not None else "") + f"]: {h['summary'][:200]}")
        named = [f"{r['ours']} ~ {r['name']}" for r in h["ram"] if r["name"]]
        if named:
            out.append("  RAM: " + ", ".join(named[:8]))
    return out
