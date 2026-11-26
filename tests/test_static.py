import pytest

from conftest import make_nes, make_prg
from renes import rom as romlib
from renes.analyze import analyze
from renes.opcodes import OPCODES, branch_target


def test_opcode_table_has_all_151_official_opcodes():
    assert len(OPCODES) == 151
    assert OPCODES[0xAD].mnemonic == "lda" and OPCODES[0xAD].size == 3
    assert OPCODES[0x6C].mode == "ind"


def test_branch_target_wraps_backwards_and_forwards():
    assert branch_target(0x8010, 0xFE) == 0x8010  # BNE * (infinite loop)
    assert branch_target(0x8010, 0x05) == 0x8017


def test_parse_reads_header_fields():
    rom = romlib.parse(make_nes(make_prg({}, reset=0x8000)), "t.nes")
    assert (rom.mapper, rom.mirroring, len(rom.prg), len(rom.chr), rom.prg_base) == (0, "vertical", 0x8000, 0x2000, 0x8000)


@pytest.mark.parametrize("data, message", [
    (b"XXXX" + bytes(12), "magic"),
    (make_nes(make_prg({}, reset=0x8000))[:-1], "size mismatch"),
], ids=["bad-magic", "truncated"])
def test_parse_rejects_bad_files(data, message):
    with pytest.raises(romlib.RomError, match=message):
        romlib.parse(data)


def test_analyze_follows_vectors_branches_and_calls():
    code = {
        0x8000: bytes([0x78,              # sei
                       0x20, 0x10, 0x80,  # jsr $8010
                       0xAD, 0x00, 0x90,  # lda $9000 (data ref)
                       0xD0, 0xF7,        # bne $8000
                       0x4C, 0x00, 0x80]),  # jmp $8000
        0x8010: bytes([0x60]),            # rts
        0x8020: bytes([0x40]),            # rti (NMI)
    }
    a = analyze(romlib.parse(make_nes(make_prg(code, reset=0x8000, nmi=0x8020))))
    assert a.labels[0x8000] == ("Reset", "vector")
    assert a.labels[0x8010] == ("Sub8010", "routine")
    assert a.labels[0x8020] == ("NMI", "vector")
    assert a.labels[0x9000] == ("Data9000", "data")
    assert a.kinds[0x8001] == "op" and a.kinds[0x8002] == "operand"
    assert 0x8011 not in a.kinds  # stops after rts


def test_analyze_stops_at_undocumented_opcode_and_brk():
    code = {0x8000: bytes([0xEA, 0x02, 0xEA]), 0x8100: bytes([0xEA, 0x00, 0xEA])}
    a = analyze(romlib.parse(make_nes(make_prg(code, reset=0x8000, nmi=0x8100))))
    assert a.kinds.get(0x8001) is None and a.kinds.get(0x8101) is None
