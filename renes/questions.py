"""Open questions (dashboard board, visualization item 7): what is still unexplained, in one place.

Each group says why it matters and what resolves it (a loop stage, a command or the human), and
lists its items with a link into the Code or Review tab. Everything is derived from data the
tools already record; nothing here changes the knowledge base.
"""
import json

from . import annotate, confidence, maps, visual
from .analyze import PRG_BANK
from .kb import KB, NON_ROM

LIMIT = 25            # items listed per group (the count covers all)
UNKNOWN_RUN = 256     # contiguous unknown ROM bytes worth listing


def _group(gid: str, title: str, why: str, resolve: str, items: list[dict], who: str = "loop") -> dict:
    return {"id": gid, "title": title, "why": why, "resolve": resolve, "who": who,
            "count": len(items), "items": items[:LIMIT]}


def _code(name: str) -> dict:
    return {"tab": "code", "target": name}


def _review(item_id: str) -> dict:
    return {"tab": "review", "target": item_id}


def questions(kb: KB) -> list[dict]:
    from .review import _experiments, routine_id

    ctxs = annotate.contexts(kb)
    labels = {(c.bank, l.addr): l for c in ctxs for l in kb.labels(c.bank)}
    ram = {l.addr: l for l in kb.labels(NON_ROM)}

    def rname(addr: int, bank: int = PRG_BANK) -> str:
        l = labels.get((bank, addr))
        return l.name if l else (f"B{bank}:${addr:04X}" if len(ctxs) > 1 else f"${addr:04X}")

    groups = []

    failed = []
    for addr, runs in sorted(_experiments(kb).items()):
        real = [r for r in runs if not r["diagnostic"]]
        if real and not any(r["passed"] for r in real) and addr in ram and ram[addr].status not in ("verified", "approved"):
            failed.append({"text": ram[addr].name, "detail": f"${addr:04X}: {len(real)} experiment(s) failed, none passed",
                           "link": _review(f"m{addr}")})
    groups.append(_group("failed", "Experiments that failed",
                         "The tested meaning didn't hold: the name may be wrong, or the experiment was.",
                         "rethink the meaning (Review: hint or rename) or design a better experiment", failed))

    routines = {c.bank: confidence.scores(kb, c.bank) for c in ctxs}
    ramscores = confidence.scores(kb, NON_ROM)
    over = []
    for bank, scores in [*routines.items(), (NON_ROM, ramscores)]:
        for addr, (ev, claimed, _, factors) in scores.items():
            if claimed is not None and claimed - ev >= confidence.OVERCONFIDENT:
                name = rname(addr, bank) if bank != NON_ROM else (ram[addr].name if addr in ram else f"${addr:04X}")
                over.append((claimed - ev, {"text": name, "detail": f"claimed {claimed:.2f}, evidence {ev:.2f} "
                                            f"({confidence.describe(factors)})",
                                            "link": _review(routine_id(bank, addr) if bank != NON_ROM else f"m{addr}")}))
    groups.append(_group("overconfident", "Claims well above their evidence",
                         "Confident names nothing has checked yet; for RAM, usually no experiment.",
                         "experiment stage (RAM), evidence stage (routines), or review",
                         [i for _, i in sorted(over, key=lambda x: -x[0])]))

    placeholder = []
    for ctx in ctxs:
        for addr, (_, _, _, factors) in sorted(routines[ctx.bank].items()):
            if factors and not factors.get("coherent"):
                sent = addr in ctx.rework
                placeholder.append({"text": rname(addr, ctx.bank), "detail": "sent back for a better name" if sent else
                                    "placeholder parts or a role only unlabeled RAM could hold",
                                    "link": _code(rname(addr, ctx.bank))})
    groups.append(_group("placeholder", "Placeholder names",
                         "Names that describe mechanics or raw numbers instead of meaning.",
                         "annotate stage (sent-back names come first); they return once what they use is named",
                         placeholder))

    probed, skipped = visual.probed_sites(kb), visual.skipped_sites(kb)
    tables = []
    for ctx in ctxs:
        for d in visual.dispatches(ctx):
            if d.index_addr is None or d.site in probed:
                continue
            var = ram[d.index_addr].name if d.index_addr in ram else f"${d.index_addr:04X}"
            tables.append({"text": f"{ctx.ref(d.site)} in {rname(d.owner, ctx.bank)}",
                           "detail": f"{len(d.handlers)} entries on {var}{',' + d.indexed if d.indexed else ''}: "
                                     + (f"skipped: {skipped[d.site]}" if d.site in skipped else "not probed yet"),
                           "link": _code(rname(d.owner, ctx.bank))})
    groups.append(_group("tables", "Jump tables never shown on screen",
                         "Each entry is a game state or object type; a picture says which.",
                         "probe stage (`visual probe`), or a --state where the table runs", tables))

    never = sorted(((c, e) for c in ctxs for e in c.notes if e in c.routines and not c.executed(e)),
                   key=lambda ce: -(ce[0].routines[ce[1]][0] - ce[1]))
    from .evidence import unreached_entries
    unreached = unreached_entries(kb)
    groups.append(_group("never_ran", "Annotated code never seen running",
                         "Named from the code alone: no trace, knock-out or screenshot can back it.",
                         "explore stage (reach more of the game), or a TAS movie that gets there",
                         [{"text": rname(e, c.bank), "detail": f"{c.routines[e][0] - e + 1} bytes"
                           + ("; the evidence playback never reaches it" if (c.bank, e) in unreached else ""),
                           "link": _code(rname(e, c.bank))} for c, e in never]))

    state, _ = maps.rom_state(kb)
    runs, start = [], None
    for i, s in enumerate(list(state) + [1]):
        if s == 0 and start is None:
            start = i
        elif s != 0 and start is not None:
            if i - start >= UNKNOWN_RUN:
                runs.append((start, i))
            start = None
    base = kb.rom().banks()[0].base                 # map positions: banks lie end to end (maps.linear)
    groups.append(_group("unknown", "Large unknown ROM regions",
                         "Neither code nor data was proven here: level data, unused code, or code never reached.",
                         "explore stage, or annotate the routines that read nearby tables",
                         [{"text": f"${base + a:04X}-${base + b - 1:04X}", "detail": f"{b - a} bytes",
                           "link": _code(f"${base + a:04X}")} for a, b in sorted(runs, key=lambda r: r[0] - r[1])]))

    ramstate, _ = maps.ram_state(kb)
    reads = kb.counters("nesInternalRam", "readCount") or []
    writes = kb.counters("nesInternalRam", "writeCount") or []
    writers: dict[int, set] = {}
    for c in ctxs:
        for a, who in annotate.ram_writers(c).items():
            writers.setdefault(a, set()).update((c.bank, e) for e in who)
    used = sorted(reads[a] + writes[a] for a in range(min(0x800, len(reads))) if ramstate[a] >= 1)
    cut = used[int(len(used) * 0.9)] if used else 0                            # the busiest tenth
    busy = sorted((a for a in range(min(0x800, len(reads)))
                   if ramstate[a] == 1 and reads[a] + writes[a] >= cut and writers.get(a)
                   and not 0x100 <= a < 0x300),                                # not the stack or sprite page
                  key=lambda a: -(reads[a] + writes[a]))
    groups.append(_group("busy_ram", "Busy RAM with no name",
                         "Among the busiest tenth of RAM, written by code, but nobody has said what it is.",
                         "annotate routines that write it (they'll propose a label), then the experiment stage",
                         [{"text": f"${a:04X}", "detail": f"{reads[a]:,} reads, {writes[a]:,} writes; "
                           f"written by {len(writers.get(a, ()))} routines", "link": _code(f"${a:04X}")} for a in busy]))

    pending = kb.db.execute("SELECT count(*) FROM brief WHERE status IS NULL OR status='proposed'").fetchone()[0]
    human = []
    if pending:
        human.append({"text": f"{pending} research facts", "detail": "proposed by Claude, not reviewed yet",
                      "link": {"tab": "research", "target": ""}})
    for bank, addr, old, name, why, agent in kb.rename_proposals():
        routines_here = next((c.routines for c in ctxs if c.bank == bank), {})
        link = _review(f"m{addr}") if bank == NON_ROM else _review(routine_id(bank, addr)) if addr in routines_here else _code(old)
        human.append({"text": f"rename {old} -> {name}?", "detail": f"{agent or 'an agent'}: {why or 'no reason given'}"
                      + " (rename it in Review, or leave it)", "link": link})
    hints = kb.db.execute("SELECT count(*) FROM routine_notes WHERE status='rework'").fetchone()[0]
    if hints:
        human.append({"text": f"{hints} routines sent back", "detail": "waiting for the annotate stage",
                      "link": {"tab": "review", "target": ""}})
    groups.append(_group("you", "Waiting on a person or the queue",
                         "Things the loop can't decide alone, or work already queued.",
                         "Research tab: accept/edit/reject facts; Review tab: decisions and hints", human, who="you"))
    return groups
