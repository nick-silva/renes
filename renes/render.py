"""Render the knowledge base to ca65 assembly plus an ld65 config.

Encoding fidelity rules (the round trip depends on them):
- Absolute-mode operands below $100 get an `a:` prefix, or ca65 would pick zero page.
- Every symbol is defined before code (equates at the top), so ca65 sizes operands correctly.
- Bytes not classified as code are emitted verbatim with `.byte`.
- Labels that cannot sit on their own line (inside an instruction) become `name = $addr`.
"""
import textwrap
from dataclasses import dataclass

from .analyze import PRG_BANK
from .kb import KB, NON_ROM
from .rom import MAPPERS, Bank
from .opcodes import ABSOLUTE_MODES, ACC, IMM, IMP, IND, IZX, IZY, OPCODES, REL, ZPX, ZPY, ABX, ABY, ZP, \
    Op, branch_target, operand_value

INDENT = " " * 13
COMMENT_COL = 42
BYTES_PER_LINE = 16
SUMMARY_WIDTH = 92
CHR_FILE = "chr.bin"


@dataclass(frozen=True)
class Rendered:
    asm: str
    cfg: str
    chr: bytes


class Symbols:
    """Operand formatting with the knowledge base's names (shared by renderer and work units).

    Data operands (not branch/jump targets) that fall inside a sized region (RAM arrays, ROM
    data tables) render as `Name+offset`.
    """

    def __init__(self, names: dict[int, str], ranges: list[tuple[int, int, str]] = ()):
        self.names = names
        self.ranges = sorted(ranges)  # (start, size, name)

    def data_name(self, value: int) -> str | None:
        if value in self.names:
            return self.names[value]
        best = None
        for start, size, name in self.ranges:
            if start > value:
                break
            if value < start + size:
                best = (start, name)  # latest start wins: the innermost region
        return f"{best[1]}+{value - best[0]}" if best else None

    def addr16(self, value: int) -> str:
        text = self.data_name(value) or f"${value:04X}"
        return f"a:{text}" if value < 0x100 else text

    def addr8(self, value: int) -> str:
        return self.data_name(value) or f"${value:02X}"

    def word(self, value: int) -> str:
        return self.names.get(value, f"${value:04X}")

    def operand(self, op: Op, value: int | None, pc: int) -> str:
        m = op.mode
        if m == IMP:
            return ""
        if m == ACC:
            return "a"
        if m == IMM:
            return f"#${value:02X}"
        if m == REL:
            return self.word(branch_target(pc, value))
        if m == ZP:
            return self.addr8(value)
        if m == ZPX:
            return f"{self.addr8(value)},x"
        if m == ZPY:
            return f"{self.addr8(value)},y"
        if m == IZX:
            return f"({self.addr8(value)},x)"
        if m == IZY:
            return f"({self.addr8(value)}),y"
        if m == IND:
            return f"({self.word(value)})"
        if m == ABX:
            return f"{self.addr16(value)},x"
        if m == ABY:
            return f"{self.addr16(value)},y"
        assert m in ABSOLUTE_MODES
        return self.addr16(value)

    def instruction(self, prg: bytes, base: int, pc: int) -> tuple[str, str | None]:
        """(text, automatic comment) for the instruction at pc."""
        off = pc - base
        op = OPCODES[prg[off]]
        value = operand_value(op, prg, off)
        if op.mode == IND and value & 0xFF == 0xFF:
            # ca65 rejects the 6502 page-wrap bug case; keep the exact bytes.
            raw = ",".join(f"${b:02X}" for b in prg[off:off + 3])
            return f".byte {raw}", f"jmp (${value:04X}) - pointer wraps within page ${value >> 8:02X}"
        return f"{op.mnemonic} {self.operand(op, value, pc)}".rstrip(), None


MAX_TABLE_SPAN = 256


def symbols_for(kb: KB, bank: int = PRG_BANK) -> Symbols:
    """Names for operands in `bank`: RAM and hardware, plus that bank's own ROM labels."""
    other, rom_labels = kb.labels(NON_ROM), kb.labels(bank)
    ranges = [(l.addr, l.size, l.name) for l in other if l.size and l.size > 1]
    # ROM data labels cover their bytes up to the next label or code (capped at one index range).
    rom = kb.rom()
    where = next(b for b in rom.banks() if b.index == bank)
    end = where.base + len(where.data)
    kinds = kb.classes(bank)
    starts = {l.addr for l in rom_labels}
    for l in rom_labels:
        if l.kind not in ("data", "table"):
            continue
        size = 1
        while (l.addr + size < end and size < MAX_TABLE_SPAN and l.addr + size not in starts
               and kinds.get(l.addr + size) in (None, "data", "word", "word_hi")):
            size += 1
        if size > 1:
            ranges.append((l.addr, size, l.name))
    return Symbols({l.addr: l.name for l in other + rom_labels}, ranges)


def _with_comment(line: str, comment: str | None) -> str:
    if not comment:
        return line
    return f"{line.ljust(COMMENT_COL - 1)} ;{comment}"


def render(kb: KB) -> Rendered:
    rom = kb.rom()
    if rom.mapper not in MAPPERS:
        raise NotImplementedError(f"mapper {rom.mapper}: supported are {', '.join(map(str, MAPPERS))}")
    banks = rom.banks()
    multi = len(banks) > 1
    other_labels = kb.labels(NON_ROM)
    other_comments = kb.comments(NON_ROM)

    out: list[str] = [
        f"; {rom.name}",
        f"; SHA-1 {rom.sha1}  mapper {rom.mapper}  PRG {len(rom.prg) // 1024}K  CHR {len(rom.chr) // 1024}K",
        "; Generated by renes from its knowledge base. Edit the KB, not this file.",
        "; Labels/comments marked ;? are unreviewed machine proposals.",
    ]
    for title, kind in (("HARDWARE", "hardware"), ("RAM", "ram")):
        section = [l for l in other_labels if l.kind == kind]
        if not section:
            continue
        out += ["", ";-------------------------------------------------------------------------------------",
                f";{title}", ""]
        for l in section:
            note = next((t for k, t in other_comments.get(l.addr, []) if k == "line"), None)
            if note and l.status in ("auto", "candidate"):
                note = f"? {note}"
            out.append(_with_comment(f"{l.name.ljust(COMMENT_COL - 1)} = ${l.addr:04X}", note))
    for bank in banks:
        kinds = kb.classes(bank.index)
        floating = [l for l in kb.labels(bank.index)
                    if not (bank.base <= l.addr < bank.base + len(bank.data)
                            and kinds.get(l.addr) in (None, "data", "op", "word"))]
        if floating:
            out += ["", ";labels that point inside an instruction" + (f" (bank {bank.index})" if multi else "")]
            out += [f"{l.name} = ${l.addr:04X}" for l in floating]

    out += ["", '.segment "HEADER"', ".byte " + ", ".join(f"${b:02X}" for b in rom.header)]
    for bank in banks:
        segment = f"BANK{bank.index}" if multi else "PRG"
        out += ["", ";" + "=" * 85, f";BANK {bank.index}: PRG ${bank.offset:05X}-${bank.offset + len(bank.data) - 1:05X}, "
                f"at ${bank.base:04X} while switched in", ";" + "=" * 85] if multi else []
        out += ["", f'.segment "{segment}"', f".org ${bank.base:04X}", ""]
        out += _render_bank(kb, bank)
    if rom.chr:
        out += ["", '.segment "CHR"', f'.incbin "{CHR_FILE}"', ""]

    memory = ["    HDR: start = $0000, size = $0010, file = %O, fill = yes;"]
    segments = ["    HEADER: load = HDR, type = ro;"]
    for bank in banks:
        area, seg = (f"PRG{bank.index}", f"BANK{bank.index}") if multi else ("PRG", "PRG")
        memory.append(f"    {area}: start = ${bank.base:04X}, size = ${len(bank.data):04X}, file = %O, fill = yes;")
        segments.append(f"    {seg + ':':<7} load = {area}, type = ro;")
    if rom.chr:
        memory.append(f"    CHR: start = $0000, size = ${len(rom.chr):04X}, file = %O, fill = yes;")
        segments.append("    CHR:    load = CHR, type = ro;")
    cfg = "\n".join(["MEMORY {", *memory, "}", "SEGMENTS {", *segments, "}", ""])
    return Rendered("\n".join(out), cfg, rom.chr)


def _render_bank(kb: KB, bank: Bank) -> list[str]:
    prg, base = bank.data, bank.base
    kinds = kb.classes(bank.index)
    rom_labels = {l.addr: l for l in kb.labels(bank.index)}
    comments = kb.comments(bank.index)
    summaries = {entry: summary for entry, summary, *_ in kb.routine_notes(bank.index)}
    sym = symbols_for(kb, bank.index)
    out: list[str] = []
    pc, end = base, base + len(prg)
    while pc < end:
        off = pc - base
        label = rom_labels.get(pc)
        notes = comments.get(pc, [])
        if label and label.kind in ("routine", "vector", "table"):
            out.append("")
            if pc in summaries:
                out.append(";" + "-" * 85)
                out += [f";{line}" for line in textwrap.wrap(summaries[pc], SUMMARY_WIDTH)]
        out += [f";{text}" for kind, text in notes if kind == "block"]
        if label:
            out.append(f"{label.name}:")
        inline = next((t for k, t in notes if k == "line"), None)
        kind = kinds.get(pc)

        if kind == "op":
            text, auto = sym.instruction(prg, base, pc)
            out.append(_with_comment(INDENT + text, inline or auto))
            pc += OPCODES[prg[off]].size
        elif kind == "word":
            value = prg[off] | (prg[off + 1] << 8)
            out.append(_with_comment(f"{INDENT}.addr {sym.word(value)}", inline))
            pc += 2
        else:
            run = [prg[off]]
            nxt = pc + 1
            while (nxt < end and len(run) < BYTES_PER_LINE and kinds.get(nxt) in (None, "data")
                   and nxt not in rom_labels and nxt not in comments):
                run.append(prg[nxt - base])
                nxt += 1
            out.append(_with_comment(f"{INDENT}.byte " + ",".join(f"${b:02X}" for b in run), inline))
            pc = nxt
    return out
