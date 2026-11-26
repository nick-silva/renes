"""M3: CDL-guided analysis, jump engines, BIT-skip splitting, routines and call graph."""
import pytest

from conftest import EXAMPLE_ROM, make_nes, make_prg, needs_cc65, rom_fact
from renes import analyze, rom as romlib
from renes.analyze import CDL_CODE, CDL_DATA, CDL_JUMP, CDL_SUB, is_jump_engine
from renes.kb import KB
from renes.render import render
from renes.verify import build_and_verify

# A typical jump engine:  asl a / tay / pla / sta $04 / pla / sta $05 / iny / lda ($04),y / sta $06
#                        iny / lda ($04),y / sta $07 / jmp ($0006)
ENGINE = bytes([0x0A, 0xA8, 0x68, 0x85, 0x04, 0x68, 0x85, 0x05, 0xC8, 0xB1, 0x04, 0x85, 0x06,
                0xC8, 0xB1, 0x04, 0x85, 0x07, 0x6C, 0x06, 0x00])


def rom_of(code: dict[int, bytes], **kw) -> romlib.Rom:
    return romlib.parse(make_nes(make_prg(code, **kw)))


def cdl_for(rom: romlib.Rom, marks: dict[int, int]) -> bytes:
    cdl = bytearray(len(rom.prg))
    for addr, flags in marks.items():
        cdl[addr - rom.prg_base] |= flags
    return bytes(cdl)


def test_jump_engine_is_recognized_and_its_table_decoded():
    code = {
        0x8000: bytes([0xA9, 0x01, 0x20, 0x00, 0x90]) + bytes([0x20, 0x81, 0x30, 0x81]),  # lda #1 / jsr engine / .addr x2
        0x8120: bytes([0x60]),            # first table target: rts
        0x8130: bytes([0x4C, 0x30, 0x81]),  # second: jmp *
        0x9000: ENGINE,
    }
    rom = rom_of(code, reset=0x8000)
    assert is_jump_engine(rom.prg, rom.prg_base, 0x9000)
    a = analyze.analyze(rom)
    assert a.jump_engines == {0x9000}
    assert a.tables == {0x8005: 2}
    assert a.kinds[0x8005] == "word" and a.kinds[0x8007] == "word"
    assert a.labels[0x8120][1] == "routine" and a.labels[0x8130][1] == "routine"
    assert (0x8000, 0x8120, 0x8002, "table") in a.calls


def test_plain_subroutine_is_not_a_jump_engine():
    rom = rom_of({0x8000: bytes([0x68, 0x68, 0x60])}, reset=0x8000)  # pla / pla / rts
    assert not is_jump_engine(rom.prg, rom.prg_base, 0x8000)


def test_proven_start_inside_operand_splits_bit_skip():
    # $8000: ldy #$00 / $8002: .byte $2C (bit abs) hiding $8003: ldy #$01 / $8005: rts
    code = {0x8000: bytes([0xA0, 0x00, 0x2C, 0xA0, 0x01, 0x60])}
    rom = rom_of(code, reset=0x8000)
    executed = {x: CDL_CODE for x in range(0x8000, 0x8006)}
    executed[0x8003] |= CDL_JUMP
    a = analyze.analyze(rom, cdl_for(rom, executed))
    assert a.kinds[0x8002] == "data" and a.sources[0x8002] == "skip"
    assert a.kinds[0x8003] == "op" and a.kinds[0x8005] == "op"


def test_bit_skip_found_when_hidden_instruction_was_decoded_first():
    # $8000: jsr $8010 / jsr $8013 / rts ; $8010: lda #$00 / .byte $2C / $8013: lda #$02 / rts
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x20, 0x13, 0x80, 0x60]),
            0x8010: bytes([0xA9, 0x00, 0x2C, 0xA9, 0x02, 0x60])}
    a = analyze.analyze(rom_of(code, reset=0x8000))
    assert a.kinds[0x8012] == "data" and a.sources[0x8012] == "skip"
    assert a.routines[0x8010][0] == 0x8012  # the routine runs up to its skip byte


def test_static_descent_never_claims_bytes_seen_only_as_data():
    # jsr $8010, where $8010.. was only ever read as data by the game
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x4C, 0x03, 0x80]), 0x8010: bytes([0xA9, 0x05, 0x60])}
    rom = rom_of(code, reset=0x8000)
    cdl = cdl_for(rom, {**{x: CDL_CODE for x in range(0x8000, 0x8006)},
                        **{x: CDL_DATA for x in range(0x8010, 0x8013)}})
    a = analyze.analyze(rom, cdl)
    assert 0x8010 not in a.kinds
    assert a.sources[0x8000] == "exec"


def test_unexecuted_code_is_reached_statically_in_phase_b():
    code = {0x8000: bytes([0xD0, 0x02, 0xEA, 0x60, 0xE8, 0x60])}  # bne $8004 / nop / rts / inx / rts
    rom = rom_of(code, reset=0x8000)
    a = analyze.analyze(rom, cdl_for(rom, {x: CDL_CODE for x in range(0x8000, 0x8004)}))
    assert a.sources[0x8000] == "exec" and a.sources[0x8004] == "static"


def test_routines_get_extents_and_cdl_sub_entries():
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x60]), 0x8010: bytes([0xE8, 0xE8, 0x60])}
    rom = rom_of(code, reset=0x8000)
    a = analyze.analyze(rom, cdl_for(rom, {0x8010: CDL_CODE | CDL_SUB, 0x8011: CDL_CODE, 0x8012: CDL_CODE}))
    assert a.routines[0x8010] == (0x8012, "jsr")
    assert a.routines[0x8000][0] == 0x8003
    assert (0x8000, 0x8010, 0x8000, "jsr") in a.calls


def test_a_logged_entry_nothing_calls_stays_in_the_routine_that_runs_into_it():
    # $8010: lda $00 / sta $01 / rts, with the logger marking $8012 (as it does where an NMI interrupted);
    # $8020: inx / rts, marked too and called by nothing, but after an rts it is a routine of its own
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x4C, 0x00, 0x80]), 0x8010: bytes([0xA5, 0x00, 0x85, 0x01, 0x60]),
            0x8020: bytes([0xE8, 0x60])}
    rom = rom_of(code, reset=0x8000)
    marks = {x: CDL_CODE for x in [*range(0x8000, 0x8006), *range(0x8010, 0x8015), 0x8020, 0x8021]}
    a = analyze.analyze(rom, cdl_for(rom, marks | {0x8010: CDL_CODE | CDL_SUB, 0x8012: CDL_CODE | CDL_SUB,
                                               0x8020: CDL_CODE | CDL_SUB}))
    assert 0x8012 not in a.routines and a.routines[0x8010][0] == 0x8014
    assert 0x8020 in a.routines


def test_code_past_a_data_gap_belongs_to_the_routine_that_branches_there():
    # $8010: lda $00 / beq $8018 / rts / .byte $FF x3 / $8018: inx / rts
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x4C, 0x00, 0x80]),
            0x8010: bytes([0xA5, 0x00, 0xF0, 0x04, 0x60, 0xFF, 0xFF, 0xFF, 0xE8, 0x60])}
    a = analyze.analyze(rom_of(code, reset=0x8000))
    assert a.kinds.get(0x8015) != "op" and a.kinds[0x8018] == "op"
    assert a.routines[0x8010][0] == 0x8019


def test_code_only_reached_from_another_routine_becomes_a_routine():
    # $8010: rts / .byte $FF / $8012: inx / rts, entered only by $8020: jmp $8012
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x20, 0x20, 0x80, 0x4C, 0x00, 0x80]),
            0x8010: bytes([0x60, 0xFF, 0xE8, 0x60]), 0x8020: bytes([0x4C, 0x12, 0x80])}
    a = analyze.analyze(rom_of(code, reset=0x8000))
    assert a.routines[0x8012] == (0x8013, "jmp")
    assert (0x8020, 0x8012, 0x8020, "jmp") in a.calls


@pytest.mark.parametrize("load, decoded", [(0xA5, True),    # lda $10 / bne: can go either way
                                           (0xA9, False)])  # lda #$10 / bne: always taken
def test_fall_through_seen_only_as_data_is_decoded_unless_the_branch_is_always_taken(load, decoded):
    # $8000: lda / bne $8007 / $8004: inx / inx / rts (read as data, never run) / $8007: rts
    code = {0x8000: bytes([load, 0x10, 0xD0, 0x03, 0xE8, 0xE8, 0x60, 0x60])}
    rom = rom_of(code, reset=0x8000)
    cdl = cdl_for(rom, {**{x: CDL_CODE for x in (0x8000, 0x8001, 0x8002, 0x8003, 0x8007)},
                        **{x: CDL_DATA for x in (0x8004, 0x8005, 0x8006)}})
    a = analyze.analyze(rom, cdl)
    assert (a.kinds.get(0x8004) == "op") == decoded


def test_targets_outside_prg_get_no_label():
    code = {0x8000: bytes([0x20, 0xA9, 0x05, 0x60])}  # jsr $05A9 (code in RAM)
    a = analyze.analyze(rom_of(code, reset=0x8000))
    assert 0x05A9 not in a.labels


@needs_cc65
def test_cdl_guided_output_still_round_trips(tmp_path):
    code = {0x8000: bytes([0xA0, 0x00, 0x2C, 0xA0, 0x01, 0x20, 0x00, 0x90, 0x20, 0x80]),
            0x8020: bytes([0x60]), 0x9000: ENGINE}
    rom = rom_of(code, reset=0x8000)
    cdl = cdl_for(rom, {**{x: CDL_CODE for x in range(0x8000, 0x8008)}, 0x8003: CDL_CODE | CDL_JUMP})
    with KB(tmp_path / "t.db") as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom, cdl))
        rendered = render(kb)
    result = build_and_verify(rendered, rom.raw, tmp_path / "out", "t")
    assert result.identical
    assert ".byte $2C" in rendered.asm and ".addr Sub8020" in rendered.asm


@pytest.mark.skipif(not EXAMPLE_ROM.exists(), reason="example ROM not present")
def test_example_rom_jump_engine_found_statically():
    engine, table, entries = rom_fact("jump_engine"), rom_fact("jump_table"), rom_fact("jump_table_entries")
    a = analyze.analyze(romlib.load(EXAMPLE_ROM))
    assert engine in a.jump_engines
    assert a.tables[table] == entries
