"""Game Genie codes (M7): research input for open games, a test set for benchmarked ones.

A Game Genie code is a ROM address, a replacement byte and (8-letter codes) a compare byte,
plus a human description ("infinite lives"). Decoded, it points at the exact instruction behind
a well-known behaviour, which makes it either:

  research  shown to the annotator as a hypothesis (only for games we don't benchmark), or
  test      never shown to the annotator: after annotation, each code's address is looked up in
            our knowledge base and its description compared with our names and summaries.
            Only aggregates are printed; per-code details go to a file for the human.

Codes are applied in Mesen by patching PRG-ROM (equivalent on NROM, where the ROM never
switches; the compare byte must match). Bank-switched games will need per-bank handling.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .kb import KB, NON_ROM

LETTERS = "APZLGITYEOXUKSVN"
CODE_RE = re.compile(rf"^[{LETTERS}]{{6}}([{LETTERS}]{{2}})?$")
USES = ("research", "test")


class CodeError(ValueError):
    pass


@dataclass(frozen=True)
class Genie:
    code: str
    address: int
    value: int
    compare: int | None


def decode(code: str) -> Genie:
    code = code.strip().upper()
    if not CODE_RE.match(code):
        raise CodeError(f"{code!r} is not a 6- or 8-letter Game Genie code")
    n = [LETTERS.index(c) for c in code]
    address = 0x8000 | ((n[3] & 7) << 12) | ((n[5] & 7) << 8) | ((n[4] & 8) << 8) \
        | ((n[2] & 7) << 4) | ((n[1] & 8) << 4) | (n[4] & 7) | (n[3] & 8)
    if len(n) == 6:
        value = ((n[1] & 7) << 4) | ((n[0] & 8) << 4) | (n[0] & 7) | (n[5] & 8)
        return Genie(code, address, value, None)
    value = ((n[1] & 7) << 4) | ((n[0] & 8) << 4) | (n[0] & 7) | (n[7] & 8)
    compare = ((n[7] & 7) << 4) | ((n[6] & 8) << 4) | (n[6] & 7) | (n[5] & 8)
    return Genie(code, address, value, compare)


def encode(address: int, value: int, compare: int | None = None) -> str:
    """Inverse of decode. Bit 3 of the third letter carries no address/value bits; published codes
    set it inconsistently and decode() ignores it. Here it is set for 8-letter codes."""
    a, v = address & 0x7FFF, value & 0xFF
    n = [0] * (6 if compare is None else 8)
    n[0] = (v & 7) | ((v >> 4) & 8)
    n[1] = ((v >> 4) & 7) | ((a >> 4) & 8)
    n[2] = ((a >> 4) & 7) | (8 if compare is not None else 0)
    n[3] = ((a >> 12) & 7) | (a & 8)
    n[4] = (a & 7) | ((a >> 8) & 8)
    if compare is None:
        n[5] = ((a >> 8) & 7) | (v & 8)
    else:
        c = compare & 0xFF
        n[5] = ((a >> 8) & 7) | (c & 8)
        n[6] = (c & 7) | ((c >> 4) & 8)
        n[7] = ((c >> 4) & 7) | (v & 8)
    return "".join(LETTERS[x] for x in n)


def rom_byte(kb_rom, address: int) -> int:
    return kb_rom.prg[(address - 0x8000) % len(kb_rom.prg)]


def applies(kb_rom, g: Genie) -> bool:
    """On NROM a code takes effect when it has no compare byte or the ROM byte matches it."""
    return g.compare is None or rom_byte(kb_rom, g.address) == g.compare


def add_codes(kb: KB, lines: list[str], source: str, use: str) -> tuple[int, list[str]]:
    """lines: 'CODE description'. Returns (added, problems)."""
    if use not in USES:
        raise CodeError(f"use must be one of {USES}")
    added, problems = 0, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        code, _, desc = line.partition(" ")
        codes = [c for c in re.split(r"[+,]", code) if c]
        try:
            for c in codes:
                decode(c)
        except CodeError as e:
            problems.append(str(e))
            continue
        kb.db.execute("INSERT OR REPLACE INTO codes (code, description, source, use, created) VALUES (?,?,?,?,?)",
                      ("+".join(c.upper() for c in codes), desc.strip() or "(no description)", source, use, now))
        added += 1
    kb.commit()
    return added, problems


def locate(kb: KB, address: int) -> dict:
    """Our view of the patched address: owning routine, its name/summary, labels used nearby."""
    owner = kb.db.execute("SELECT entry, last FROM routines WHERE bank=0 AND entry<=? AND last>=? ORDER BY entry DESC LIMIT 1",
                          (address, address)).fetchone()
    names = {l.addr: l for l in kb.labels(0)}
    if not owner:
        return {"routine": None}
    entry = owner[0]
    note = kb.db.execute("SELECT summary FROM routine_notes WHERE bank=0 AND entry=?", (entry,)).fetchone()
    comment = kb.db.execute("SELECT text FROM comments WHERE bank=0 AND addr=? AND kind='line'", (address,)).fetchone()
    # RAM operands of the instructions around the patch
    from .opcodes import OPCODES, ZEROPAGE_MODES, ABSOLUTE_MODES, operand_value
    rom = kb.rom()
    ram_names = {l.addr: l.name for l in kb.labels(NON_ROM)}
    kinds = kb.classes(0)
    near = []
    for pc in range(max(entry, address - 8), min(owner[1], address + 8) + 1):
        if kinds.get(pc) != "op":
            continue
        op = OPCODES.get(rom.prg[pc - rom.prg_base])
        if op and op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES:
            v = operand_value(op, rom.prg, pc - rom.prg_base)
            if v in ram_names:
                near.append(ram_names[v])
    label = names.get(entry)
    return {"routine": entry, "name": label.name if label else None,
            "annotated": bool(note) and label is not None and label.source == "claude",
            "summary": note[0] if note else "", "line_comment": comment[0] if comment else "", "near_ram": near}


def evaluate(kb: KB, details_path: Path | None = None) -> dict:
    """Aggregate agreement between test codes' descriptions and our annotations."""
    from .benchmark import tokens
    rom = kb.rom()
    rows = []
    for cid, code, desc, use in kb.db.execute("SELECT id, code, description, use FROM codes WHERE use='test'"):
        g = decode(code.split("+")[0])
        where = locate(kb, g.address)
        ours = " ".join(filter(None, [where.get("name") or "", where.get("summary", ""), where.get("line_comment", ""),
                                      " ".join(where.get("near_ram", []))]))
        d, o = tokens(desc), tokens(ours)
        overlap = len(d & o) / len(d) if d else 0.0
        rows.append({"code": code, "description": desc, "applies": applies(rom, g), "address": g.address,
                     "routine": where.get("routine"), "annotated": where.get("annotated", False),
                     "ours": where.get("name"), "overlap": round(overlap, 2)})
    applicable = [r for r in rows if r["applies"]]
    annotated = [r for r in applicable if r["annotated"]]
    result = {"codes": len(rows), "apply_to_this_rom": len(applicable), "in_code_we_know": sum(1 for r in applicable if r["routine"]),
              "in_annotated_routines": len(annotated),
              "description_overlap_mean": round(sum(r["overlap"] for r in annotated) / len(annotated), 3) if annotated else None,
              "agreeing (overlap >= 0.5)": sum(1 for r in annotated if r["overlap"] >= 0.5)}
    if details_path:
        details_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Game Genie test codes (for the human reviewer only: do not show to the annotator)", "",
                 "| code | description | applies | address | our routine | overlap |", "|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['code']} | {r['description']} | {'yes' if r['applies'] else 'no'} | ${r['address']:04X} | "
                         f"{r['ours'] or '-'} | {r['overlap']} |")
        details_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def research_listing(kb: KB) -> str:
    """Research codes with their location in our annotation: input for the annotator."""
    out = []
    rom = kb.rom()
    for code, desc, source in kb.db.execute("SELECT code, description, source FROM codes WHERE use='research' ORDER BY id"):
        for part in code.split("+"):
            g = decode(part)
            where = locate(kb, g.address)
            routine = (f"in {where['name'] or hex(where['routine'])}" if where.get("routine") else "outside known routines")
            out.append(f"{part}  \"{desc}\": ${g.address:04X} := ${g.value:02X}"
                       + (f" if ${g.compare:02X}" if g.compare is not None else "")
                       + f" (ROM has ${rom_byte(rom, g.address):02X}; {'applies' if applies(rom, g) else 'does NOT apply'}) {routine}")
    return "\n".join(out) or "(no research codes)"


def try_code(kb_path: Path, code: str, frames: int = 600, log=print) -> dict:
    """Apply a code in Mesen and compare a scripted run with an untouched control run."""
    from .emu.mesen import Mesen
    from .evidence import SCRIPTED

    parts = [decode(c) for c in code.split("+")]
    with KB(kb_path) as kb:
        rom = kb.rom()
        cid = kb.db.execute("SELECT id FROM codes WHERE code=?", (code.upper(),)).fetchone()
        labels = {l.addr: l.name for l in kb.labels(NON_ROM) if l.addr < 0x800}
    bad = [g.code for g in parts if not applies(rom, g)]
    if bad:
        return {"ok": False, "message": f"{', '.join(bad)}: compare byte does not match this ROM"}
    script = [b for n, b in SCRIPTED for _ in range(n)][:frames]
    rom_file = kb_path.with_suffix(".nes")
    rom_file.write_bytes(rom.raw)
    results = {}
    try:
        with Mesen(rom_file) as m:
            m.step(1)
            boot = m.save_bytes()
            for role in ("control", "treatment"):
                m.load_bytes(boot)
                if role == "treatment":
                    for g in parts:
                        m.write_prg(g.address, bytes([g.value]), len(rom.prg))
                shots = []
                for i, b in enumerate(script):
                    m.step(1, b)
                    if (i + 1) % (len(script) // 3 or 1) == 0:
                        shots.append((i + 1, m.screenshot_bytes()))
                results[role] = (m.ram(), shots)
                if role == "treatment":
                    for g in parts:
                        m.write_prg(g.address, bytes([rom_byte(rom, g.address)]), len(rom.prg))
    finally:
        rom_file.unlink(missing_ok=True)
    differs = [{"name": labels[a], "control": results["control"][0][a], "treatment": results["treatment"][0][a]}
               for a in sorted(labels) if results["control"][0][a] != results["treatment"][0][a]]
    if cid:
        with KB(kb_path) as kb:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            kb.db.execute("DELETE FROM media WHERE kind='code' AND ref=?", (cid[0],))
            for role in ("control", "treatment"):
                for frame, png in results[role][1]:
                    kb.add_media("code", 0, parts[0].address, cid[0], role, frame, png, now)
            kb.commit()
    return {"ok": True, "message": f"{len(differs)} labeled RAM bytes differ", "differs": differs}
