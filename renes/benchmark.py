"""M8 benchmark: score the knowledge base against a reference disassembly.

The reference is read by code only. The annotator (Claude) must never see reference names,
or every later annotation is contaminated. So `score()` returns **aggregates only**: how much
of the code/data split agrees, how many reference labels we found, and how similar our names
are to the reference names at the same addresses. Per-name pairs go to a details file meant for
the human (`out/<stem>/benchmark_details.md`); skills tell Claude not to open it.

The reference must reassemble to this ROM. Rather than reimplementing its assembler, the parser
walks the source alongside the ROM: each instruction's size comes from the opcode byte at the
current address, and its mnemonic must match. The alignment rate is reported, so a reference
that does not fit this ROM is obvious.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .kb import KB, NON_ROM
from .opcodes import OPCODES

MNEMONICS = {op.mnemonic for op in OPCODES.values()}
LABEL_RE = re.compile(r"^([A-Za-z_][\w]*):")
ANY_EQUATE_RE = re.compile(r"^[A-Za-z_][\w]*\s*=")
EQUATE_RE = re.compile(r"^([A-Za-z_][\w]*)\s*=\s*(\$[0-9A-Fa-f]+|%[01]+|\d+)\s*$")
MATCH = 0.5            # name similarity counted as a match
RTS = 0x60
# Generic abbreviations only (no game knowledge): both sides are expanded the same way.
ABBREV = {"pos": "position", "num": "number", "ctrl": "control", "spr": "sprite", "obj": "object",
          "ptr": "pointer", "addr": "address", "dir": "direction", "init": "initialize", "lo": "low",
          "hi": "high", "tbl": "table", "dat": "data", "vram": "vram", "buf": "buffer", "cnt": "counter",
          "ctr": "counter", "tmr": "timer", "plyr": "player", "en": "enemy", "snd": "sound", "sfx": "sound",
          "msg": "message", "chk": "check", "calc": "calculate", "attrib": "attribute", "attr": "attribute",
          "pal": "palette", "img": "image", "gfx": "graphics", "rel": "relative", "offs": "offset", "ofs": "offset",
          "bg": "background", "flg": "flag", "temp": "scratch", "tmp": "scratch"}
STOP = {"a", "the", "of", "to", "and", "or", "for", "then", "sub", "routine", "is", "by", "in", "on"}


class ReferenceError(ValueError):
    pass


@dataclass
class Reference:
    code: set[int] = field(default_factory=set)        # CPU addresses of instruction bytes
    data: set[int] = field(default_factory=set)        # CPU addresses of .db/.dw bytes
    names: dict[int, list[str]] = field(default_factory=dict)   # ROM address -> label names
    ram: dict[int, list[str]] = field(default_factory=dict)     # RAM address -> equate names
    constants: int = 0          # equates < $800 only ever used as immediates (#Name): not RAM, not paired
    lines: list[str] = field(default_factory=list)              # the source, for grading context
    where: dict[str, int] = field(default_factory=dict)         # label/equate name -> line index
    instructions: int = 0
    aligned: int = 0


def _value(token: str) -> int:
    token = token.strip()
    if token.startswith("$"):
        return int(token[1:], 16)
    if token.startswith("%"):
        return int(token[1:], 2)
    return int(token)


IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def parse_reference(text: str, prg: bytes, prg_base: int) -> Reference:
    ref = Reference()
    pc = None
    pending: list[str] = []
    memory_use: set[str] = set()     # names used as a memory operand (lda Name,x / sta Name+1)

    def byte_at(addr: int) -> int | None:
        i = addr - prg_base
        return prg[i] if 0 <= i < len(prg) else None

    ref.lines = text.splitlines()
    for index, raw in enumerate(ref.lines):
        code = raw.split(";")[0].rstrip()
        if not code.strip():
            continue
        if ANY_EQUATE_RE.match(code.strip()):
            m = EQUATE_RE.match(code.strip())   # expression equates (Name = Other+1) are skipped
            if m and _value(m.group(2)) < 0x800:
                ref.ram.setdefault(_value(m.group(2)), []).append(m.group(1))
                ref.where[m.group(1)] = index
            continue
        m = LABEL_RE.match(code)
        if m:
            pending.append(m.group(1))
            ref.where[m.group(1)] = index
            code = code[m.end():]
            if not code.strip():
                continue
        words = code.split(None, 1)
        head = words[0].lower()
        rest = words[1] if len(words) > 1 else ""
        if head == ".org":
            pc = _value(rest)
            continue
        if head in (".index", ".mem"):
            continue
        if pc is None:
            raise ReferenceError("code before .org")
        for name in pending:
            ref.names.setdefault(pc, []).append(name)
        pending = []
        if head in (".db", ".dw"):
            size = (1 if head == ".db" else 2) * len([x for x in rest.split(",") if x.strip()])
            ref.data.update(range(pc, pc + size))
            pc += size
        elif head in MNEMONICS:
            if rest and not rest.lstrip().startswith("#"):
                memory_use.update(IDENT_RE.findall(rest))
            ref.instructions += 1
            op = OPCODES.get(byte_at(pc))
            if op is None or op.mnemonic != head:
                raise ReferenceError(f"reference does not match this ROM at ${pc:04X}")
            ref.aligned += 1
            ref.code.update(range(pc, pc + op.size))
            pc += op.size
        else:
            raise ReferenceError(f"unknown directive {head!r}")
    # An equate below $800 is only RAM if the code addresses memory with it; `EnemyTypeA = $06`
    # used as `lda #EnemyTypeA` is a constant and would pair with whatever RAM sits at $0006.
    for addr in list(ref.ram):
        names = [n for n in ref.ram[addr] if n in memory_use]
        ref.constants += len(ref.ram[addr]) - len(names)
        if names:
            ref.ram[addr] = names
        else:
            del ref.ram[addr]
    return ref


def tokens(name: str) -> set[str]:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", name.replace("_", " "))
    out = set()
    for w in words:
        w = ABBREV.get(w.lower(), w.lower())
        if w.endswith("s") and len(w) > 3:
            w = w[:-1]
        if w not in STOP:
            out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    exact = ta & tb
    # partial credit for prefixes (position/pos, enemies/enemy after stemming misses)
    partial = {x for x in ta - exact for y in tb - exact if len(x) >= 3 and len(y) >= 3
               and (x.startswith(y) or y.startswith(x))}
    return (len(exact) + 0.5 * len(partial)) / len(ta | tb)


def _bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    return "<0.5" if conf < 0.5 else "0.5-0.7" if conf < 0.7 else ">=0.7"


def _evidence_bucket(score: float | None) -> str:
    if score is None:
        return "unknown"
    return "<0.3" if score < 0.3 else "0.3-0.6" if score < 0.6 else ">=0.6"


def score(kb: KB, ref: Reference) -> tuple[dict, list[tuple]]:
    """Aggregate scores, plus per-name rows (for the human-only details file)."""
    rom = kb.rom()
    base, size = rom.prg_base, len(rom.prg)
    ours = kb.classes(0)
    code_kinds, data_kinds = {"op", "operand"}, {"data", "word", "word_hi"}
    our_code = {a for a, k in ours.items() if k in code_kinds}
    our_data = {a for a, k in ours.items() if k in data_kinds}
    classified = our_code | our_data
    agree = len(our_code & ref.code) + len(our_data & ref.data)
    ref_code_labels = {a for a in ref.names if a in ref.code}
    labels = kb.labels(0)
    our_label_addrs = {l.addr for l in labels}
    routines = {e for e, _, _ in kb.routines(0)}

    evidence = {(b, a): s for b, a, s in kb.db.execute("SELECT bank, addr, score FROM evidence")}
    rows = []
    shared_exits = 0
    for l in labels + kb.labels(NON_ROM):
        if l.source != "claude":
            continue
        if l.bank != NON_ROM and 0 <= l.addr - base < size and rom.prg[l.addr - base] == RTS                 and l.addr in ref.names:
            # A lone RTS that many routines branch to: both names describe someone else's exit.
            shared_exits += 1
            continue
        refs = ref.ram.get(l.addr) if l.bank == NON_ROM else ref.names.get(l.addr)
        if not refs:
            rows.append((l.kind, l.status, l.confidence, l.name, None, None, l.addr, l.bank))
            continue
        best = max(refs, key=lambda r: similarity(l.name, r))
        rows.append((l.kind, l.status, l.confidence, l.name, best, similarity(l.name, best), l.addr, l.bank))

    def naming(subset) -> dict:
        scored = [r[5] for r in subset if r[5] is not None]
        return {"names": len(subset), "with_reference": len(scored),
                "match_rate": round(sum(1 for s in scored if s >= MATCH) / len(scored), 3) if scored else None,
                "mean_similarity": round(sum(scored) / len(scored), 3) if scored else None}

    results = {
        "reference": {"instructions": ref.instructions, "aligned": ref.aligned,
                      "code_bytes": len(ref.code), "data_bytes": len(ref.data), "labels": len(ref.names),
                      "ram_names": sum(len(v) for v in ref.ram.values()), "constants_skipped": ref.constants,
                      "shared_exits_skipped": shared_exits},
        "classification": {
            "classified_share": round(len(classified) / size, 3),
            "agreement_on_classified": round(agree / max(len(classified), 1), 4),
            "code_recall": round(len(our_code & ref.code) / max(len(ref.code), 1), 4),
            "code_precision": round(len(our_code & ref.code) / max(len(our_code), 1), 4),
            "data_called_code": len(our_code & ref.data),
            "code_called_data": len(our_data & ref.code),
        },
        "labels": {
            "reference_code_labels_found": round(len(ref_code_labels & our_label_addrs) / max(len(ref_code_labels), 1), 3),
            "routine_entries_on_reference_labels": round(len(routines & set(ref.names)) / max(len(routines), 1), 3),
        },
        "naming": {
            "all": naming(rows),
            "by_kind": {k: naming([r for r in rows if r[0] == k]) for k in sorted({r[0] for r in rows})},
            "by_status": {s: naming([r for r in rows if r[1] == s]) for s in sorted({r[1] for r in rows})},
            "by_confidence": {b: naming([r for r in rows if _bucket(r[2]) == b])
                              for b in ("<0.5", "0.5-0.7", ">=0.7", "unknown")},
            "by_evidence": {b: naming([r for r in rows if _evidence_bucket(evidence.get((r[7], r[6]))) == b])
                            for b in ("<0.3", "0.3-0.6", ">=0.6", "unknown")},
        },
    }
    return results, rows


def run(kb_path: Path, reference_path: Path, details_path: Path) -> dict:
    with KB(kb_path) as kb:
        rom = kb.rom()
        ref = parse_reference(reference_path.read_text(encoding="latin-1"), rom.prg, rom.prg_base)
        results, rows = score(kb, ref)
        kb.add_benchmark(datetime.now(timezone.utc).isoformat(timespec="seconds"), reference_path.name,
                         json.dumps(results))
        kb.commit()
    details_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Benchmark details (for the human reviewer only: do not show to the annotator)", "",
             "| address | kind | status | conf | ours | reference | similarity |", "|---|---|---|---|---|---|---|"]
    for kind, status, conf, name, refname, sim, addr, bank in sorted(rows, key=lambda r: (r[5] is None, r[5] or 0)):
        where = f"${addr:04X}" + (" (RAM)" if bank == NON_ROM else "")
        lines.append(f"| {where} | {kind} | {status} | {conf if conf is not None else '-'} | {name} | "
                     f"{refname or '-'} | {'-' if sim is None else f'{sim:.2f}'} |")
    details_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def format_results(results: dict, previous: dict | None = None) -> str:
    """Aggregate view: safe for the annotator to read (no reference names)."""
    r, c, lab, n = results["reference"], results["classification"], results["labels"], results["naming"]

    def delta(path: list[str]) -> str:
        if not previous:
            return ""
        old, new = previous, results
        for p in path:
            old, new = (old or {}).get(p), (new or {}).get(p)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)) and old != new:
            return f" ({'+' if new > old else ''}{new - old:.3g})"
        return ""

    out = [f"Reference: {r['aligned']}/{r['instructions']} instructions aligned with this ROM, "
           f"{r['labels']} labels, {r['ram_names']} RAM names"
           + (f" (not paired: {r.get('constants_skipped', 0)} constant equates, "
              f"{r.get('shared_exits_skipped', 0)} names on shared RTS exits)" if "constants_skipped" in r else ""), "",
           "Code/data split",
           f"  classified: {c['classified_share']:.1%} of PRG{delta(['classification', 'classified_share'])}",
           f"  agreement on classified bytes: {c['agreement_on_classified']:.2%}"
           f"{delta(['classification', 'agreement_on_classified'])}",
           f"  code recall {c['code_recall']:.2%}, precision {c['code_precision']:.2%}; "
           f"data called code: {c['data_called_code']} bytes, code called data: {c['code_called_data']} bytes",
           "", "Labels",
           f"  reference code labels we also have: {lab['reference_code_labels_found']:.1%}",
           f"  our routine entries that the reference also labels: {lab['routine_entries_on_reference_labels']:.1%}",
           "", f"Naming (match = token similarity >= {MATCH})"]

    def line(title: str, s: dict, path: list[str]) -> str:
        if not s["with_reference"]:
            return f"  {title:<16} {s['names']:>4} names, none at a referenced address"
        return (f"  {title:<16} {s['names']:>4} names, {s['with_reference']:>4} comparable: match {s['match_rate']:.0%}"
                f"{delta(path + ['match_rate'])}, mean similarity {s['mean_similarity']:.2f}")

    out.append(line("all", n["all"], ["naming", "all"]))
    for group in ("by_kind", "by_status", "by_confidence", "by_evidence"):
        for k, s in n.get(group, {}).items():
            if s["names"]:
                out.append(line(f"{group[3:]}: {k}", s, ["naming", group, k]))
    return "\n".join(out) + "\n"
