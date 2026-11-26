"""Multi-bank (AxROM) ROMs: far calls through a bank switch, and annotation addressed by bank."""
from conftest import make_nes, make_prg, needs_cc65
from renes import analyze, annotate, rom as romlib
from renes.banking import FarCall, far_calls
from renes.kb import KB
from renes.render import render
from renes.verify import build_and_verify

# The same trampoline at $FF00 in both banks: X = jump-table offset, kept in $54 (high byte $55);
# switch to bank 1, call $8000 there (jmp ($0054)), switch back.
TRAMPOLINE = bytes([0x86, 0x54, 0xA9, 0x80, 0x85, 0x55,      # stx $54 / lda #$80 / sta $55
                    0xA0, 0x01, 0x8C, 0x00, 0x80,              # ldy #1 / sty $8000
                    0x20, 0x00, 0x80,                          # jsr $8000
                    0xA9, 0x00, 0x8D, 0x00, 0x80, 0x60])       # lda #0 / sta $8000 / rts


def axrom(tmp_path):
    bank0 = make_prg({0x8000: bytes([0x20, 0x00, 0x90, 0x4C, 0x00, 0x80]),     # jsr $9000 / jmp $8000
                      0x9000: bytes([0xA2, 0x03, 0x20, 0x00, 0xFF, 0x60]),     # ldx #3 / jsr $FF00 / rts
                      0xFF00: TRAMPOLINE}, reset=0x8000)
    bank1 = make_prg({0x8000: bytes([0x6C, 0x54, 0x00,                         # jmp ($0054)
                                     0x4C, 0x00, 0x91]),                       # $8003: jmp $9100
                      0x8010: bytes([0x20, 0x00, 0xFF, 0x4C, 0x10, 0x80]),     # reset: jsr $FF00 / loop
                      0x9100: bytes([0xE8, 0x60]),                             # the far routine: inx / rts
                      0xFF00: TRAMPOLINE}, reset=0x8010)
    nes = make_nes(bank0 + bank1, b"", mapper=7)
    rom = romlib.parse(nes, "t.nes")
    path = tmp_path / "t.db"
    with KB(path) as kb:
        kb.store_rom(rom)
        analyze.analyze_and_store(kb, rom)
        kb.commit()
    return path, nes


def test_far_call_lands_on_the_routine_behind_the_jump_table(tmp_path):
    path, _ = axrom(tmp_path)
    with KB(path) as kb:
        assert far_calls(kb) == [FarCall(0, 0x9000, 0x9002, 1, 0x9100)]
        # only reachable from bank 0: the second analysis pass makes it a routine of bank 1
        assert 0x9100 in {e for e, _, _ in kb.routines(1)}
        ctx0, ctx1 = annotate.contexts(kb)
        assert ctx0.far_out[0x9000] == [(1, 0x9100, 0x9002)] and ctx1.far_in[0x9100] == [(0, 0x9000, 0x9002)]
        unit = annotate.work_unit(ctx0, 0x9000)
        assert "Calls in other banks" in unit and "B1_Sub9100 (bank 1)" in unit
        assert 'give the routine as "B0:$9000"' in unit
        assert "Called from other banks: B0_Sub9000 (bank 0, $9002)" in annotate.work_unit(ctx1, 0x9100)


def test_routines_are_addressed_by_bank_when_banks_share_addresses(tmp_path):
    path, _ = axrom(tmp_path)
    with KB(path) as kb:
        ctxs = annotate.contexts(kb)
        assert annotate.resolve_routine(kb, ctxs, "B1:$9100") == (1, 0x9100)
        assert annotate.resolve_routine(kb, ctxs, "$9100") == (1, 0x9100)          # only bank 1 has one there
        assert "write it as B0:$FF00" in annotate.resolve_routine(kb, ctxs, "$FF00")  # both banks do
        assert annotate.resolve_routine(kb, ctxs, "B1_Sub9100") == (1, 0x9100)
        assert "not a routine entry" in annotate.resolve_routine(kb, ctxs, "B0:$9100")
        assert {c.bank for c, _ in annotate.queue_all(ctxs)} == {0, 1}


@needs_cc65
def test_apply_writes_to_the_routines_bank_and_still_rebuilds(tmp_path):
    path, nes = axrom(tmp_path)
    report, ok = annotate.apply(path, [
        {"routine": "B1:$9100", "name": "BumpCounter", "summary": "Adds one to X.", "lines": {"$9100": "one more"}},
        {"routine": "B0:$FF00", "name": "CallBank1", "summary": "Calls bank 1's table entry X."},
    ], tmp_path / "out")
    assert ok, report.rejected
    with KB(path) as kb:
        assert kb.label_at(1, 0x9100).name == "BumpCounter" and kb.label_at(0, 0x9100) is None
        assert kb.label_at(0, 0xFF00).name == "CallBank1" and kb.label_at(1, 0xFF00).name != "CallBank1"
        assert kb.comments(1)[0x9100] and 0x9100 not in kb.comments(0)
        assert annotate.progress(kb)["annotated"] == 2
        assert build_and_verify(render(kb), nes, tmp_path / "again", "t").identical
