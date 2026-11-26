"""Pictures for the annotator: PNG helpers, jump-table detection, tiles, work-unit sections."""
from conftest import make_nes, make_prg
from renes import analyze, annotate, png, visual, rom as romlib
from renes.kb import KB, NON_ROM, Label

ENGINE = bytes([0x0A, 0xA8, 0x68, 0x85, 0x04, 0x68, 0x85, 0x05, 0xC8, 0xB1, 0x04, 0x85, 0x06,
                0xC8, 0xB1, 0x04, 0x85, 0x07, 0x6C, 0x06, 0x00])
CODE = {
    0x8000: bytes([0xA5, 0x10, 0x20, 0x00, 0x90]) + bytes([0x20, 0x81, 0x30, 0x81]),    # lda $10 / jsr engine / 2 entries
    0x8020: bytes([0xB5, 0x16, 0x38, 0xE9, 0x03, 0x20, 0x00, 0x90]) + bytes([0x20, 0x81]),  # lda $16,x / sec / sbc #3 / jsr
    0x8120: bytes([0xA9, 0x74, 0x99, 0x01, 0x02, 0xA9, 0x09, 0x9D, 0x9A, 0x04, 0x60]),   # lda #$74 / sta $0201,y / ... / rts
    0x8130: bytes([0x4C, 0x40, 0x80]),
    0x8040: bytes([0xA5, 0x30, 0xF0, 0x01, 0x60, 0x20, 0x20, 0x90]) + bytes([0x20, 0x81, 0x30, 0x81]),  # lda $30 / beq / rts / jsr helper
    0x9000: ENGINE,
    0x9020: bytes([0xA5, 0x55]) + ENGINE,                     # lda $55, then falls into a second engine copy
}


def kb_for(tmp_path, chr_rom=b"\x00" * 0x2000):
    rom = romlib.parse(make_nes(make_prg(CODE, reset=0x8000, nmi=0x8020), chr_rom), "t.nes")
    kb = KB(tmp_path / "t.db")
    kb.store_rom(rom)
    analyze.store(kb, analyze.analyze(rom))
    kb.commit()
    return kb


def test_png_round_trip_crop_and_diff():
    img = png.blank(16, 8, (10, 20, 30))
    rows = [bytearray(r) for r in img[2]]
    rows[3][5 * 3:5 * 3 + 3] = bytes([255, 0, 0])
    changed = (16, 8, [bytes(r) for r in rows])
    assert png.decode(png.encode(changed)) == changed
    assert png.diff_box(img, changed) == (5, 3, 6, 4)
    assert png.crop(changed, 4, 2, 7, 5)[:2] == (3, 3)
    assert png.scale(changed, 2)[:2] == (32, 16)


def test_jump_tables_are_found_with_their_index_variable(tmp_path):
    kb = kb_for(tmp_path)
    ds = {d.site: d for d in visual.dispatches(annotate.Context.load(kb))}
    plain, adjusted = ds[0x8002], ds[0x8025]
    assert plain.handlers == [0x8120, 0x8130] and plain.index_addr == 0x10 and plain.indexed is None
    assert adjusted.index_addr == 0x16 and adjusted.indexed == "x" and adjusted.adjust == 3
    assert adjusted.value(0) == 3
    assert ds[0x8045].index_addr == 0x55   # the helper's load, not the early-out test of $30 (feedback #38)


def test_tiles_written_and_exported(tmp_path):
    chr_rom = bytearray(0x2000)
    chr_rom[0x74 * 16] = 0xFF            # tile $74, top row: colour 1
    kb = kb_for(tmp_path, bytes(chr_rom))
    ctx = annotate.Context.load(kb)
    assert visual.tiles_written(ctx, 0x8120) == [0x74]
    assert visual.export_tiles(kb, tmp_path / "out") == 512
    w, h, rows = png.decode((tmp_path / "out" / "tiles" / "p0" / "74.png").read_bytes())
    assert (w, h) == (64, 64) and rows[0][:3] != rows[-1][:3]


def test_work_units_flag_unlabeled_ram_and_apply_warns_about_unbacked_roles(tmp_path):
    kb = kb_for(tmp_path)
    kb.set_setting("export_dir", str(tmp_path / "out"))
    visual.export_tiles(kb, tmp_path / "out")
    kb.commit()
    unit = annotate.work_unit(annotate.Context.load(kb), 0x8120)
    assert "### Unlabeled RAM this routine writes" in unit and "$049A" in unit
    assert "### Sprite tiles this routine writes" in unit and "74.png" in unit
    assert "entry 0 of the jump table at $8002" in unit and "not probed yet" in unit
    ctx = annotate.Context.load(kb)
    r = annotate.Report()
    annotate.apply_one(kb, ctx, {"routine": "$8120", "name": "SetObjectTimer", "summary": "Sets $049A,y to 9."}, r)
    assert any("timer" in w and "$049A" in w for w in r.warnings)
    r2 = annotate.Report()
    kb.add_label(Label(NON_ROM, 0x049A, "Object_Timer", "ram", "candidate", "claude", 0.3))
    annotate.apply_one(kb, annotate.Context.load(kb), {"routine": "$8120", "name": "SetObjectTimer", "summary": "x"}, r2)
    assert not r2.warnings                # the role is now backed by a labeled (testable) variable


def test_role_warning_skips_a_leading_verb_and_counts_sized_labels(tmp_path):
    kb = kb_for(tmp_path)
    r = annotate.Report()
    annotate.apply_one(kb, annotate.Context.load(kb), {"routine": "$8120", "name": "StepObjectRamp", "summary": "x"}, r)
    assert not r.warnings                 # Step is the verb here (feedback #35)
    r = annotate.Report()
    annotate.apply_one(kb, annotate.Context.load(kb), {"routine": "$8120", "name": "AdvanceRampStep", "summary": "x"}, r)
    assert any("step" in w for w in r.warnings)
    kb.add_label(Label(NON_ROM, 0x0490, "Object_Timers", "ram", "verified", "claude", 0.8, 16))
    r = annotate.Report()
    annotate.apply_one(kb, annotate.Context.load(kb), {"routine": "$8120", "name": "SetObjectTimer", "summary": "x"}, r)
    assert not r.warnings                 # $049A is Object_Timers+10 (feedback #37)


def test_metatile_tables_are_found_from_observed_blocks_and_rendered(tmp_path):
    from collections import Counter
    from renes import metatiles

    entries = [(i, i + 1, i + 0x10, i + 0x11) for i in range(0x20, 0x40, 2)]      # TL TR BL BR, 16 blocks
    table = bytes(b for tl, tr, bl, br in entries for b in (tl, bl, tr, br))       # stored column by column
    rom = romlib.parse(make_nes(make_prg({**CODE, 0xA000: table}, reset=0x8000, nmi=0x8020)), "t.nes")
    kb = KB(tmp_path / "t.db")
    kb.store_rom(rom)
    analyze.store(kb, analyze.analyze(rom))
    seen = {e for i, e in enumerate(entries) if i % 3 != 1}   # some never on screen; they still belong
    [t] = metatiles.find_tables(rom.prg, rom.prg_base, kb.classes(0), seen)
    assert (t.addr, t.order, t.layout, t.count) == (0xA000, "cols", "interleaved", 16)
    assert t.entry(rom.prg, rom.prg_base, 3) == entries[3]
    assert metatiles.render(kb, [t], metatiles.Observed(Counter(seen), {}), tmp_path / "out") == 16
    w, h, _ = png.decode((tmp_path / "out" / "metatiles" / "A000" / "0F.png").read_bytes())
    assert (w, h) == (16 * metatiles.ZOOM, 16 * metatiles.ZOOM)
    kb.set_setting("metatiles", __import__("json").dumps([__import__("dataclasses").asdict(t)]))
    kb.set_setting("export_dir", str(tmp_path / "out"))
    assert "16 entries" in metatiles.describe(kb, 0xA008) and metatiles.describe(kb, 0x9000) is None


def test_renderer_pointers_fix_table_starts_and_split_merged_tables(tmp_path):
    from renes import metatiles

    def cols(entries):
        return bytes(b for tl, tr, bl, br in entries for b in (tl, bl, tr, br))

    a = [(i, i + 1, i + 0x10, i + 0x11) for i in range(0x20, 0x40, 2)]        # 16 blocks at $A000
    b = [(i, i + 1, i + 0x10, i + 0x11) for i in range(0x60, 0x70, 2)]        # 8 blocks at $A040
    ptrs = bytes([0x00, 0x40, 0xA0, 0xA0])                                     # split lo/hi: $A000, $A040
    rom = romlib.parse(make_nes(make_prg({**CODE, 0xA000: cols(a) + cols(b), 0xA100: ptrs},
                                         reset=0x8000, nmi=0x8020)), "t.nes")
    kb = KB(tmp_path / "t.db")
    kb.store_rom(rom)
    analyze.store(kb, analyze.analyze(rom))
    seen = set(a[4:]) | set(b)                       # the first four blocks of $A000 never on screen
    found = metatiles.find_tables(rom.prg, rom.prg_base, kb.classes(0), seen)
    assert [(t.addr, t.count) for t in found] == [(0xA010, 20)]              # one late, merged run
    fixed = metatiles.refine(rom.prg, rom.prg_base, kb.classes(0), found)
    assert [(t.addr, t.count) for t in fixed] == [(0xA000, 16), (0xA040, 8)]
    assert [t.addr for t in metatiles.reached(rom.prg, rom.prg_base, {0xA100, 0xA102}, fixed)] == [0xA000, 0xA040]
