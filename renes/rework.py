"""Automatic rework of weak routine names (backlog item 5).

About 10% of the benchmark game's names were placeholders that describe mechanics instead of meaning:
`Store0InByte39` (a raw address where a variable name belongs), `SetAYTo1ThenContinue` (says
nothing about what runs next). They were the best possible names when written, because the
RAM or code they lean on had no name yet. Once it does, the routine goes back to the front of
the annotation queue (status "rework") with a hint that says what changed.

Each trigger is recorded as a hint (source "rework"), so the same reason never re-queues a
routine twice: a re-annotation that keeps the weak name is accepted.
"""
import re
from datetime import datetime, timezone

from .annotate import AUTO_NAME_RE, Context, _instruction_starts, _listing, contexts
from .kb import KB, NON_ROM
from .opcodes import OPCODES, REL, branch_target, operand_value
from .render import symbols_for

SOURCE = "rework"
HEX_TOKEN = re.compile(r"(?<![0-9A-F])(?=[0-9A-F]*[0-9])[0-9A-F]{2,4}(?![0-9A-Fa-z])")   # "Byte39", not "Sprite0Clear"
CONTINUE_RE = re.compile(r"(Then|And)?(Continue|Fallthrough|FallThrough|FallInto)([0-9A-F]{2,4})?$")


def _real_name(name: str | None) -> bool:
    return bool(name) and not AUTO_NAME_RE.match(name)


def _next_routine(ctx: Context, entry: int) -> tuple[int, int] | None:
    """Where execution goes after this routine: (target, routine containing it), from a
    fall-through, or a JMP/branch as the last instruction (often into the middle of a routine)."""
    last = ctx.routines[entry][0]
    if ctx.falls_through(entry) and last + 1 in ctx.routines:
        return last + 1, last + 1
    starts = _instruction_starts(ctx, entry, last)
    if starts:
        pc = max(starts)
        op = OPCODES[ctx.prg[pc - ctx.base]]
        value = operand_value(op, ctx.prg, pc - ctx.base)
        target = (branch_target(pc, value) if op.mode == REL
                  else value if op.mnemonic == "jmp" and op.mode == "abs" else None)
        owner = ctx.owner(target) if target is not None and not entry <= target <= last else None
        if owner is not None:
            return target, owner
    return None


def placeholder_parts(name: str) -> list[str]:
    """The parts of a name that mark it as a placeholder, whether or not a fix is possible yet."""
    if AUTO_NAME_RE.match(name):
        return []
    parts = [f"raw number ${t}" for t in HEX_TOKEN.findall(name)]
    if CONTINUE_RE.search(name):
        parts.append("'continue' without saying into what")
    return parts


def _value_pictures(ctx: Context, entry: int, value: int, ram_use, ram_labels) -> list[str]:
    """A number in the name that the routine stores into or compares with a variable whose
    probe shows that value on screen: the picture says what the number stands for."""
    from . import visual

    code = visual._decode(ctx, entry, ctx.routines[entry][0] + 1)
    used = any(op.mode == "imm" and v == value for _, op, v in code)
    out = []
    for addr in sorted(ram_use) if used else []:
        img = visual.probe_images(ctx.kb, addr).get(value)
        if img:
            name = ram_labels[addr].name if addr in ram_labels else f"${addr:04X}"
            out.append(f"the name uses the number ${value:02X}; a probe shows what {name} = ${value:02X} looks "
                       f"on screen ({img.get('crop') or img.get('full')}): name the thing it is")
    return out


def reasons(ctx: Context, entry: int, labels: dict, ram_labels: dict) -> list[str]:
    """Why this routine's name can now be improved (empty: keep it)."""
    name = labels[entry].name if entry in labels else None
    if not _real_name(name) or entry not in ctx.notes or entry not in ctx.routines:
        return []
    out = []
    tokens = HEX_TOKEN.findall(name)
    if tokens:
        sym = symbols_for(ctx.kb, ctx.bank)
        use = _listing(ctx, sym, labels, {}, entry, ctx.routines[entry][0])
        nxt, hops = entry, 0
        while hops < 4 and ctx.falls_through(nxt) and ctx.routines[nxt][0] + 1 in ctx.routines:
            # "Store0InByte39" sets A and falls into the code that does the store
            nxt, hops = ctx.routines[nxt][0] + 1, hops + 1
            more = _listing(ctx, sym, labels, {}, nxt, ctx.routines[nxt][0])
            for a, u in more.ram_use.items():
                use.ram_use[a] |= u
            use.data_refs |= more.data_refs
        touched = set(use.ram_use) | use.data_refs
        for t in tokens:
            addr = int(t, 16)
            label = ram_labels.get(addr) or (labels.get(addr) if addr in use.data_refs else None)
            if addr in touched and label and _real_name(label.name):
                out.append(f"the name uses the raw address ${t}, which is now labeled {label.name}: "
                           f"name what the routine does with {label.name}")
            elif addr <= 0xFF:
                out += _value_pictures(ctx, entry, addr, use.ram_use, ram_labels)
    if CONTINUE_RE.search(name):
        found = _next_routine(ctx, entry)
        nxt = found[1] if found else None
        if nxt is not None and nxt in ctx.notes and nxt in labels and _real_name(labels[nxt].name) \
                and not placeholder_parts(labels[nxt].name):
            where = "" if found[0] == nxt else f" (at ${found[0]:04X}, inside it)"
            out.append(f"the name says it continues, and what it continues into is now known: "
                       f"{labels[nxt].name}{where}: {ctx.notes[nxt][:120]}. Name the combined effect")
    return out


NEVER_RAN = re.compile(r"\b(never (ran|runs|run|executed|seen|read|reached|called|set|taken)|"
                       r"not (executed|reached|seen)|unused|dead code|unreachable)\b", re.I)


def stale_claims(kb: KB, ctx: Context | None = None, ran: float = 0.9) -> list[tuple[int, str, float, str]]:
    """Annotated routines whose summary or comments say code never ran (or is unused) while coverage
    now shows at least `ran` of their bytes executed: true when written, stale after more playing.
    The last item says where the phrase is, e.g. 'line comment $CE8F "never seen"'."""
    if ctx is None:
        return sorted(row for c in contexts(kb) for row in stale_claims(kb, c, ran))
    comments = kb.comments(ctx.bank)
    names = {l.addr: l.name for l in kb.labels(ctx.bank)}
    out = []
    for entry, text in ctx.notes.items():
        if entry not in ctx.routines:
            continue
        last = ctx.routines[entry][0]
        texts = [("summary", text)] + [("note" if k == "block" and a == entry else f"{k} comment ${a:04X}", t)
                                       for a, items in sorted(comments.items()) if entry <= a <= last
                                       for k, t in items]
        found = next(((where, m.group(0)) for where, t in texts if (m := NEVER_RAN.search(t))), None)
        if found is None:
            continue
        share = sum(ctx.executed(x) for x in range(entry, last + 1)) / (last - entry + 1)
        if share >= ran:
            out.append((entry, names.get(entry, f"${entry:04X}"), share, f'{found[0]} "{found[1]}"'))
    return sorted(out)


def scan(kb: KB, ctx: Context | None = None) -> list[tuple[int, str]]:
    """Send routines with improvable placeholder names back to the queue. Returns (entry, reason)."""
    if ctx is None:
        return [row for c in contexts(kb) for row in scan(kb, c)]
    bank = ctx.bank
    labels = {l.addr: l for l in kb.labels(bank)}
    ram_labels = {l.addr: l for l in kb.labels(NON_ROM)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sent = []
    for entry in sorted(ctx.notes):
        label = labels.get(entry)
        if label is None or label.status in ("verified", "approved") or entry in ctx.rework:
            continue
        given = {text for text, source in kb.hints(bank, entry) if source == SOURCE}
        for reason in reasons(ctx, entry, labels, ram_labels):
            if reason in given:
                continue
            kb.add_hint(bank, entry, reason, SOURCE, now)
            kb.db.execute("UPDATE routine_notes SET status='rework' WHERE bank=? AND entry=?", (bank, entry))
            ctx.rework.add(entry)
            sent.append((entry, reason))
    return sent
