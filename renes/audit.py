"""Reference-free quality audit: does the code support the names? (any project)

Few games have a reference disassembly, so few can be graded. The audit gives every project
a quality number: a separate auditor agent (the `renes-audit` skill, never the annotator) reads a
sample of annotated routines and RAM labels, each with its code, our name, summary and comments,
the callees' summaries, the RAM it touches and the runtime evidence, and judges:

  supported    the code clearly does what the name and summary say
  plausible    consistent with the code, but not demonstrated by it (vague, or depends on
               meanings established elsewhere)
  unsupported  the code doesn't do what is claimed, or contradicts it
  unclear      not enough context to judge

`prepare` writes out/<stem>/audit/packet.json; the auditor writes verdicts.json (second auditors:
verdicts_<x>.json, for agreement); `record` stores aggregates only (table `audits`). When the
project was also graded against a reference, `record` cross-tabulates the two on the pairs both
judged, which shows whether "supported" tracks "same meaning".
"""
import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import annotate, confidence
from .analyze import PRG_BANK
from .benchmark import _bucket, _evidence_bucket
from .grade import agreement
from .kb import KB, NON_ROM
from .opcodes import OPCODES, TERMINATORS
from .render import symbols_for

VERDICTS = ("supported", "plausible", "unsupported", "unclear")
USES = 8              # routines with code snippets shown per RAM label
HITS = 2              # snippets per routine
MAX_LIST = 25         # callers, callees, RAM per routine; longer lists say how many were left out
MAX_EXITS = 6         # places a routine leads to, shown with their code
SCHEMA = """CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, sample INTEGER NOT NULL, results TEXT NOT NULL)"""


def audit_dir(kb_path: Path, root: Path) -> Path:
    return root / "out" / kb_path.stem / "audit"


def _routine_item(ctx, sym, labels, comments, ram_labels, entry: int, checked: dict) -> dict:
    kb = ctx.kb
    name = lambda a: labels[a].name if a in labels else f"${a:04X}"
    last = ctx.routines[entry][0]
    listing = annotate._listing(ctx, sym, labels, comments, entry, last)
    callees = sorted({c for c, _, k in ctx.callees.get(entry, [])})
    ko = kb.db.execute("SELECT screen_differs, effect FROM knockouts WHERE bank=? AND entry=? ORDER BY id DESC LIMIT 1",
                       (ctx.bank, entry)).fetchone()
    effect = None
    if ko:
        e = json.loads(ko[1])
        effect = (f"screen {'changed' if ko[0] else 'unchanged'}; labeled RAM that changed: "
                  + (", ".join(d["name"] for d in e["labeled"][:8]) or "none") + f"; {e['other_bytes']} other RAM bytes")
    return {"type": "routine", "addr": ctx.ref(entry), "name": labels[entry].name if entry in labels else f"${entry:04X}",
            "summary": ctx.notes.get(entry, ""),
            "code": listing.lines,              # whole: a cut-off routine can only be judged "plausible"
            "callers": _capped(sorted({name(c) for c, _, k in ctx.callers.get(entry, []) if k != "fall"}), MAX_LIST),
            "falls_in_from": sorted({name(c) for c, _, k in ctx.callers.get(entry, []) if k == "fall"}),
            "leads_to": _leads_to(ctx, sym, labels, comments, entry, last, listing.exits),
            "callees": _capped([f"{name(c)}: {ctx.notes.get(c, '(not annotated)')[:160]}" for c in callees], MAX_LIST),
            "ram": _capped([f"${a:04X} {ram_labels[a].name} [{ram_labels[a].status}]" if a in ram_labels
                            else f"${a:04X} (unlabeled)" for a in sorted(listing.ram_use)], MAX_LIST),
            "evidence": {"ran": f"{sum(ctx.executed(x) for x in range(entry, last + 1)) * 100 // (last - entry + 1)}% of bytes",
                         "knockout": effect},
            "_bank": ctx.bank, "_entry": entry, "_confidence": checked.get(entry, (None, None, None))[2],
            "_evidence": checked.get(entry, (None,))[0]}


def _leads_to(ctx, sym, labels, comments, entry: int, last: int, exits: set[int]) -> list[dict]:
    """The code a routine falls through into and the code its branches and jumps leave for, up to the
    next rts/jmp: claims about what happens next can only be judged with it."""
    name = lambda a: labels[a].name if a in labels else f"${a:04X}"
    final = max(annotate._instruction_starts(ctx, entry, last), default=entry)
    targets = [(t, "branch or jump") for t in sorted(exits)]
    if OPCODES[ctx.prg[final - ctx.base]].mnemonic not in TERMINATORS and last + 1 not in exits:
        targets.insert(0, (last + 1, "falls through"))
    out = []
    for t, how in targets[:MAX_EXITS]:
        owner = ctx.owner(t)
        if owner is None:
            out.append({"addr": f"${t:04X}", "how": how, "in": "no known routine"})
            continue
        snippet = annotate._listing(ctx, sym, labels, comments, t, ctx.routines[owner][0],
                                    max_instructions=10, stop_at_terminal=True)
        out.append({"addr": f"${t:04X}", "how": how,
                    "in": f"{'entry of' if owner == t else 'inside'} {name(owner)}: {ctx.notes.get(owner, '(not annotated)')[:160]}",
                    "code": snippet.lines})
    if len(targets) > MAX_EXITS:
        out.append({"more": len(targets) - MAX_EXITS})
    return out


def _capped(items: list, n: int) -> list:
    return items[:n] + ([f"... {len(items) - n} more"] if len(items) > n else [])


def _ram_item(ctx, sym, labels, comments, label, users: list[int], checked: dict, listings: dict) -> dict:
    """`listings` collects the whole code of each routine a snippet comes from, written next to the
    packet so the auditor can read past the snippet."""
    snippets = []
    for e in users[:USES]:
        lines = annotate._listing(ctx, sym, labels, comments, e, ctx.routines[e][0]).lines
        hits = [i for i, l in enumerate(lines) if label.name in l]
        name = labels[e].name if e in labels else f"${e:04X}"
        path = f"listings/{f'B{ctx.bank}_' if ctx.multi else ''}{e:04X}.asm"
        listings[path] = lines
        for n, i in enumerate(hits[:HITS]):
            snippets.append({"routine": name, "summary": "" if n else ctx.notes.get(e, "")[:160],
                             "lines": lines[max(0, i - 6):i + 7], "listing": path})
    return {"type": "ram", "addr": f"${label.addr:04X}", "name": label.name, "size": label.size or 1,
            "status": label.status, "comment": "", "uses": snippets,
            "used_by": _capped([labels[e].name if e in labels else f"${e:04X}" for e in users], MAX_LIST),
            "_bank": NON_ROM, "_entry": label.addr, "_confidence": checked.get(label.addr, (None, None, None))[2],
            "_evidence": checked.get(label.addr, (None,))[0]}


def prepare(kb_path: Path, out: Path, routines: int = 45, ram: int = 15, seed: int = 1,
            like: Path | None = None) -> dict:
    """Write packet.json. `like`: a grading packet whose routine/RAM addresses to audit instead of a
    random sample (to check the audit against reference grading)."""
    with KB(kb_path) as kb:
        ctxs = {c.bank: c for c in annotate.contexts(kb)}
        sym = {b: symbols_for(kb, b) for b in ctxs}
        labels = {b: {l.addr: l for l in kb.labels(b)} for b in ctxs}
        comments = {b: kb.comments(b) for b in ctxs}
        ram_labels = {l.addr: l for l in kb.labels(NON_ROM) if l.kind == "ram"}
        ram_comments = kb.comments(NON_ROM)
        checked = {b: confidence.scores(kb, b) for b in [*ctxs, NON_ROM]}
        access = defaultdict(set)                       # RAM address -> (bank, routine) using it
        for c in ctxs.values():
            for a, who in confidence.ram_access(c).items():
                access[a] |= {(c.bank, e) for e in who}
        rng = random.Random(seed)
        annotated = [(c.bank, e) for c in ctxs.values() for e in c.notes if e in c.routines]
        # a multi-byte label is used wherever any of its bytes is addressed (Score+1, not only Score)
        users_of = {l.addr: set().union(*(access.get(a, set()) for a in range(l.addr, l.addr + (l.size or 1))))
                    for l in ram_labels.values()}
        named_ram = [l for l in ram_labels.values() if l.source == "claude" and users_of[l.addr]]
        if like is not None:
            # a grading packet (pairs with a kind) or an earlier audit packet (items with a type)
            data = json.loads(like.read_text(encoding="utf-8"))
            rows = [(p["kind"], p["addr"]) for p in data["pairs"]] if "pairs" in data else \
                   [(i["type"], i["addr"]) for i in data["items"]]
            want_r = {annotate.parse_ref(a) for k, a in rows if k == "routine"}
            want_m = {annotate.parse_addr(a) for k, a in rows if k == "ram"}
            pick_r = [(b, e) for b, e in annotated if (b, e) in want_r or (None, e) in want_r]
            pick_m = [l for l in named_ram if l.addr in want_m]
        else:
            strata = defaultdict(list)                  # even spread over effective confidence
            for b, e in annotated:
                strata[_bucket(checked[b].get(e, (None, None, None))[2])].append((b, e))
            pick_r = []
            for group in strata.values():
                pick_r += rng.sample(group, min(len(group), -(-routines // max(1, len(strata)))))
            pick_r = rng.sample(pick_r, min(len(pick_r), routines))
            pick_m = rng.sample(named_ram, min(len(named_ram), ram))
        items = [_routine_item(ctxs[b], sym[b], labels[b], comments[b], ram_labels, e, checked[b]) for b, e in pick_r]
        listings = {}
        for l in pick_m:
            # RAM is shared by every bank: show its uses in the bank that uses it most
            b = max(ctxs, key=lambda k: sum(1 for ub, _ in users_of[l.addr] if ub == k))
            ctx = ctxs[b]
            users = sorted((e for ub, e in users_of[l.addr] if ub == b), key=lambda e: (e not in ctx.notes, e))
            item = _ram_item(ctx, sym[b], labels[b], comments[b], l, users, checked[NON_ROM], listings)
            item["comment"] = next((t for k, t in ram_comments.get(l.addr, []) if k == "line"), "")
            items.append(item)
        game = kb.rom().name
    rng.shuffle(items)
    for n, item in enumerate(items, 1):
        item["id"] = f"a{n}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "packet.json").write_text(json.dumps({"game": game, "items": items}, indent=1), encoding="utf-8")
    for old in [*out.glob("verdicts*.json"), *out.glob("listings/*.asm")]:
        old.unlink()
    for rel, lines in listings.items():
        (out / rel).parent.mkdir(exist_ok=True)
        (out / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"items": len(items), "routines": len(pick_r), "ram": len(pick_m)}


def _summarise(items: list[dict]) -> dict:
    judged = [i for i in items if i["verdict"] != "unclear"]
    n = len(judged)
    count = {v: sum(1 for i in items if i["verdict"] == v) for v in VERDICTS}
    return {"items": len(items), **count,
            "supported_rate": round(count["supported"] / n, 3) if n else None,
            "plausible_rate": round(count["plausible"] / n, 3) if n else None,
            "consistent_rate": round((count["supported"] + count["plausible"]) / n, 3) if n else None}


def _verdicts(path: Path) -> dict[str, str]:
    return {v["id"]: v["verdict"] for v in json.loads(path.read_text(encoding="utf-8")) if v.get("verdict") in VERDICTS}


def record(kb_path: Path, out: Path, grading: Path | None = None) -> dict:
    packet = json.loads((out / "packet.json").read_text(encoding="utf-8"))
    given = _verdicts(out / "verdicts.json")
    items = [{**i, "verdict": given[i["id"]]} for i in packet["items"] if i["id"] in given]
    if not items:
        raise ValueError("verdicts.json has no valid verdicts for this packet")

    def group(key) -> dict:
        groups: dict[str, list] = defaultdict(list)
        for i in items:
            groups[key(i)].append(i)
        return {k: _summarise(v) for k, v in sorted(groups.items())}

    results = {"audited": len(items), "of": len(packet["items"]), "all": _summarise(items),
               "by_type": group(lambda i: i["type"]),
               "by_confidence": group(lambda i: _bucket(i["_confidence"])),
               "by_evidence": group(lambda i: _evidence_bucket(i["_evidence"]))}
    others = sorted(out.glob("verdicts_*.json"))
    if others:
        results["agreement"] = {o.stem[len("verdicts_"):]: agreement(given, _verdicts(o)) for o in others}
    if grading is not None and (grading / "verdicts.json").exists():
        # Cross-check with reference grading, matched by address: counts only, no names.
        graded = json.loads((grading / "packet.json").read_text(encoding="utf-8"))["pairs"]
        verdict = _grade_verdicts(grading)
        grade_of = {("ram" if p["kind"] == "ram" else "routine", p["addr"]): verdict[p["id"]]
                    for p in graded if p["id"] in verdict}
        cross = Counter(f"{i['verdict']} / {grade_of[(i['type'], i['addr'])]}" for i in items
                        if (i["type"], i["addr"]) in grade_of)
        if cross:
            results["against_grading"] = dict(sorted(cross.items()))
    why = {v["id"]: v.get("why", "") for v in json.loads((out / "verdicts.json").read_text(encoding="utf-8"))}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with KB(kb_path) as kb:
        kb.db.execute(SCHEMA)
        kb.db.execute("INSERT INTO audits (created, sample, results) VALUES (?,?,?)", (now, len(items), json.dumps(results)))
        results["sent_back"] = send_back(kb, [i for i in items if i["verdict"] == "unsupported"], why, now)
        kb.commit()
    return results


def send_back(kb: KB, unsupported: list[dict], why: dict[str, str], now: str) -> int:
    """Unsupported names go back to the annotator with the auditor's reason (an automatic hint,
    source "audit"); routines also return to the front of the queue. The auditor saw only our own
    annotations, so its reasons are safe to show the annotator (unlike reference grading). Each
    reason is given once."""
    n = 0
    for i in unsupported:
        bank, addr = i["_bank"], i["_entry"]
        text = f"audit: the code doesn't support '{i['name']}'" + (f": {why[i['id']]}" if why.get(i["id"]) else "")
        if text in {t for t, s in kb.hints(bank, addr) if s == "audit"}:
            continue
        kb.add_hint(bank, addr, text, "audit", now)
        if bank != NON_ROM:
            kb.db.execute("UPDATE routine_notes SET status='rework' WHERE bank=? AND entry=? AND status='candidate'",
                          (bank, addr))
        n += 1
    return n


def _grade_verdicts(grading: Path) -> dict[str, str]:
    from .grade import _verdicts as grade_verdicts
    return grade_verdicts(grading / "verdicts.json")


def latest(kb: KB) -> dict | None:
    kb.db.execute(SCHEMA)
    row = kb.db.execute("SELECT created, results FROM audits ORDER BY id DESC LIMIT 1").fetchone()
    return {"created": row[0], **json.loads(row[1])} if row else None


def format_results(results: dict) -> str:
    def line(title: str, s: dict) -> str:
        if s["supported_rate"] is None:
            return f"  {title:<22} {s['items']:>3} items, all unclear"
        plausible = s.get("plausible_rate", s["consistent_rate"] - s["supported_rate"])   # older audits lack it
        return (f"  {title:<22} {s['items']:>3} items: supported {s['supported_rate']:.0%}, plausible "
                f"{plausible:.0%}, consistent {s['consistent_rate']:.0%}; {s['unsupported']} unsupported, "
                f"{s['unclear']} unclear")

    out = [f"Audit: {results['audited']}/{results['of']} items judged (supported = the code shows it; "
           "consistent = supported + plausible)", line("all", results["all"])]
    for name, a in (results.get("agreement") or {}).items():
        if a["pairs"]:
            out.append(f"  second auditor {name}: agrees on {a['rate']:.0%} of {a['pairs']} items (kappa {a['kappa']:.2f})")
    for group in ("by_type", "by_confidence", "by_evidence"):
        for k, s in results[group].items():
            out.append(line(f"{group[3:]}: {k}", s))
    if results.get("against_grading"):
        out.append("  audit / reference grading (same items): "
                   + ", ".join(f"{k} {v}" for k, v in results["against_grading"].items()))
    return "\n".join(out) + "\n"
