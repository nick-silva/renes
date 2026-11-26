"""M4 annotation loop, driven from Claude Code (no API key): work units out, proposals in.

`next` emits a self-contained work unit for the next routine, bottom-up along the call graph
(routines whose callees are already annotated come first). Claude reads it and writes a JSON
proposal. `apply` validates the proposal, writes it to the KB as *candidate* knowledge, and
proves the ROM still reassembles byte-identically; on any failure nothing is kept.

Proposal format (one object, or a list of them):
{
  "routine": "$C100",                       # entry address of the routine in the work unit
  "name": "InitializeNameTables",           # new routine label
  "summary": "Clears both nametables ...",  # one or two sentences, shown to callers
  "notes": ["optional block comment line", ...],
  "lines": {"$C103": "comment", ...},       # per-instruction comments (instruction starts)
  "labels": {"$C109": "WriteNTByte", ...},  # local code labels inside the routine
  "ram": {"$0010": {"name": "Mirror_PPU_CTRL", "comment": "..."}, ...},
  "data": {"$C141": "NameTableInitData", ...},  # ROM data the routine references
  "confidence": 0.7
}
"""
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .analyze import CDL_CODE, CDL_DATA, PRG_BANK
from .kb import KB, NON_ROM, Label
from .opcodes import ABSOLUTE_MODES, IND, OPCODES, REL, ZEROPAGE_MODES, branch_target, operand_value
from .render import render, symbols_for
from .verify import BuildError, build_and_verify
from . import metatiles

SOURCE = "claude"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,39}$")
AUTO_NAME_RE = re.compile(r"^(B\d+_)?(Sub|L|Data|Tbl)[0-9A-F]{4}$")
RESERVED = {op.mnemonic for op in OPCODES.values()} | {"a", "x", "y", "s", "z", "f"}
PROTECTED = {"builtin", "verified", "approved"}
WRITES = {"sta", "stx", "sty"}
READ_WRITES = {"inc", "dec", "asl", "lsr", "rol", "ror"}
TERMINAL = {"rts", "rti", "jmp"}
MAX_TEXT = 140      # comments
MAX_SUMMARY = 400   # routine summaries (wrapped when rendered)


def is_ram(addr: int) -> bool:
    return addr < 0x0800 or 0x6000 <= addr < 0x8000


def parse_addr(text: str | int) -> int:
    if isinstance(text, int):
        return text
    t = text.strip().lower().lstrip("$")
    return int(t[2:] if t.startswith("0x") else t, 16)


BANK_REF_RE = re.compile(r"^\s*[Bb](\d+)\s*:\s*(.+)$")


def parse_ref(text: str | int) -> tuple[int | None, int]:
    """"B1:$8539" -> (1, 0x8539); "$8539" -> (None, 0x8539): the bank is then implied."""
    m = BANK_REF_RE.match(text) if isinstance(text, str) else None
    return (int(m.group(1)), parse_addr(m.group(2))) if m else (None, parse_addr(text))


def banks(kb: KB) -> list[int]:
    """The KB's PRG bank numbers (one for NROM; one per 32 KB bank for AxROM)."""
    return [b.index for b in kb.rom().banks()]


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text.encode("ascii", "replace").decode()[:limit]


def _fit(r, what: str, text: str, limit: int = MAX_TEXT) -> str:
    """clean_text, with a warning in the report when the text had to be cut."""
    whole = clean_text(text, 1 << 16)
    if len(whole) > limit:
        r.warnings.append(f"{what}: {len(whole)} characters, cut to {limit} (dropped: \"{whole[limit:limit + 40]}...\"); "
                          "shorten it or move detail into notes or line comments")
    return whole[:limit]


# ---------------------------------------------------------------------------------------------
# Work units


@dataclass
class Context:
    kb: KB
    prg: bytes
    base: int
    kinds: dict[int, str]
    cdl: bytes | None
    routines: dict[int, tuple[int, str]]
    notes: dict[int, str]
    callers: dict[int, list[tuple[int, int, str]]] = field(default_factory=lambda: defaultdict(list))
    callees: dict[int, list[tuple[int, int, str]]] = field(default_factory=lambda: defaultdict(list))
    rework: set[int] = field(default_factory=set)   # annotated, but sent back by the reviewer
    bank: int = PRG_BANK
    multi: bool = False                             # one of several banks: addresses need "B1:"
    # calls through a bank switch (banking.far_calls): entry -> [(other bank, routine, site)]
    far_out: dict[int, list[tuple[int, int, int]]] = field(default_factory=lambda: defaultdict(list))
    far_in: dict[int, list[tuple[int, int, int]]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls, kb: KB, bank: int = PRG_BANK) -> "Context":
        all_banks = kb.rom().banks()
        where = next(b for b in all_banks if b.index == bank)
        cdl = kb.coverage("nesPrgRom", "cdl")
        if cdl is not None:
            cdl = cdl[where.offset:where.offset + len(where.data)]
        ctx = cls(kb, where.data, where.base, kb.classes(bank), cdl,
                  {e: (last, how) for e, last, how in kb.routines(bank)},
                  {e: s for e, s, *_ in kb.routine_notes(bank)}, bank=bank, multi=len(all_banks) > 1)
        ctx.rework = {e for e, _, status, *_ in kb.routine_notes(bank) if status == "rework"}
        if ctx.multi:
            far = getattr(kb, "_far_calls", None)
            if far is None:
                from .banking import far_calls
                far = kb._far_calls = far_calls(kb)     # the same for every bank's context
            for f in far:
                if f.bank == bank:
                    ctx.far_out[f.caller].append((f.target_bank, f.target, f.site))
                if f.target_bank == bank:
                    ctx.far_in[f.target].append((f.bank, f.caller, f.site))
        for caller, callee, site, kind in kb.calls(bank):
            if callee in ctx.routines and callee != caller:
                ctx.callers[callee].append((caller, site, kind))
                ctx.callees[caller].append((callee, site, kind))
        for entry, (last, _) in ctx.routines.items():
            # A routine that runs straight into the next one depends on it like a call.
            if ctx.falls_through(entry) and last + 1 in ctx.routines:
                ctx.callers[last + 1].append((entry, last, "fall"))
                ctx.callees[entry].append((last + 1, last, "fall"))
        return ctx

    def falls_through(self, entry: int) -> bool:
        last = self.routines[entry][0]
        if self.kinds.get(last) == "data":
            return True  # a BIT-skip byte: execution continues past the hidden instruction
        starts = _instruction_starts(self, entry, last)
        return bool(starts) and OPCODES[self.prg[max(starts) - self.base]].mnemonic not in TERMINAL

    def executed(self, addr: int) -> bool:
        return self.cdl is not None and bool(self.cdl[addr - self.base] & CDL_CODE)

    def owner(self, addr: int) -> int | None:
        """Entry of the routine whose extent contains addr."""
        for entry, (last, _) in self.routines.items():
            if entry <= addr <= last:
                return entry
        return None

    def blockers(self, entry: int) -> int:
        return len({c for c, _, _ in self.callees[entry] if c not in self.notes})

    def ref(self, addr: int) -> str:
        """How a proposal names this address: "$8539", or "B1:$8539" when banks share addresses."""
        return f"B{self.bank}:${addr:04X}" if self.multi else f"${addr:04X}"


def contexts(kb: KB) -> list[Context]:
    return [Context.load(kb, b) for b in banks(kb)]


TINY = 3  # instructions; such routines are usually alternate entry points into a neighbour


def _queue_key(ctx: Context, entry: int):
    last = ctx.routines[entry][0]
    n = len(_instruction_starts(ctx, entry, last))
    size = last - entry + 1
    proven = sum(ctx.executed(x) for x in range(entry, last + 1)) / size
    return entry not in ctx.rework, ctx.blockers(entry), n <= TINY, -round(proven, 1), -min(n, 40), entry


def _pending(ctx: Context) -> list[int]:
    return [e for e in ctx.routines if e not in ctx.notes or e in ctx.rework]


def queue(ctx: Context) -> list[int]:
    """Unannotated routines, bottom-up along the call graph.

    Among routines whose callees are all annotated, substantial ones come first (they carry
    the most evidence); tiny fragments come last, and are also shown inside the work unit of
    the routine they fall into so they can be named together.
    """
    # Routines the reviewer sent back come first; they still count as annotated for blocking.
    return sorted(_pending(ctx), key=lambda e: _queue_key(ctx, e))


def queue_all(ctxs: list[Context]) -> list[tuple[Context, int]]:
    """queue() over every bank, merged (an AxROM bank's call graph stays inside the bank)."""
    return sorted(((c, e) for c in ctxs for e in _pending(c)), key=lambda ce: (_queue_key(*ce), ce[0].bank))


def _instruction_starts(ctx: Context, entry: int, last: int) -> set[int]:
    starts, pc = set(), entry
    while pc <= last:
        if ctx.kinds.get(pc) == "op":
            starts.add(pc)
            pc += OPCODES[ctx.prg[pc - ctx.base]].size
        else:
            pc += 1
    return starts


def _evidence_note(checked: dict, addr: int) -> str:
    """", evidence 0.35" for a RAM label with a stored evidence score (confidence.py)."""
    return f", evidence {checked[addr][0]:.2f}" if addr in checked else ""


def _experiment_note(kb: KB, addr: int) -> str:
    runs = kb.experiments(NON_ROM, addr)
    if not runs:
        return ""
    failed = sum(1 for *_, passed in runs if not passed)
    passed = len(runs) - failed
    parts = ([f"{passed} exp passed"] if passed else []) + ([f"{failed} exp failed"] if failed else [])
    return ", " + ", ".join(parts)


@dataclass
class Listing:
    lines: list[str] = field(default_factory=list)
    ram_use: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    data_refs: set[int] = field(default_factory=set)
    exits: set[int] = field(default_factory=set)  # branch/jump targets outside [start, last]


def _listing(ctx: Context, sym, labels, comments, start: int, last: int,
             max_instructions: int | None = None, stop_at_terminal: bool = False) -> Listing:
    out = Listing()
    pc, count = start, 0
    while pc <= last and (max_instructions is None or count < max_instructions):
        if pc in labels and pc != start:
            out.lines.append(f"{labels[pc].name}:")
        note = next((t for k, t in comments.get(pc, []) if k == "line"), "")
        if ctx.kinds.get(pc) != "op":
            if ctx.kinds.get(pc) != "data":
                break
            out.lines.append(f"${pc:04X}  .byte ${ctx.prg[pc - ctx.base]:02X}"
                             + (f" ; {note}" if note else "      ; skip byte (BIT trick)"))
            pc += 1
            continue
        op = OPCODES[ctx.prg[pc - ctx.base]]
        value = operand_value(op, ctx.prg, pc - ctx.base)
        text, auto = sym.instruction(ctx.prg, ctx.base, pc)
        mark = " " if ctx.executed(pc) else "?"
        remark = note or auto
        out.lines.append(f"${pc:04X}{mark} {text:<28} ; {remark}" if remark else f"${pc:04X}{mark} {text}")
        if op.mode == REL or (op.mnemonic == "jmp" and op.mode != IND):
            target = branch_target(pc, value) if op.mode == REL else value
            if not start <= target <= last:
                out.exits.add(target)
        elif value is not None and op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES and op.mode != IND:
            if is_ram(value):
                out.ram_use[value].add("rw" if op.mnemonic in READ_WRITES else
                                       "w" if op.mnemonic in WRITES else "r")
            elif ctx.base <= value < ctx.base + len(ctx.prg) and ctx.kinds.get(value) != "op":
                out.data_refs.add(value)
        pc += op.size
        count += 1
        if stop_at_terminal and op.mnemonic in TERMINAL:
            break
    return out


def work_unit(ctx: Context, entry: int) -> str:
    kb, sym = ctx.kb, symbols_for(ctx.kb, ctx.bank)
    last, how = ctx.routines[entry]
    size = last - entry + 1
    proven = sum(ctx.executed(x) for x in range(entry, last + 1))
    labels = {l.addr: l for l in kb.labels(ctx.bank)}
    other = {l.addr: l for l in kb.labels(NON_ROM)}
    comments = kb.comments(ctx.bank)
    reads = kb.counters("nesInternalRam", "readCount") or []
    writes = kb.counters("nesInternalRam", "writeCount") or []

    def title(addr: int) -> str:
        return labels[addr].name if addr in labels else f"${addr:04X}"

    def note_of(e: int) -> str:
        return ctx.notes.get(e, "(not annotated yet)")

    out = [f"## Routine {title(entry)}  {ctx.ref(entry)}-${last:04X}",
           f"{size} bytes, {proven * 100 // size}% executed during exploration, found via {how}."]
    if ctx.multi:
        out.append(f"Bank {ctx.bank} (every bank sits at ${ctx.base:04X}): give the routine as \"{ctx.ref(entry)}\" "
                   "in proposals; lines, labels and data inside it are in the same bank.")
    callers = sorted((c, s, k) for c, s, k in ctx.callers.get(entry, []) if k != "fall")
    if callers:
        out.append("Called from: " + ", ".join(f"{title(c)} (${s:04X} {k})" for c, s, k in callers[:12])
                   + (f" ... +{len(callers) - 12} more" if len(callers) > 12 else ""))
    elif not ctx.far_in.get(entry):
        out.append("Called from: (no known JSR/JMP/table callers: a vector, fall-through, or a computed jump)")
    callees = sorted({(c, k) for c, _, k in ctx.callees.get(entry, [])})
    if callees:
        out.append("Calls:")
        out += [f"- {title(c)} ({k}): {note_of(c)}" for c, k in callees]
    if ctx.far_out.get(entry) or ctx.far_in.get(entry):
        out += _far_lines(ctx, entry)

    main = _listing(ctx, sym, labels, comments, entry, last)
    out += ["", "### Code", "```", *main.lines, "```"]
    if ctx.falls_through(entry):
        out.append(f"(Falls through into {title(last + 1)} at ${last + 1:04X}.)")
    out.append("Lines marked `?` were never executed during exploration (decoded statically).")

    # Alternate entry points that run into this routine (chains of fall-throughs).
    chain, frontier = [], [entry]
    while frontier and len(chain) < 6:
        target = frontier.pop()
        for c, _, k in ctx.callers.get(target, []):
            if k == "fall" and c not in chain:
                chain.append(c)
                frontier.append(c)
    if chain:
        out += ["", "### Alternate entries that fall into this routine",
                "Fragments that run into the code above. If not yet annotated, you may add proposals",
                "for them in the same batch."]
        for c in sorted(chain):
            frag = _listing(ctx, sym, labels, comments, c, ctx.routines[c][0])
            status = f"annotated: {ctx.notes[c]}" if c in ctx.notes else "not annotated"
            out += [f"{title(c)} ${c:04X} ({status})", "```", *frag.lines, "```"]
            for addr, uses in frag.ram_use.items():
                main.ram_use[addr] |= uses
            main.data_refs |= frag.data_refs

    # Code at jumps and branches that leave the routine.
    outside = sorted(main.exits)
    if outside:
        out += ["", "### Code at jumps/branches leaving this routine"]
        for t in outside[:6]:
            owner = ctx.owner(t)
            if owner is None:
                out.append(f"{title(t)} ${t:04X}: not inside any known routine")
                continue
            where = "entry of" if owner == t else "inside"
            out.append(f"{title(t)} ${t:04X}: {where} {title(owner)} - {note_of(owner)}")
            if owner != t or owner not in ctx.notes:
                snippet = _listing(ctx, sym, labels, comments, t, ctx.routines[owner][0],
                                   max_instructions=10, stop_at_terminal=True)
                out += ["```", *snippet.lines, "```"]
        if len(outside) > 6:
            out.append(f"... +{len(outside) - 6} more targets")

    if main.ram_use:
        from .confidence import scores
        checked = scores(kb, NON_ROM)
        out += ["", "### RAM it touches", "addr   here    label [status]                      game-wide reads/writes"]
        for addr in sorted(main.ram_use):
            l = other.get(addr)
            inner = sym.data_name(addr)
            name = f"{l.name} [{l.status}{_experiment_note(kb, addr)}{_evidence_note(checked, addr)}]" if l else (
                f"{inner} (inside a named region)" if inner else "-")
            rw = f"{reads[addr]}/{writes[addr]}" if addr < len(reads) else "n/a"
            out.append(f"${addr:04X}  {'/'.join(sorted(main.ram_use[addr])):<6}  {name:<34} {rw}")
    if main.data_refs:
        out += ["", "### ROM data it references"]
        for addr in sorted(main.data_refs):
            off = addr - ctx.base
            flag = "read as data during play" if ctx.cdl and ctx.cdl[off] & CDL_DATA else "never read during play"
            preview = " ".join(f"{b:02X}" for b in ctx.prg[off:off + 16])
            out.append(f"${addr:04X} {title(addr)}: {preview} ... ({flag})")
            blocks = metatiles.describe(kb, addr)
            if blocks:
                out.append(f"  {blocks}: open the sheet to see what the entries are")
        for t in metatiles.reached(ctx.prg, ctx.base, main.data_refs, metatiles.tables(kb)):
            out.append(f"  points to the {metatiles.describe(kb, t.addr)}: open the sheet to see what the entries are")
    out += _visual_sections(ctx, entry, other)
    from .library import section as library_section
    if ctx.bank == PRG_BANK:                  # library matches are stored for bank 0 only
        out += library_section(kb, entry)
    out += _unlabeled_ram_section(ctx, entry, main.ram_use, other, sym)
    out += _review_sections(kb, entry, main.ram_use, labels, other, ctx.bank)
    return "\n".join(out)


def _far_lines(ctx: Context, entry: int) -> list[str]:
    """Calls into and out of other banks, through the bank-switch trampoline."""
    kb = ctx.kb

    def other(bank: int, addr: int) -> str:
        l = kb.label_at(bank, addr)
        note = kb.db.execute("SELECT summary FROM routine_notes WHERE bank=? AND entry=?", (bank, addr)).fetchone()
        return f"{l.name if l else f'B{bank}:${addr:04X}'} (bank {bank}): {note[0] if note else '(not annotated yet)'}"
    out = []
    calls = sorted(set(ctx.far_out.get(entry, [])))
    if calls:
        out.append("Calls in other banks (through the bank switch; the call site, then where it lands):")
        out += [f"- ${site:04X} -> {other(b, t)}" for b, t, site in calls]
    callers = sorted(set(ctx.far_in.get(entry, [])))
    if callers:
        out.append("Called from other banks: " + ", ".join(
            f"{(kb.label_at(b, c).name if kb.label_at(b, c) else f'B{b}:${c:04X}')} (bank {b}, ${s:04X})"
            for b, c, s in callers[:12]) + (f" ... +{len(callers) - 12} more" if len(callers) > 12 else ""))
    return out


def ram_writers(ctx: Context) -> dict[int, set[int]]:
    """RAM address -> routines whose code writes it (static operands; cached on the context)."""
    cached = getattr(ctx, "_ram_writers", None)
    if cached is not None:
        return cached
    writers: dict[int, set[int]] = defaultdict(set)
    for entry, (last, _) in ctx.routines.items():
        pc = entry
        while pc <= last:
            if ctx.kinds.get(pc) != "op":
                pc += 1
                continue
            op = OPCODES[ctx.prg[pc - ctx.base]]
            value = operand_value(op, ctx.prg, pc - ctx.base)
            if (op.mnemonic in WRITES or (op.mnemonic in READ_WRITES and op.mode != "acc")) \
                    and value is not None and op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES and is_ram(value):
                writers[value].add(entry)
            pc += op.size
    ctx._ram_writers = writers
    return writers


ROLE_WORDS = {"timer", "counter", "count", "speed", "flag", "state", "dir", "direction", "facing", "position", "pos",
              "offset", "index", "size", "height", "width", "lives", "score", "mode", "type", "kind", "health",
              "length", "delay", "phase", "step", "frame", "accel", "gravity", "limit", "range"}
# role words that also lead a routine name as its verb (StepRamp, CountLives, OffsetSprites)
VERB_ROLE_WORDS = {"step", "count", "offset", "index", "size", "limit", "range", "delay", "score", "position",
                   "flag", "type", "phase", "time", "frame"}


def _words(name: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?![a-z])", name)}


def _unlabeled_ram_section(ctx: Context, entry: int, ram_use, other, sym) -> list[str]:
    """Unlabeled RAM this routine writes, with how widely the game writes it (backlog item 2)."""
    writers = ram_writers(ctx)
    rows = []
    for addr in sorted(ram_use):
        if addr in other or sym.data_name(addr) or not ram_use[addr] & {"w", "rw"}:
            continue
        who = writers.get(addr, set())
        annotated = len(who & set(ctx.notes))
        rows.append((len(who), f"- ${addr:04X}: written by {len(who)} routines ({annotated} annotated)"))
    if not rows:
        return []
    rows.sort(key=lambda r: -r[0])
    return ["", "### Unlabeled RAM this routine writes",
            "Don't give these a meaning in names or summaries (\"timer\", \"speed\"...): say what is stored",
            "(\"sets $049A,x to 9\"), or propose a RAM label with low confidence so an experiment can test it.",
            *[r for _, r in rows[:10]]]


def _visual_sections(ctx: Context, entry: int, other) -> list[str]:
    """Pictures the annotator can open: jump-table states, values compared here, sprite tiles (items 1, 4)."""
    from . import visual

    kb, out = ctx.kb, []
    export = visual.export_dir(kb)
    sites = getattr(ctx, "_dispatches", None)
    if sites is None:
        sites = ctx._dispatches = visual.dispatches(ctx)
    lines = []
    for d in sites:
        here = [i for i, h in enumerate(d.handlers) if h == entry]
        if not here and d.owner != entry:
            continue
        var = "an unknown index" if d.index_addr is None else (
            (other[d.index_addr].name if d.index_addr in other else f"${d.index_addr:04X}") + (f",{d.indexed}" if d.indexed else ""))
        images = visual.probe_images(kb, d.index_addr, d.site) if d.index_addr is not None else {}
        for i in here:
            v = d.value(i)
            img = images.get(v, {})
            lines.append(f"- entry {i} of the jump table at ${d.site:04X} (selected by {var}"
                         + (f" = ${v:02X})" if d.index_addr is not None else ")")
                         + (f"; screen with it held there: {img.get('crop') or img.get('full')}" if img else ""))
        if d.owner == entry and images:
            names = {l.addr: l.name for l in kb.labels(ctx.bank)}
            lines.append(f"- this routine dispatches on {var}; what each value looks like:")
            for i, h in enumerate(d.handlers[:20]):
                img = images.get(d.value(i), {})
                if img:
                    lines.append(f"    ${d.value(i):02X} -> {names.get(h, f'${h:04X}')}: {img.get('crop') or img.get('full')}")
        if (here or d.owner == entry) and d.index_addr is not None and not images:
            lines.append(f"  (not probed yet: `python -m renes visual <kb> probe ${d.site:04X}`)")
    if lines:
        out += ["", "### Where this routine sits in a jump table", *lines,
                "Open the image files with the Read tool: they show the screen with the variable held at that value."]
    seen, compared = set(), []
    code = visual._decode(ctx, entry, ctx.routines[entry][0] + 1)
    for k, (_, op, value) in enumerate(code):
        if op.mnemonic in ("lda", "ldx", "ldy") and op.mode != "imm" and value is not None and is_ram(value):
            for _, op2, v2 in code[k + 1:k + 4]:
                if op2.mnemonic in ("cmp", "cpx", "cpy") and op2.mode == "imm" and (value, v2) not in seen:
                    seen.add((value, v2))
                    img = visual.probe_images(kb, value).get(v2, {})
                    if img:
                        name = other[value].name if value in other else f"${value:04X}"
                        compared.append(f"- {name} = ${v2:02X}: {img.get('crop') or img.get('full')}")
                    break
    if compared:
        out += ["", "### Values compared here, as they look on screen", *compared]
    tiles = visual.tiles_written(ctx, entry)
    if tiles and export and (export / "tiles").exists():
        out += ["", "### Sprite tiles this routine writes (open the images)",
                *[f"- tile ${t:02X}: {export / 'tiles' / 'p0' / f'{t:02X}.png'} (or p1/ for the other pattern table)" for t in tiles[:12]]]
    return out


def _review_sections(kb: KB, entry: int, ram_use, labels, other, bank: int = PRG_BANK) -> list[str]:
    """Reviewer hints, knock-out evidence and relevant game-brief facts for a work unit."""
    out = []
    note = kb.db.execute("SELECT summary, status FROM routine_notes WHERE bank=? AND entry=?",
                         (bank, entry)).fetchone()
    if note and note[1] == "rework":
        out += ["", "### Sent back by the reviewer: revise this annotation",
                f"Current summary: {note[0]}", "Keep what is right; fix what the hints say. Re-propose the whole routine."]
    hints = [("routine", t, s) for t, s in kb.hints(bank, entry)]
    hints += [(other[a].name if a in other else f"${a:04X}", t, s) for a in sorted(ram_use) for t, s in kb.hints(NON_ROM, a)]
    human = [h for h in hints if h[2] not in ("rework", "audit")]
    if human:
        out += ["", "### Reviewer hints (from the human: follow them)"]
        out += [f"- {what}: {text}" for what, text, _ in human]
    auto = [text for _, text, source in hints if source == "rework"]
    if auto:
        out += ["", "### Why this name was sent back (automatic: something it depends on is now known)"]
        out += [f"- {text}" for text in auto]
    audited = [f"{what}: {text}" for what, text, source in hints if source == "audit"]
    if audited:
        out += ["", "### An independent audit found the code doesn't support this (fix it, or show why it's right)"]
        out += [f"- {text}" for text in audited]
    ko = kb.db.execute("SELECT frames, screen_differs, effect FROM knockouts WHERE bank=? AND entry=? "
                       "ORDER BY id DESC LIMIT 1", (bank, entry)).fetchone()
    if ko:
        frames, differs, effect = ko
        effect = json.loads(effect)
        diffs, other_bytes = effect["labeled"], effect["other_bytes"]
        out += ["", "### Knock-out (this routine patched to return immediately)",
                f"Over {frames} frames: screen {'differs' if differs else 'is unchanged'}; "
                + ("labeled RAM that differs (normal->knocked): " + ", ".join(
                    f"{d['name']} {d['normal']:02X}->{d['knocked']:02X}" for d in diffs[:10]) if diffs
                   else "no labeled RAM differs") + f"; {other_bytes} unlabeled RAM bytes differ"]
    brief = kb.db.execute("SELECT kind, text, source FROM brief WHERE status='accepted'").fetchall()
    if brief:
        from .benchmark import tokens
        words = set()
        for l in [labels.get(entry)] + [other.get(a) for a in ram_use]:
            if l is not None:
                words |= tokens(l.name)
        scored = sorted(((len(words & tokens(text)), kind, text) for kind, text, _ in brief), reverse=True)
        relevant = [(k, t) for n, k, t in scored if n][:4]
        if relevant:
            out += ["", "### Game brief (facts from the manual/walkthroughs that may apply)"]
            out += [f"- [{k}] {t}" for k, t in relevant]
    return out


# ---------------------------------------------------------------------------------------------
# Proposals


@dataclass
class Report:
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # kept, but worth fixing (shown as `?`)
    requeued: list[str] = field(default_factory=list)   # weak names this batch made fixable (rework.py)


def _check_name(kb: KB, name: str, bank: int, addr: int) -> str | None:
    if not NAME_RE.match(name):
        return "must be 2-40 chars of letters, digits, underscore, not starting with a digit"
    if name.lower() in RESERVED:
        return "is a reserved word (6502 mnemonic or register)"
    if AUTO_NAME_RE.match(name):
        return "looks like an auto-generated name"
    other = kb.label_named(name)
    if other and (other.bank, other.addr) != (bank, addr):
        return f"already names ${other.addr:04X}"
    current = kb.label_at(bank, addr)
    if current and current.status in PROTECTED and current.name != name:
        return f"would overwrite {current.status} label {current.name}"
    return None


def _set_label(kb: KB, r: Report, bank: int, addr: int, name: str, kind: str, confidence,
               size: int | None = None, why: str = "") -> None:
    problem = _check_name(kb, name, bank, addr)
    if problem and problem.startswith("would overwrite"):
        # only a person renames a verified name: keep the new one as a proposal for review
        kb.propose_rename(bank, addr, kb.label_at(bank, addr).name, name, clean_text(why, MAX_SUMMARY),
                          os.environ.get("RENES_AGENT"), datetime.now(timezone.utc).isoformat(timespec="seconds"))
        r.rejected.append(f"label {name} @ ${addr:04X}: {problem}; proposed for review (dashboard Questions tab)")
        return
    if problem:
        r.rejected.append(f"label {name} @ ${addr:04X}: {problem}")
        return
    current = kb.label_at(bank, addr)
    if current and current.status in PROTECTED:
        # same name as a verified/approved label: keep it, but its extent may still be corrected
        if size is not None and size != (current.size or 1):
            kb.add_label(Label(bank, addr, current.name, current.kind, current.status, current.source,
                               current.confidence, size), replace=True)
            r.accepted.append(f"size of {current.status} label {current.name}: {current.size or 1} -> {size} bytes")
        else:
            r.accepted.append(f"label ${addr:04X} = {name} (already {current.status}, unchanged)")
        return
    size = size if size is not None else (current.size if current else None)
    kb.add_label(Label(bank, addr, name, current.kind if current else kind, "candidate", SOURCE, confidence, size),
                 replace=True)
    r.accepted.append(f"label ${addr:04X} = {name}" + (f" ({size} bytes)" if size and size > 1 else ""))
    if current and current.name != name and current.status != "auto":
        n = kb.propagate_name(current.name, name)
        if n:
            r.accepted.append(f"renamed {current.name} -> {name}: {n} summaries/comments updated")


def unbacked_roles(ctx: Context, entry: int, name: str, labeled: dict, proposed: set = frozenset()
                   ) -> tuple[set[str], list[int]]:
    """Role words in `name` ("Timer", "Speed") that no labeled RAM the routine writes backs, and
    the unlabeled RAM it writes (both empty when it writes no unlabeled RAM)."""
    covering = dict(labeled)
    for l in labeled.values():               # an address inside a sized label is labeled (Timers+11)
        for a in range(l.addr + 1, l.addr + (l.size or 1)):
            covering.setdefault(a, l)
    writes = {a for a, who in ram_writers(ctx).items() if entry in who}
    unlabeled = sorted(a for a in writes if a not in covering and a not in proposed)
    if not unlabeled or not name:
        return set(), unlabeled
    backed = set().union(*(_words(covering[a].name) for a in writes if a in covering))
    backed |= {w[:-1] for w in backed if w.endswith("s")}          # Timers backs Timer
    words = _words(name)
    lead = re.match(r"[A-Z][a-z]+", name)
    if lead and lead.group().lower() in VERB_ROLE_WORDS:            # StepRamp: Step is the verb, not a role
        words = _words(name[lead.end():])
    return (words & ROLE_WORDS) - backed, unlabeled


def _warn_unbacked_roles(ctx: Context, entry: int, names: list[str], proposed_ram: set, r: Report) -> None:
    """Names that claim a role ("Timer", "Speed") the routine could only get from unlabeled RAM."""
    labeled = {l.addr: l for l in ctx.kb.labels(NON_ROM)}
    proposed = {parse_addr(a) for a in proposed_ram}
    for name in names:
        claims, unlabeled = unbacked_roles(ctx, entry, name, labeled, proposed)
        if claims:
            r.warnings.append(f"{name} says '{', '.join(sorted(claims))}', but the only RAM it writes that could be that is "
                              f"unlabeled ({', '.join(f'${a:04X}' for a in unlabeled[:4])}): propose a RAM label so it "
                              f"gets tested, or name what is stored")


def _apply_ram(kb: KB, ram: dict, r: Report, conf) -> None:
    for addr_text, item in ram.items():
        addr = parse_addr(addr_text)
        if not is_ram(addr):
            r.rejected.append(f"ram ${addr:04X}: not a RAM address")
            continue
        item = item if isinstance(item, dict) else {"name": item}
        size = item.get("size")
        if size is not None:
            size = int(size)
            if not 1 <= size <= 256 or not is_ram(addr + size - 1):
                r.rejected.append(f"ram ${addr:04X} size {size}: must be 1-256 bytes and stay in RAM")
                size = None
        if item.get("name"):
            _set_label(kb, r, NON_ROM, addr, item["name"], "ram", conf, size,
                       why=item.get("why") or item.get("comment") or "")
        if item.get("comment"):
            kb.delete_comments(NON_ROM, addr, "line", SOURCE)
            kb.add_comment(NON_ROM, addr, "line", _fit(r, f"ram comment ${addr:04X}", item["comment"]),
                           "candidate", SOURCE)
            r.accepted.append(f"ram comment ${addr:04X}")
        if size is not None and not item.get("name"):
            r.rejected.append(f"ram ${addr:04X}: a size needs the label's name (repeat the current one to keep it)")


def resolve_routine(kb: KB, ctxs: list[Context], text) -> tuple[int, int] | str:
    """(bank, entry) for "B1:$8539", "$8539" or a routine's current name; else why not.
    A bare address is fine while only one bank has a routine there."""
    try:
        bank, addr = parse_ref(text)
    except ValueError:                       # a routine's current name works as well as its address
        named = kb.label_named(str(text).strip())
        if named is None or named.bank not in {c.bank for c in ctxs}:
            return f"{text}: neither an address nor a routine name"
        bank, addr = named.bank, named.addr
    where = [c.bank for c in ctxs if (bank is None or c.bank == bank) and addr in c.routines]
    if not where:
        return f"{text} is not a routine entry"
    if len(where) > 1:
        return f"{text}: banks {', '.join(map(str, where))} all have a routine there; write it as B{where[0]}:${addr:04X}"
    return where[0], addr


def apply_one(kb: KB, ctx: Context, p: dict, r: Report) -> None:
    if not p.get("routine"):
        # RAM label fixes (name, size, comment) on their own: no routine is re-annotated
        if not p.get("ram") or set(p) - {"ram", "confidence"}:
            r.rejected.append("a proposal needs a routine, or only \"ram\" entries (and confidence)")
            return
        conf = p.get("confidence")
        _apply_ram(kb, p["ram"], r, float(conf) if conf is not None else None)
        return
    bank = ctx.bank
    entry = p["_entry"] if "_entry" in p else resolve_routine(kb, [ctx], p["routine"])
    if isinstance(entry, str):
        r.rejected.append(entry)
        return
    if isinstance(entry, tuple):
        entry = entry[1]
    starts = _instruction_starts(ctx, entry, ctx.routines[entry][0])
    conf = p.get("confidence")
    conf = float(conf) if conf is not None else None

    summary = _fit(r, f"summary ${entry:04X}", p.get("summary") or "", MAX_SUMMARY)
    if not summary:
        r.rejected.append(f"${entry:04X}: summary is required")
        return
    if p.get("name"):
        _set_label(kb, r, bank, entry, p["name"], "routine", conf, why=p.get("why") or p.get("summary") or "")
    _warn_unbacked_roles(ctx, entry, [p.get("name") or ""] + list((p.get("labels") or {}).values()),
                         set((p.get("ram") or {})), r)
    for addr_text, name in (p.get("labels") or {}).items():
        addr = parse_addr(addr_text)
        if addr not in starts or addr == entry:
            r.rejected.append(f"local label {name} @ ${addr:04X}: not an instruction start inside the routine")
            continue
        _set_label(kb, r, bank, addr, name, "code", conf)
    for addr_text, text in (p.get("lines") or {}).items():
        addr = parse_addr(addr_text)
        if addr not in starts:
            r.rejected.append(f"comment @ ${addr:04X}: not an instruction start inside the routine")
            continue
        kb.delete_comments(bank, addr, "line", SOURCE)
        if not clean_text(text):                    # "" removes an earlier comment
            r.accepted.append(f"comment ${addr:04X} removed")
            continue
        kb.add_comment(bank, addr, "line", _fit(r, f"comment ${addr:04X}", text), "candidate", SOURCE)
        r.accepted.append(f"comment ${addr:04X}")
    if "notes" in p:
        kb.delete_comments(bank, entry, "block", SOURCE)
        for n, line in enumerate(p.get("notes") or [], 1):
            kb.add_comment(bank, entry, "block", _fit(r, f"note {n} of ${entry:04X}", line), "candidate", SOURCE)
    _apply_ram(kb, p.get("ram") or {}, r, conf)
    for addr_text, name in (p.get("data") or {}).items():
        addr = parse_addr(addr_text)
        if not (ctx.base <= addr < ctx.base + len(ctx.prg)) or ctx.kinds.get(addr) not in (None, "data", "word"):
            r.rejected.append(f"data label {name} @ ${addr:04X}: not the start of a data byte")
            continue
        _set_label(kb, r, bank, addr, name, "data", conf)
    kb.set_routine_note(bank, entry, summary, "candidate", SOURCE, conf,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    r.accepted.append(f"summary ${entry:04X}")


def apply(kb_path: Path, proposals: list[dict], out_dir: Path) -> tuple[Report, bool]:
    """Apply proposals atomically; keeps them only if the ROM still reassembles identically."""
    r = Report()
    kb = KB(kb_path)
    try:
        ctxs = {c.bank: c for c in contexts(kb)}
        for p in proposals:
            try:
                if not p.get("routine"):
                    apply_one(kb, ctxs[min(ctxs)], p, r)     # RAM-only fixes: no bank involved
                    continue
                found = resolve_routine(kb, list(ctxs.values()), p["routine"])
                if isinstance(found, str):
                    r.rejected.append(found)
                    continue
                apply_one(kb, ctxs[found[0]], {**p, "_entry": found[1]}, r)
            except (KeyError, ValueError, TypeError) as e:
                r.rejected.append(f"malformed proposal {p.get('routine', '?')}: {e!r}")
        rom = kb.rom()
        try:
            result = build_and_verify(render(kb), rom.raw, out_dir, kb_path.stem)
            ok = result.identical
            if not ok:
                r.rejected.append(f"ROM no longer identical (first diff ${result.first_diff:X}); rolled back")
        except BuildError as e:
            ok = False
            r.rejected.append(f"assembly failed, rolled back: {str(e).strip()[:400]}")
        if ok:
            from . import confidence, rework
            names = {l.addr: l.name for b in ctxs for l in kb.labels(b)}
            r.requeued = [f"{names.get(e, f'${e:04X}')}: {why}" for e, why in rework.scan(kb)]
            confidence.refresh(kb)
            kb.commit()
        else:
            kb.db.rollback()
        return r, ok
    finally:
        kb.db.close()


def progress(kb: KB) -> dict[str, int]:
    ctxs = contexts(kb)
    named = [l for c in ctxs for l in kb.labels(c.bank)
             if l.kind in ("routine", "vector") and not AUTO_NAME_RE.match(l.name)]
    return {
        "routines": sum(len(c.routines) for c in ctxs),
        "annotated": sum(len(c.notes) for c in ctxs),
        "named_routines": len(named),
        "ram_labels": sum(1 for l in kb.labels(NON_ROM) if l.kind == "ram"),
        "line_comments": kb.db.execute("SELECT count(*) FROM comments WHERE source=? AND kind='line'",
                                       (SOURCE,)).fetchone()[0],
        "ready_now": sum(1 for c, e in queue_all(ctxs) if c.blockers(e) == 0),
    }


def load_proposals(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]
