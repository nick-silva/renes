"""Semantic benchmark grading (backlog item 6).

Token similarity undercounts: `Powerup_Kind` vs `PowerUpType` scores low but means the same thing,
and the benchmark game's RAM names were ~90% right by hand at a 32% token "match". A grader with reference access
judges a sample of pairs instead:

  same     names the same thing (different words are fine)
  entity   right behaviour or mechanics, but misses or misnames the game thing (SpawnObject2F vs
           a name for the actual enemy), or is clearly more generic
  wrong    a different meaning
  unclear  the reference context is not enough to tell

Isolation: the grader is a separate agent (the `renes-grade` skill), never the annotator. `prepare`
writes the sample with reference names and source context to out/<stem>/grading/packet.json; the
grader writes verdicts.json next to it; `record` stores **aggregates only** (table `gradings`). The
annotator must not open out/<stem>/grading/, like benchmark_details.md.
"""
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .analyze import PRG_BANK
from .benchmark import Reference, _bucket, _evidence_bucket, parse_reference, score
from .kb import KB, NON_ROM

VERDICTS = ("same", "entity", "wrong", "unclear")
SHARES = {"routine": 0.4, "ram": 0.35, "data": 0.125, "code": 0.125}   # of the sample, by label kind
CONTEXT = 3        # reference source lines shown after the definition (and up to 3 comment lines before)

SCHEMA = """CREATE TABLE IF NOT EXISTS gradings (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, sample INTEGER NOT NULL, results TEXT NOT NULL)"""


def grading_dir(kb_path: Path, root: Path) -> Path:
    return root / "out" / kb_path.stem / "grading"


def _context(ref: Reference, name: str) -> str:
    i = ref.where.get(name)
    if i is None:
        return ""
    start = i
    while start > 0 and i - start < 3 and ref.lines[start - 1].lstrip().startswith(";"):
        start -= 1
    return "\n".join(ref.lines[start:i + 1 + CONTEXT])


def _ours(bank: int, addr: int, notes: dict, comments: dict) -> str:
    if bank == PRG_BANK and addr in notes:
        return notes[addr]
    texts = [t for k, t in comments.get(bank, {}).get(addr, []) if k in ("line", "block")]
    return " ".join(texts)[:300]


def prepare(kb_path: Path, out: Path, sample: int = 150, seed: int = 1) -> dict:
    """Write packet.json: a stratified sample of our names paired with the reference's."""
    with KB(kb_path) as kb:
        rom = kb.rom()
        ref = parse_reference(Path(kb.setting("reference")).read_text(encoding="latin-1"), rom.prg, rom.prg_base)
        _, rows = score(kb, ref)
        notes = {e: s for e, s, *_ in kb.routine_notes(PRG_BANK)}
        comments = {PRG_BANK: kb.comments(PRG_BANK), NON_ROM: kb.comments(NON_ROM)}
        evidence = {(b, a): s for b, a, s in kb.db.execute("SELECT bank, addr, score FROM evidence")}
    rng = random.Random(seed)
    by_kind = defaultdict(list)
    for r in rows:
        if r[4] is not None:
            by_kind[r[0]].append(r)
    chosen = []
    for kind, share in SHARES.items():
        pool = by_kind.get(kind, [])
        chosen += rng.sample(pool, min(len(pool), round(sample * share)))
    rng.shuffle(chosen)
    pairs = []
    for n, (kind, status, conf, name, _, sim, addr, bank) in enumerate(chosen, 1):
        refs = (ref.ram if bank == NON_ROM else ref.names)[addr]
        pairs.append({"id": f"g{n}", "kind": kind, "addr": f"${addr:04X}", "ours": name,
                      "our_description": _ours(bank, addr, notes, comments),
                      "reference": refs, "reference_source": "\n---\n".join(_context(ref, x) for x in refs),
                      # for aggregation only; the grader ignores these
                      "_status": status, "_confidence": conf, "_evidence": evidence.get((bank, addr)),
                      "_similarity": sim})
    out.mkdir(parents=True, exist_ok=True)
    (out / "packet.json").write_text(json.dumps({"game": rom.name, "pairs": pairs}, indent=1), encoding="utf-8")
    for old in out.glob("verdicts*.json"):
        old.unlink()
    return {"pairs": len(pairs), "by_kind": {k: sum(1 for p in pairs if p["kind"] == k) for k in SHARES}}


def _summarise(items: list[dict]) -> dict:
    graded = [i for i in items if i["verdict"] != "unclear"]
    n = len(graded)
    count = {v: sum(1 for i in items if i["verdict"] == v) for v in VERDICTS}
    return {"pairs": len(items), **count,
            "same_rate": round(count["same"] / n, 3) if n else None,
            "right_behaviour_rate": round((count["same"] + count["entity"]) / n, 3) if n else None,
            "token_match_rate": round(sum(1 for i in graded if (i["_similarity"] or 0) >= 0.5) / n, 3) if n else None}


def agreement(first: dict[str, str], second: dict[str, str]) -> dict:
    """Share of pairs two graders judged alike, and Cohen's kappa (agreement beyond chance)."""
    ids = sorted(set(first) & set(second))
    if not ids:
        return {"pairs": 0, "rate": None, "kappa": None}
    n = len(ids)
    po = sum(first[i] == second[i] for i in ids) / n
    pe = sum((sum(first[i] == v for i in ids) / n) * (sum(second[i] == v for i in ids) / n) for v in VERDICTS)
    return {"pairs": n, "rate": round(po, 3), "kappa": round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0}


def _verdicts(path: Path) -> dict[str, str]:
    return {v["id"]: v["verdict"] for v in json.loads(path.read_text(encoding="utf-8")) if v.get("verdict") in VERDICTS}


def record(kb_path: Path, out: Path) -> dict:
    """Read the grader's verdicts, store aggregates, return them. Per-pair verdicts stay in out/."""
    packet = json.loads((out / "packet.json").read_text(encoding="utf-8"))
    given = _verdicts(out / "verdicts.json")
    items = [{**p, "verdict": given[p["id"]]} for p in packet["pairs"] if p["id"] in given]
    if not items:
        raise ValueError("verdicts.json has no valid verdicts for this packet")

    def group(key) -> dict:
        groups: dict[str, list] = defaultdict(list)
        for i in items:
            groups[key(i)].append(i)
        return {k: _summarise(v) for k, v in sorted(groups.items())}

    results = {"graded": len(items), "of": len(packet["pairs"]), "all": _summarise(items),
               "by_kind": group(lambda i: i["kind"]), "by_status": group(lambda i: i["_status"]),
               "by_confidence": group(lambda i: _bucket(i["_confidence"])),
               "by_evidence": group(lambda i: _evidence_bucket(i["_evidence"]))}
    # Independent second graders (verdicts_<x>.json) measure how repeatable the grading is.
    others = sorted(out.glob("verdicts_*.json"))
    if others:
        results["agreement"] = {o.stem[len("verdicts_"):]: agreement(given, _verdicts(o)) for o in others}
    with KB(kb_path) as kb:
        kb.db.execute(SCHEMA)
        kb.db.execute("INSERT INTO gradings (created, sample, results) VALUES (?,?,?)",
                      (datetime.now(timezone.utc).isoformat(timespec="seconds"), len(items), json.dumps(results)))
        kb.commit()
    return results


def latest(kb: KB) -> dict | None:
    kb.db.execute(SCHEMA)
    row = kb.db.execute("SELECT created, results FROM gradings ORDER BY id DESC LIMIT 1").fetchone()
    return {"created": row[0], **json.loads(row[1])} if row else None


def format_results(results: dict) -> str:
    def line(title: str, s: dict) -> str:
        if s["same_rate"] is None:
            return f"  {title:<20} {s['pairs']:>3} pairs, all unclear"
        return (f"  {title:<20} {s['pairs']:>3} pairs: same meaning {s['same_rate']:.0%}, right behaviour "
                f"{s['right_behaviour_rate']:.0%} (token match {s['token_match_rate']:.0%}); "
                f"{s['wrong']} wrong, {s['unclear']} unclear")

    out = [f"Semantic grading: {results['graded']}/{results['of']} pairs graded "
           "(same = same meaning; right behaviour = same + right mechanics but missing the game thing)",
           line("all", results["all"])]
    for name, a in (results.get("agreement") or {}).items():
        if a["pairs"]:
            out.append(f"  second grader {name}: agrees on {a['rate']:.0%} of {a['pairs']} pairs (kappa {a['kappa']:.2f})")
    for group in ("by_kind", "by_status", "by_confidence", "by_evidence"):
        for k, s in results[group].items():
            out.append(line(f"{group[3:]}: {k}", s))
    return "\n".join(out) + "\n"
