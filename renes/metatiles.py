"""Metatile pictures (backlog item 9): what each entry of a game's 16x16 block tables looks like.

Many NES games build their background from 2x2-tile blocks ("metatiles") defined in ROM tables;
level data then stores block numbers. Knowing that block $C2 is a question block makes the code
that tests for it readable. Instead of knowing any game's format, this module:

  1. observes: loads explorer-archive states and reads the PPU nametables, collecting every
     16x16 block on screen (its four tiles, attribute palette and the palette RAM)
  2. finds tables: scans ROM bytes outside code for runs of 4-byte entries (or four parallel
     tables) whose entries reproduce observed blocks, in row or column tile order
  3. renders every entry of each table as a picture, coloured with a palette it was seen with

A game that doesn't use such tables (some draw their levels from other units) gets none;
its pictures are the tile sheets. Results: out/<stem>/metatiles/<addr>/<NN>.png plus a sheet per
table, and the `metatiles` setting (JSON list of tables) for work units.
"""
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from . import png
from .analyze import PRG_BANK
from .kb import KB, LIVE_STATES

# 2C02 palette (NESdev): RGB for each of the 64 colour indices.
NES_RGB = [int(x, 16) for x in """
7C7C7C 0000FC 0000BC 4428BC 940084 A80020 A81000 881400 503000 007800 006800 005800 004058 000000 000000 000000
BCBCBC 0078F8 0058F8 6844FC D800CC E40058 F83800 E45C10 AC7C00 00B800 00A800 00A844 008888 000000 000000 000000
F8F8F8 3CBCFC 6888FC 9878F8 F878F8 F85898 F87858 FCA044 F8B800 B8F818 58D854 58F898 00E8D8 787878 000000 000000
FCFCFC A4E4FC B8B8F8 D8B8F8 F8B8F8 F8A4C0 F0D0B0 FCE0A8 F8D878 D8F878 B8F8B8 B8F8D8 00FCFC F8D8F8 000000 000000
""".split()]
GRAY = (0x0F, 0x00, 0x10, 0x30)
MIN_HITS = 6          # observed entries a table needs
MAX_MISSES = 2        # consecutive unobserved entries tolerated inside a table
ZOOM = 4
STATES = 24


@dataclass
class Table:
    addr: int
    order: str            # "rows": TL TR BL BR, "cols": TL BL TR BR
    layout: str           # "interleaved" (4 bytes per entry) or "parallel" (four tables `stride` apart)
    stride: int
    count: int
    hits: int

    def entry(self, prg: bytes, base: int, i: int) -> tuple[int, int, int, int]:
        """The entry's tiles in row order (TL, TR, BL, BR)."""
        o = self.addr - base
        a, b, c, d = (prg[o + 4 * i:o + 4 * i + 4] if self.layout == "interleaved"
                      else [prg[o + k * self.stride + i] for k in range(4)])
        return (a, b, c, d) if self.order == "rows" else (a, c, b, d)

    def extent(self) -> range:
        return range(self.addr, self.addr + (4 * self.count if self.layout == "interleaved"
                                             else 3 * self.stride + self.count))


@dataclass
class Observed:
    blocks: Counter                     # (TL, TR, BL, BR) -> times seen
    looks: dict                         # block -> Counter of (palette 4 colour indices, bg pattern table)


def observe(kb_path: Path, states: int = STATES) -> Observed:
    """16x16 blocks on screen in archive states spread over the game (plus the default start)."""
    from .experiment import Lab
    from .visual import DEFAULT_SETUP

    blocks, looks = Counter(), defaultdict(Counter)
    with KB(kb_path) as kb:
        ids = [i for (i,) in kb.db.execute(f"SELECT id FROM states WHERE {LIVE_STATES} ORDER BY frame")]
        picks = [None] + ids[::max(1, len(ids) // states)][:states]
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        try:
            for sid in picks:
                lab.reach({"state": sid} if sid is not None else {"setup": DEFAULT_SETUP})
                nt = lab.m.read(0, 0x800, "nesNametableRam")
                pal = lab.m.read(0, 32, "nesPaletteRam")
                table = 1 if lab.m.state().get("ppu.control.backgroundPatternAddr") else 0
                for t in (0, 0x400):
                    for by in range(15):
                        for bx in range(16):
                            tl = t + by * 64 + bx * 2
                            key = (nt[tl], nt[tl + 1], nt[tl + 32], nt[tl + 33])
                            attr = nt[t + 0x3C0 + (by // 2) * 8 + bx // 2]
                            p = (attr >> (((by % 2) * 2 + bx % 2) * 2)) & 3
                            blocks[key] += 1
                            looks[key][((pal[0],) + tuple(pal[p * 4 + 1:p * 4 + 4]), table)] += 1
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)
    return Observed(blocks, dict(looks))


def find_tables(prg: bytes, base: int, classes: dict[int, str], seen: set) -> list[Table]:
    """ROM tables whose entries reproduce observed blocks (outside code bytes)."""
    code = {a - base for a, k in classes.items() if k in ("op", "operand")}
    out = []
    for order in ("rows", "cols"):
        keys = seen if order == "rows" else {(a, c, b, d) for a, b, c, d in seen}
        s = 0
        while s < len(prg) - 4:                                    # interleaved: 4 bytes per entry
            if s in code or tuple(prg[s:s + 4]) not in keys:
                s += 1
                continue
            e, hits, miss = s, 0, 0
            while e + 4 <= len(prg) and not code & {e, e + 1, e + 2, e + 3}:
                if tuple(prg[e:e + 4]) in keys:
                    hits, miss = hits + 1, 0
                else:
                    if miss == MAX_MISSES:
                        break
                    miss += 1
                e += 4
            n = (e - s) // 4 - miss               # trailing unobserved entries aren't part of it
            if hits >= MIN_HITS and len({tuple(prg[s + 4 * i:s + 4 * i + 4]) for i in range(n)}) >= 4:
                out.append(Table(base + s, order, "interleaved", 4, n, hits))
                s += 4 * n
            else:
                s += 1
        firsts = {k[0] for k in keys}
        for stride in range(8, 257):                               # parallel: four tables `stride` apart
            for s in range(0, len(prg) - 4 * stride + 1):
                if prg[s] not in firsts or s in code:
                    continue
                hits = sum(1 for i in range(stride) if (prg[s + i], prg[s + stride + i], prg[s + 2 * stride + i],
                                                        prg[s + 3 * stride + i]) in keys)
                if hits >= max(MIN_HITS, stride // 3):
                    out.append(Table(base + s, order, "parallel", stride, stride, hits))
    out.sort(key=lambda t: -t.hits)
    kept, used = [], set()
    for t in out:                                                  # drop overlapping weaker finds
        if not used & set(t.extent()):
            kept.append(t)
            used |= set(t.extent())
    return sorted(kept, key=lambda t: t.addr)


MAX_LEAD = 64        # bytes a pointer may sit before a found table (entries never seen on screen)


def refine(prg: bytes, base: int, classes: dict[int, str], found: list[Table]) -> list[Table]:
    """Use the game's own pointers to its block tables. The longest pointer group (.dw words, or
    split low/high byte tables) whose pointers all land, entry-aligned and in ascending order, in or
    just before found tables is the renderer's table list. Its pointers fix table starts (leading
    entries never seen on screen) and split tables found as one run."""
    interleaved = [t for t in found if t.layout == "interleaved"]
    if not interleaved:
        return found
    code = {a - base for a, k in classes.items() if k in ("op", "operand")}
    starts = {t.addr for t in interleaved}

    def owner(p: int) -> Table | None:
        return next((t for t in interleaved if t.addr - MAX_LEAD <= p < t.addr + 4 * t.count
                     and (p - t.addr) % 4 == 0), None)

    best: list[int] = []
    for a in range(len(prg) - 1):
        if a in code:
            continue
        groups = [[prg[a + 2 * i] | prg[a + 2 * i + 1] << 8 for i in range(n)] for n in (2, 3, 4, 6, 8)
                  if a + 2 * n <= len(prg)]
        groups += [[prg[a + i] | prg[a + k + i] << 8 for i in range(k)] for k in (2, 3, 4, 6, 8)
                   if a + 2 * k <= len(prg)]
        for ptrs in groups:
            if (all(owner(p) for p in ptrs) and all(x < y for x, y in zip(ptrs, ptrs[1:]))
                    and (len(ptrs), sum(p in starts for p in ptrs)) > (len(best), sum(p in starts for p in best))):
                best = ptrs
    if not best:
        return found
    out = [t for t in found if t.layout != "interleaved"]
    for t in interleaved:
        cuts = sorted(p for p in best if owner(p) is t)
        if not cuts:
            out.append(t)
            continue
        if cuts[0] > t.addr:                     # entries before the first pointer stay their own run
            out.append(Table(t.addr, t.order, t.layout, 4, (cuts[0] - t.addr) // 4, t.hits))
        end = t.addr + 4 * t.count
        for p, q in zip(cuts, cuts[1:] + [end]):
            out.append(Table(p, t.order, t.layout, 4, (q - p) // 4, t.hits if p == cuts[0] else 0))
    out.sort(key=lambda t: t.addr)
    # A table the renderer points at runs up to the next one it points at (unseen entries included).
    for i, t in enumerate(out[:-1]):
        nxt = out[i + 1].addr
        end = t.addr + 4 * t.count
        if t.addr in best and nxt in best and end < nxt and not code & set(range(end - base, nxt - base)):
            out[i] = Table(t.addr, t.order, t.layout, 4, (nxt - t.addr) // 4, t.hits)
    return out


def _tile(chr_rom: bytes, table: int, tile: int, colours: tuple) -> list[bytes]:
    base = table * 0x1000 + tile * 16
    lo, hi = chr_rom[base:base + 8], chr_rom[base + 8:base + 16]
    rgb = [NES_RGB[c & 0x3F] for c in colours]
    rows = []
    for y in range(8):
        row = bytearray()
        for x in range(8):
            v = rgb[((lo[y] >> (7 - x)) & 1) | (((hi[y] >> (7 - x)) & 1) << 1)]
            row += bytes((v >> 16, (v >> 8) & 0xFF, v & 0xFF))
        rows.append(bytes(row))
    return rows


def block_image(chr_rom: bytes, tiles: tuple, colours: tuple, table: int) -> png.Image:
    tl, tr, bl, br = (_tile(chr_rom, table, t, colours) for t in tiles)
    return 16, 16, [a + b for a, b in zip(tl, tr)] + [a + b for a, b in zip(bl, br)]


def render(kb: KB, tables: list[Table], obs: Observed, out: Path) -> int:
    rom = kb.rom()
    if len(rom.chr) < 0x2000:
        return 0
    n = 0
    for t in tables:
        folder = out / "metatiles" / f"{t.addr:04X}"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [t.entry(rom.prg, rom.prg_base, i) for i in range(t.count)]
        common = Counter()
        for e in entries:
            common.update(obs.looks.get(e, {}))
        fallback = common.most_common(1)[0][0] if common else (GRAY, 0)
        cell = 16 * ZOOM + 2
        sheet = [bytearray(png.blank(16 * cell, ((len(entries) + 15) // 16) * cell, (40, 40, 60))[2][y])
                 for y in range(((len(entries) + 15) // 16) * cell)]
        for i, e in enumerate(entries):
            colours, table = obs.looks[e].most_common(1)[0][0] if e in obs.looks else fallback
            img = png.scale(block_image(rom.chr, e, colours, table), ZOOM)
            (folder / f"{i:02X}.png").write_bytes(png.encode(img))
            x0, y0 = (i % 16) * cell + 1, (i // 16) * cell + 1
            for y, row in enumerate(img[2]):
                sheet[y0 + y][x0 * 3:(x0 + 16 * ZOOM) * 3] = row
            n += 1
        (out / "metatiles" / f"sheet_{t.addr:04X}.png").write_bytes(
            png.encode((16 * cell, len(sheet), [bytes(r) for r in sheet])))
    return n


def export(kb_path: Path, out: Path) -> list[Table]:
    """Observe, find, render; remember the tables in the `metatiles` setting."""
    obs = observe(kb_path)
    with KB(kb_path) as kb:
        rom = kb.rom()
        classes = kb.classes(PRG_BANK)
        tables = refine(rom.prg, rom.prg_base, classes, find_tables(rom.prg, rom.prg_base, classes, set(obs.blocks)))
        render(kb, tables, obs, out)
        kb.set_setting("metatiles", json.dumps([asdict(t) for t in tables]))
        kb.set_setting("export_dir", str(out))
        kb.commit()
    return tables


def tables(kb: KB) -> list[Table]:
    return [Table(**t) for t in json.loads(kb.setting("metatiles") or "[]")]


def reached(prg: bytes, base: int, refs: set[int], found: list[Table]) -> list[Table]:
    """Tables a routine reaches through pointers in the data it references: .dw words, or split
    low/high byte tables (pointer i = lo[i] | hi[i] << 8). At least two pointers must land exactly
    on table starts, so a coincidental byte pair doesn't count."""
    starts = {t.addr: t for t in found}
    hits: dict[int, int] = Counter()
    refs = sorted(a for a in refs if 0 <= a - base < len(prg) - 1)
    for a in refs:
        ptrs = [prg[a - base + 2 * i] | prg[a - base + 2 * i + 1] << 8 for i in range(8) if a - base + 2 * i + 1 < len(prg)]
        if sum(p in starts for p in ptrs) >= 2:
            hits.update(p for p in ptrs if p in starts)
        for b in refs:
            if a != b:
                ptrs = [prg[a - base + i] | prg[b - base + i] << 8 for i in range(8) if b - base + i < len(prg)]
                if sum(p in starts for p in ptrs) >= 2:
                    hits.update(p for p in ptrs if p in starts)
    return [starts[a] for a in sorted(hits)]


def describe(kb: KB, addr: int) -> str | None:
    """Work-unit note for a ROM address inside a metatile table: what it is and where the pictures are."""
    from .visual import export_dir

    out = export_dir(kb)
    for t in tables(kb):
        if addr == t.addr or addr in t.extent():
            sheet = out / "metatiles" / f"sheet_{t.addr:04X}.png" if out else None
            where = f"; pictures: {sheet} (entry NN: {out / 'metatiles' / f'{t.addr:04X}'}/NN.png)" if sheet else ""
            return (f"16x16 block (metatile) table at ${t.addr:04X}, {t.count} entries, "
                    f"{t.layout}{'' if t.layout == 'interleaved' else f' stride {t.stride}'}, {t.order} order{where}")
    return None
