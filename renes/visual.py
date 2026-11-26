"""Pictures for the annotator: what a tile, a state or an object type looks like on screen.

The benchmark comparison showed the annotator got behaviour right but missed *what* things are
(scenery, projectiles, specific enemies), because it only ever saw numbers. This module makes
images Claude can open while annotating:

  tiles      every 8x8 tile of the cartridge's CHR ROM as a PNG (two pattern tables)
  dispatch   jump tables found by the analysis, with the RAM variable that selects the entry
  probe      for a dispatch (or any RAM variable): hold the variable at each value for a while,
             screenshot, and crop what changed against an untouched run

Images are stored in the KB (media kind "probe") and exported under out/<stem>/ so work units
can list their paths.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import png
from .analyze import PRG_BANK
from .annotate import is_ram
from .kb import KB, LIVE_STATES, NON_ROM
from .opcodes import IMM, OPCODES, operand_value

DEFAULT_SETUP = [[60, "none"], [4, "start"], [240, "none"]]   # press Start at the title, wait for play
GRAY = [(0, 0, 0), (96, 96, 96), (176, 176, 176), (255, 255, 255)]
TILE_ZOOM = 8
OAM_PAGE = 0x0200


def export_dir(kb: KB) -> Path | None:
    value = kb.setting("export_dir")
    return Path(value) if value else None


# ---------------------------------------------------------------------------------------------
# Tiles


def tile_image(chr_rom: bytes, table: int, tile: int) -> png.Image:
    base = table * 0x1000 + tile * 16
    lo, hi = chr_rom[base:base + 8], chr_rom[base + 8:base + 16]
    rows = []
    for y in range(8):
        rows.append(b"".join(bytes(GRAY[((lo[y] >> (7 - x)) & 1) | (((hi[y] >> (7 - x)) & 1) << 1)]) for x in range(8)))
    return 8, 8, rows


def export_tiles(kb: KB, out: Path) -> int:
    """tiles/p0/xx.png and tiles/p1/xx.png (each tile at 8x), plus a sheet per pattern table."""
    chr_rom = kb.rom().chr
    if len(chr_rom) < 0x2000:
        return 0
    n = 0
    for table in (0, 1):
        folder = out / "tiles" / f"p{table}"
        folder.mkdir(parents=True, exist_ok=True)
        cell = 8 * 4 + 2
        sheet = png.blank(16 * cell, 16 * cell, (40, 40, 60))
        sheet_rows = [bytearray(r) for r in sheet[2]]
        for tile in range(256):
            img = tile_image(chr_rom, table, tile)
            (folder / f"{tile:02X}.png").write_bytes(png.encode(png.scale(img, TILE_ZOOM)))
            big = png.scale(img, 4)
            x0, y0 = (tile % 16) * cell + 1, (tile // 16) * cell + 1
            for y, row in enumerate(big[2]):
                sheet_rows[y0 + y][x0 * 3:(x0 + 32) * 3] = row
            n += 1
        (out / "tiles" / f"sheet_p{table}.png").write_bytes(png.encode((sheet[0], sheet[1], [bytes(r) for r in sheet_rows])))
    return n


def tiles_written(ctx, entry: int) -> list[int]:
    """Immediate tile numbers the routine stores into the sprite buffer's tile bytes (OAM +1)."""
    last = ctx.routines[entry][0]
    out, imm, pc = [], None, entry
    while pc <= last:
        if ctx.kinds.get(pc) != "op":
            pc += 1
            continue
        op = OPCODES[ctx.prg[pc - ctx.base]]
        value = operand_value(op, ctx.prg, pc - ctx.base)
        if op.mnemonic == "lda":
            imm = value if op.mode == IMM else None
        elif op.mnemonic == "sta" and imm is not None and value is not None and op.mode in ("abs", "abx", "aby"):
            if OAM_PAGE <= value < OAM_PAGE + 0x100 and (value - OAM_PAGE) % 4 == 1 and imm not in out:
                out.append(imm)
        elif op.mnemonic in ("jsr", "rts", "jmp", "pla", "txa", "tya", "adc", "sbc", "and", "ora", "eor"):
            imm = None
        pc += op.size
    return out


# ---------------------------------------------------------------------------------------------
# Dispatch tables


@dataclass
class Dispatch:
    site: int                  # the JSR to the jump engine
    owner: int                 # routine containing it
    handlers: list[int]        # table entries in order (index 0, 1, ...)
    index_addr: int | None     # RAM variable that selects the entry
    indexed: str | None        # "x"/"y" when the variable is an array indexed by that register
    adjust: int                # value of the variable = index + adjust
    source: str                # how the index is computed, for the listing

    def value(self, i: int) -> int:
        return (i + self.adjust) & 0xFF


def _decode(ctx, start: int, end: int) -> list[tuple[int, object, int | None]]:
    out, pc = [], start
    while pc < end:
        if ctx.kinds.get(pc) != "op":
            pc += 1
            continue
        op = OPCODES[ctx.prg[pc - ctx.base]]
        out.append((pc, op, operand_value(op, ctx.prg, pc - ctx.base)))
        pc += op.size
    return out


def _engine_prefix(ctx, callee: int) -> list[tuple[int, object, int | None]]:
    """The instructions a jump-engine callee runs before the engine proper (its first asl or pla)."""
    out, pc = [], callee
    while len(out) < 8 and ctx.base <= pc < ctx.base + len(ctx.prg):
        op = OPCODES.get(ctx.prg[pc - ctx.base])
        if op is None or op.mnemonic in ("asl", "pla"):
            return out
        out.append((pc, op, operand_value(op, ctx.prg, pc - ctx.base)))
        pc += op.size
    return []


A_NEUTRAL = {"sec", "clc", "sei", "cli", "cld", "clv", "nop", "cmp", "cpx", "cpy", "bit", "sta", "stx", "sty",
             "ldx", "ldy", "inx", "iny", "dex", "dey", "tax", "tay", "inc", "dec",
             "bcc", "bcs", "beq", "bne", "bmi", "bpl", "bvc", "bvs"}
ARRAY_MODES = {"zpx": "x", "abx": "x", "zpy": "y", "aby": "y"}


def dispatches(ctx) -> list[Dispatch]:
    """Every JSR into a jump engine, with its table entries and (when simple) its index variable.

    Found from the code itself rather than the analysis's table edges: a table whose bytes were
    already classified as data during play gets no edges there.
    """
    from .analyze import is_jump_engine

    engines = {}
    for _, callee, site, kind in ctx.kb.calls(ctx.bank):
        if kind == "jsr" and callee not in engines:
            engines[callee] = ctx.base <= callee < ctx.base + len(ctx.prg) and is_jump_engine(ctx.prg, ctx.base, callee)
    sites = {site: callee for _, callee, site, kind in ctx.kb.calls(ctx.bank) if kind == "jsr" and engines.get(callee)}
    out = []
    for site, callee in sorted(sites.items()):
        handlers, p = [], site + 3
        while len(handlers) < 64 and 0 <= p - ctx.base < len(ctx.prg) - 1 and ctx.kinds.get(p) != "op":
            word = ctx.prg[p - ctx.base] | ctx.prg[p - ctx.base + 1] << 8
            if ctx.kinds.get(word) != "op":
                break
            handlers.append(word)
            p += 2
        owner = max((e for e, (last, _) in ctx.routines.items() if e <= site <= last), default=site)
        # a helper that loads the index and falls into the engine (lda $55 / asl a / pla ...) runs
        # after the code at the call site, so its loads decide the index (feedback #38)
        code = _decode(ctx, owner, site) + _engine_prefix(ctx, callee)
        index_addr = indexed = None
        adjust, source = 0, "unknown"
        i = len(code) - 1
        while i >= 0:
            _, op, value = code[i]
            m = op.mnemonic
            if m in A_NEUTRAL:
                i -= 1
                continue
            if m == "sbc" and op.mode == IMM:
                adjust += value
            elif m == "adc" and op.mode == IMM:
                adjust -= value
            elif m in ("tya", "txa"):
                reg = "y" if m == "tya" else "x"
                j = next((k for k in range(i - 1, -1, -1) if code[k][1].mnemonic == "ld" + reg), None)
                if j is None or code[j][1].mode == IMM or not is_ram(code[j][2] or 0x8000):
                    source = f"computed ({m})"
                    break
                index_addr, indexed = code[j][2], ARRAY_MODES.get(code[j][1].mode)
                source = f"ld{reg} ${index_addr:04X}" + (f",{indexed}" if indexed else "")
                break
            elif m == "lda" and op.mode != IMM and value is not None and is_ram(value):
                index_addr, indexed = value, ARRAY_MODES.get(op.mode)
                source = f"lda ${value:04X}" + (f",{indexed}" if indexed else "")
                break
            else:
                source = f"computed ({m})"
                break
            i -= 1
        if adjust and index_addr is not None:
            source += f" minus ${adjust & 0xFF:02X}"
        out.append(Dispatch(site, owner, handlers, index_addr, indexed, adjust, source))
    return out


# ---------------------------------------------------------------------------------------------
# Probes: hold a variable at each value, screenshot, crop the change


def _crop_change(control: png.Image, shot: png.Image) -> png.Image | None:
    box = png.diff_box(control, shot)
    if box is None:
        return None
    w, h, _ = control
    x0, y0, x1, y1 = box
    if (x1 - x0) * (y1 - y0) > w * h * 0.6:
        return None          # most of the screen changed: the full screenshot says it all
    m = 6
    x0, y0, x1, y1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)
    img = png.crop(shot, x0, y0, x1, y1)
    return png.scale(img, 3) if max(img[0], img[1]) < 80 else img


def probe(kb_path: Path, addr: int, values: list[int], out: Path, site: int = 0,
          state: int | None = None, frames: int = 40, log=print) -> list[dict]:
    """For each value: from the same start, hold `addr` at the value for `frames` frames and
    screenshot. Stores media (kind "probe", ref = dispatch site or 0) and exports PNGs."""
    from .experiment import Lab

    with KB(kb_path) as kb:
        name = probe_folder(kb, addr, site)
    folder = out / "probes" / name
    folder.mkdir(parents=True, exist_ok=True)
    spec = {"state": state} if state is not None else {"setup": DEFAULT_SETUP}
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    with KB(kb_path) as kb:
        kb.set_setting("export_dir", str(out))
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        try:
            lab.reach(spec)
            control_png = lab.run([(frames, "none")], shots=0).screen
            control = png.decode(control_png)
            (folder / "control.png").write_bytes(control_png)
            # a jump table's earlier pictures go too, even when they held another (wrong) index variable
            kb.db.execute("DELETE FROM media WHERE kind='probe' AND ref=? AND (addr=? OR ?)", (site, addr, site != 0))
            kb.add_media("probe", NON_ROM, addr, site, "control", -1, control_png, created)
            for v in values:
                shot_png = lab.run([(frames, "none")], freeze={addr: v}, shots=0).screen
                shot = png.decode(shot_png)
                cropped = _crop_change(control, shot)
                (folder / f"{v:02X}.png").write_bytes(shot_png)
                kb.add_media("probe", NON_ROM, addr, site, "full", v, shot_png, created)
                if cropped is not None:
                    crop_png = png.encode(cropped)
                    (folder / f"{v:02X}_crop.png").write_bytes(crop_png)
                    kb.add_media("probe", NON_ROM, addr, site, "crop", v, crop_png, created)
                same = shot_png == control_png
                results.append({"value": v, "same_as_control": same, "crop": cropped is not None})
                log(f"  {name} = ${v:02X}: " + ("no visible change" if same else
                                                 "changed area cropped" if cropped is not None else "screen changed"))
            kb.commit()
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)
    return results


def find_slot(kb_path: Path, d: Dispatch, state: int | None = None, frames: int = 60,
              tries: int = 16) -> tuple[int | None, list[tuple[int, int]]]:
    """For an indexed dispatch (object types), a start state where the table runs and the X/Y
    slots its JSR runs with there, most frequent first: (state, [(slot, calls)]).

    Tries the default start (press Start, wait), then up to `tries` explorer-archive states spread
    over the game, unless `state` is given. ([], None) when the table never runs."""
    from collections import Counter
    from .experiment import Lab

    with KB(kb_path) as kb:
        if state is not None:
            candidates = [state]
        else:
            ids = [i for (i,) in kb.db.execute(f"SELECT id FROM states WHERE {LIVE_STATES} ORDER BY frame")]
            step = max(1, len(ids) // tries)
            candidates = [None] + ids[step // 2::step][:tries]
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        try:
            for cand in candidates:
                lab.reach({"state": cand} if cand is not None else {"setup": DEFAULT_SETUP})
                lab.m.trace(d.site, d.site, max_lines=2000)   # only the JSR itself: one row per call
                lab.m.step(frames)
                _, rows = lab.m.trace_log()
                calls = Counter(r[d.indexed] for r in rows if d.indexed)
                if calls:
                    return cand, sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)
    return None, []


def probed_sites(kb: KB) -> set[int]:
    return {ref for (ref,) in kb.db.execute("SELECT DISTINCT ref FROM media WHERE kind='probe'")}


def skipped_sites(kb: KB) -> dict[int, str]:
    """Dispatch sites the loop gave up probing (site -> reason), from the probe_skipped setting."""
    import json
    return {int(k): v for k, v in json.loads(kb.setting("probe_skipped") or "{}").items()}


def skip_site(kb: KB, site: int, reason: str) -> None:
    import json
    skipped = skipped_sites(kb)
    skipped[site] = reason
    kb.set_setting("probe_skipped", json.dumps({str(k): v for k, v in skipped.items()}))
    kb.commit()


def probe_images(kb: KB, addr: int, site: int | None = None) -> dict[int, dict[str, str]]:
    """value -> {"full": path, "crop": path} for an earlier probe of addr (exported files)."""
    out = export_dir(kb)
    if out is None:
        return {}
    query = "SELECT DISTINCT ref, frame, role FROM media WHERE kind='probe' AND addr=?" + (" AND ref=?" if site is not None else "")
    rows = kb.db.execute(query, (addr, site) if site is not None else (addr,)).fetchall()
    images: dict[int, dict[str, str]] = {}
    for ref, value, role in rows:
        if role == "control":
            continue
        folder = out / "probes" / probe_folder(kb, addr, ref)
        path = folder / (f"{value:02X}.png" if role == "full" else f"{value:02X}_crop.png")
        if path.exists():
            images.setdefault(value, {})[role] = str(path)
    return images


def probe_folder(kb: KB, addr: int, site: int) -> str:
    label = kb.label_at(NON_ROM, addr)
    base = label.name if label else f"ram_{addr:04X}"
    return f"{base}_dispatch_{site:04X}" if site else base
