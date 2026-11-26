"""Round trip: KB -> .asm -> ca65/ld65 -> must be byte-identical to the input ROM."""
import random

import pytest

from conftest import make_nes, make_prg, needs_cc65, real_roms
from renes import analyze, rom as romlib
from renes.cli import main
from renes.kb import KB
from renes.opcodes import OPCODES
from renes.render import render
from renes.verify import build_and_verify


def roundtrip(tmp_path, nes: bytes, classes: dict[int, str] | None = None):
    rom = romlib.parse(nes, "t.nes")
    with KB(tmp_path / "t.db") as kb:
        kb.store_rom(rom)
        result = analyze.analyze(rom)
        if classes is not None:
            result.kinds.update(classes)
        analyze.store(kb, result)
        rendered = render(kb)
    return build_and_verify(rendered, nes, tmp_path / "out", "t"), rendered


@needs_cc65
def test_every_opcode_with_awkward_operands_reassembles_identically(tmp_path):
    """Each official opcode with operands $0010 / $00FF / $8000 (abs < $100 needs `a:`)."""
    blob, kinds, pc = bytearray(), {}, 0x8000
    for operand in (0x0010, 0x00FF, 0x8000):
        for op in sorted(OPCODES.values(), key=lambda o: o.code):
            kinds[pc] = "op"
            raw = [op.code] + [operand & 0xFF, operand >> 8][: op.size - 1]
            for i in range(1, op.size):
                kinds[pc + i] = "operand"
            blob += bytes(raw)
            pc += op.size
    nes = make_nes(make_prg({0x8000: bytes(blob)}, reset=0xF000))
    result, rendered = roundtrip(tmp_path, nes, kinds)
    assert result.identical, f"first diff at ${result.first_diff:X}"
    assert "lda a:$0010" in rendered.asm


@needs_cc65
def test_random_bytes_reassemble_identically(tmp_path):
    rng = random.Random(1234)
    prg = bytes(rng.randrange(256) for _ in range(0x8000))
    nes = make_nes(prg, bytes(rng.randrange(256) for _ in range(0x2000)))
    result, _ = roundtrip(tmp_path, nes)
    assert result.identical, f"first diff at ${result.first_diff:X}"


@needs_cc65
def test_nrom_128_mirrors_at_c000(tmp_path):
    nes = make_nes(make_prg({0xC000: bytes([0x4C, 0x00, 0xC0])}, reset=0xC000, size=0x4000))
    result, rendered = roundtrip(tmp_path, nes)
    assert result.identical
    assert ".org $C000" in rendered.asm


@needs_cc65
@pytest.mark.parametrize("path", real_roms(), ids=lambda p: p.name)
def test_real_rom_round_trip_via_cli(tmp_path, path):
    kb = tmp_path / "game.db"
    assert main(["init", str(path), "--kb", str(kb)]) == 0
    assert main(["build", str(kb), "--out", str(tmp_path / "out")]) == 0


@needs_cc65
def test_axrom_banks_are_analysed_separately_and_rebuild_identically(tmp_path):
    # two 32 KB banks, both at $8000; the same stub address does different things in each bank
    bank0 = make_prg({0x8000: bytes([0xA9, 0x01, 0x8D, 0x00, 0x80, 0x4C, 0x00, 0x90]),  # lda #1 / sta $8000 / jmp $9000
                      0x9000: bytes([0x20, 0x10, 0x90, 0x60]), 0x9010: bytes([0x60])}, reset=0x8000)
    bank1 = make_prg({0x8000: bytes([0xA9, 0x00, 0x8D, 0x00, 0x80, 0x4C, 0x00, 0x80]),
                      0x9000: bytes([0xE8, 0x60])}, reset=0x8000)
    nes = make_nes(bank0 + bank1, b"", mapper=7)                 # CHR RAM: no CHR ROM
    rom = romlib.parse(nes, "t.nes")
    assert [(b.index, b.base, b.offset) for b in rom.banks()] == [(0, 0x8000, 0), (1, 0x8000, 0x8000)]
    with KB(tmp_path / "t.db") as kb:
        kb.store_rom(rom)
        for result in analyze.analyze_banks(rom):
            analyze.store(kb, result)
        assert kb.label_at(0, 0x8000).name == "B0_NMI" and kb.label_at(1, 0x8000).name == "B1_NMI"   # NMI = Reset here
        assert kb.label_at(0, 0x9010) is not None and kb.label_at(1, 0x9010) is None
        rendered = render(kb)
    assert rendered.cfg.count("start = $8000") == 2 and "CHR" not in rendered.cfg
    assert build_and_verify(rendered, nes, tmp_path / "out", "t").identical
