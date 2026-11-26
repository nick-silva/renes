"""When is a project finished, and what does it hand over?

A project is finished when every check below passes. The loop drives the last steps itself
(experiment, evidence and audit stages after annotation completes, then `done`), so an agent can
finish a game cleanly and move to the next ROM.

  annotated   every routine annotated, none sent back
  pictures    tile sheets and metatiles exported, every jump table probed or skipped
  evidence    every annotated routine that runs has a trace and knock-out
  experiments an experiment round ran after the last annotation, or fewer than MIN_UNTESTED
              RAM candidates are left untested
  audit       an audit with fewer than AUDIT_STALE routines (or AUDIT_STALE_SHARE of them)
              re-annotated since, AUDIT_MIN of its sample supported by the code, and nothing it
              sent back still waiting
  build       the last build reassembled byte-identically

`export` writes out/<stem>/export/: the annotated source (.asm/.cfg, rebuilds the ROM with ca65),
README.md (the game, the numbers, the subsystems, how to build), routines.md (every routine with its
summary, by subsystem) and ram.md (the RAM map), plus the tile and metatile sheets. It contains the
game's code and data: keep it local, like the ROM.
"""
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import audit as audit_mod, grade as grade_mod, maps
from .analyze import PRG_BANK
from .kb import KB, NON_ROM

AUDIT_MIN = 0.7
AUDIT_STALE, AUDIT_STALE_SHARE = 10, 0.05   # re-audit once this many routines (or this share) changed
MIN_UNTESTED = 5


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    todo: str = ""           # the stage or action that fixes it


def checklist(kb: KB, s=None) -> list[Check]:
    from . import cycle

    s = s or cycle.status(kb)
    one = lambda sql, *a: kb.db.execute(sql, a).fetchone()[0]
    rework = one("SELECT count(*) FROM routine_notes WHERE status='rework'")
    last_note = one("SELECT max(updated) FROM routine_notes") or ""
    last_exp = one("SELECT max(created) FROM experiments") or ""
    a = audit_mod.latest(kb)
    build = json.loads(kb.setting("last_build") or "{}")
    out = [
        Check("annotated", s.annotated >= s.routines and not rework,
              f"{s.annotated}/{s.routines} routines" + (f", {rework} sent back" if rework else ""), "annotate"),
        Check("pictures", not s.tiles_missing and not s.unprobed,
              "tiles, metatiles and jump tables done" if not s.tiles_missing and not s.unprobed else
              ("tiles or metatiles missing; " if s.tiles_missing else "") + (f"{len(s.unprobed)} jump tables unprobed"
                                                                            if s.unprobed else ""), "probe"),
        Check("evidence", s.without_evidence == 0, f"{s.without_evidence} annotated routines without traces/knock-outs",
              "evidence"),
        Check("experiments", len(s.untested) < MIN_UNTESTED or (last_exp and last_exp > last_note),
              f"{len(s.untested)} untested RAM candidates; last experiment "
              + ("after" if last_exp and last_exp > last_note else "before") + " the last annotation", "experiment"),
    ]
    if a is None:
        out.append(Check("audit", False, "never audited", "audit"))
    else:
        # a few revised summaries don't make the sample unrepresentative: re-audit after real change
        changed = one("SELECT count(*) FROM routine_notes WHERE updated > ?", a["created"])
        # names the audit sent back and that were reworked since need re-checking (audit --repeat),
        # however few they are; otherwise a few revised summaries don't make the sample stale
        sent_back = a.get("sent_back", a["all"].get("unsupported", 0))
        recheck = bool(sent_back) and changed > 0
        fresh = not recheck and changed < max(AUDIT_STALE, s.routines * AUDIT_STALE_SHARE)
        rate = a["all"]["supported_rate"] or 0
        out.append(Check("audit", fresh and rate >= AUDIT_MIN and not rework,
                         f"{rate:.0%} supported ({a['audited']} items, {a['created'][:10]})"
                         + (f"; {changed} routines re-annotated since" if changed else "")
                         + ("; re-check the sent-back names: audit prepare --repeat" if recheck and not rework else ""),
                         # sent-back names first; then a fresh audit; a fresh low score needs a person
                         "annotate" if rework else "audit" if not fresh else "review"))
    out.append(Check("build", bool(build.get("ok")), build.get("message", "never built"), "review"))
    return out


def finished(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def format_checklist(checks: list[Check]) -> str:
    return "\n".join(f"  [{'x' if c.ok else ' '}] {c.name:<12} {c.detail}" + ("" if c.ok else f"  -> {c.todo}")
                     for c in checks) + "\n"


def export(kb_path: Path, out_root: Path) -> Path:
    """Write the hand-over package to out/<stem>/export/ (refused unless the source rebuilds the ROM)."""
    from .render import render
    from .verify import build_and_verify

    stem = kb_path.stem
    target = out_root / stem / "export"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with KB(kb_path) as kb:
        rom = kb.rom()
        result = build_and_verify(render(kb), rom.raw, target, stem)
        if not result.identical:
            raise RuntimeError(f"the annotated source no longer rebuilds the ROM (first difference ${result.first_diff:X})")
        for leftover in (target / f"{stem}.nes", target / f"{stem}.o"):
            leftover.unlink(missing_ok=True)       # the source is the deliverable, not a ROM copy
        checks = checklist(kb)
        # keyed by map position (maps.linear), like the subsystem members: unique across banks
        labels = {maps.linear(rom, b.index, l.addr): l for b in rom.banks() for l in kb.labels(b.index)}
        notes = {maps.linear(rom, b.index, e): s for b in rom.banks() for e, s, *_ in kb.routine_notes(b.index)}
        where = {maps.linear(rom, b.index, e): (b.index, e) for b in rom.banks() for e, *_ in kb.routines(b.index)}
        multi = len(rom.banks()) > 1
        ram = sorted((l for l in kb.labels(NON_ROM) if l.kind == "ram"), key=lambda l: l.addr)
        ram_comments = kb.comments(NON_ROM)
        groups = maps.subsystems(kb)["clusters"]
        a, g = audit_mod.latest(kb), grade_mod.latest(kb)
        verified = sum(1 for l in ram if l.status in ("verified", "approved"))
        cdl = kb.coverage("nesPrgRom", "cdl") or b""
    images = target / "images"
    images.mkdir()
    for sheet in list((out_root / stem / "tiles").glob("sheet_*.png")) + list((out_root / stem / "metatiles").glob("sheet_*.png")):
        shutil.copy(sheet, images / sheet.name)

    def rname(e: int) -> str:
        return labels[e].name if e in labels else addr_text(e)

    def addr_text(e: int) -> str:
        bank, addr = where.get(e, (0, e))
        return f"B{bank}:${addr:04X}" if multi else f"${addr:04X}"

    readme = [f"# {Path(rom.name).stem}: annotated disassembly", "",
              f"Produced with renes. `{stem}.asm` reassembles byte-identically to the original ROM "
              f"(SHA-1 `{rom.sha1}`); you need that ROM's CHR data (`chr.bin` is extracted from it).", "",
              "## Build", "", "```", f"ca65 {stem}.asm -o {stem}.o", f"ld65 -C {stem}.cfg {stem}.o -o {stem}.nes", "```", "",
              "## Numbers", "",
              f"- {len(notes)} routines annotated, {len(ram)} RAM labels ({verified} verified by experiment or approved)",
              f"- {sum(1 for f in cdl if f & 1) * 100 // max(1, len(rom.prg))}% of the program ROM seen executing",
              f"- audit: {a['all']['supported_rate']:.0%} of {a['audited']} sampled names supported by the code"
              if a and a["all"]["supported_rate"] is not None else "- audit: not run",
              f"- against a reference disassembly: {g['all']['same_rate']:.0%} same meaning ({g['graded']} graded)"
              if g and g["all"]["same_rate"] is not None else "- no reference grading",
              "", "## Finished?", "", "```", format_checklist(checks).rstrip(), "```", "",
              "## Subsystems", "", "Routines grouped by the call graph (details in `routines.md`):", ""]
    readme += [f"- **{c['name']}**: {c['routines']} routines, {c['bytes']} bytes" for c in groups if c["routines"] >= 3]
    readme += ["", "Names marked `;?` in the source are unreviewed candidates; `ram.md` gives each RAM label's status.",
               "Images: `images/` (tile and metatile sheets)."]
    (target / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

    lines = ["# Routines", ""]
    for c in groups:
        lines += [f"## {c['name']}", ""]
        lines += [f"- `{addr_text(e)}` **{rname(e)}**: {notes.get(e, '(not annotated)')}" for e in c["members"]]
        lines.append("")
    (target / "routines.md").write_text("\n".join(lines), encoding="utf-8")

    rows = ["# RAM map", "", "| address | size | name | status | comment |", "|---|---|---|---|---|"]
    for l in ram:
        comment = next((t for k, t in ram_comments.get(l.addr, []) if k == "line"), "")
        rows.append(f"| `${l.addr:04X}` | {l.size or 1} | {l.name} | {l.status} | {comment} |")
    (target / "ram.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return target
