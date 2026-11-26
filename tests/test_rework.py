"""Backlog items 5, 8, 10: weak names sent back, evidence vs claimed confidence, slot picking."""
import pytest

from conftest import ROOT, make_nes, make_prg
from renes import analyze, annotate, confidence, review, rework, visual, rom as romlib
from renes.kb import KB, NON_ROM, Label

# $8000: lda $10 / sta a:$0011 / jsr $8030 / jsr $8040 / jsr $8042 / jmp $8000
# $8030: inc $10 / rts          $8040: lda #1 (falls through)          $8042: sta $12 / rts
CODE = {0x8000: bytes([0xA5, 0x10, 0x8D, 0x11, 0x00, 0x20, 0x30, 0x80, 0x20, 0x40, 0x80, 0x20, 0x42, 0x80,
                       0x4C, 0x00, 0x80]),
        0x8030: bytes([0xE6, 0x10, 0x60]),
        0x8040: bytes([0xA9, 0x01, 0x85, 0x12, 0x60])}


def note(kb, entry, name, summary, conf=0.6):
    kb.add_label(Label(0, entry, name, "routine", "candidate", "claude", conf), replace=True)
    kb.set_routine_note(0, entry, summary, "candidate", "claude", conf, "t")


@pytest.fixture
def kb(tmp_path):
    rom = romlib.parse(make_nes(make_prg(CODE, reset=0x8000)), "t.nes")
    with KB(tmp_path / "t.db") as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom))
        note(kb, 0x8030, "IncByte10", "Adds one to $0010.")
        note(kb, 0x8040, "SetATo1ThenContinue", "Loads 1 into A and continues.")
        note(kb, 0x8042, "StoreLivesCount", "Stores A in Lives_Count.")
        kb.commit()
        yield kb


def test_raw_address_names_are_sent_back_once_the_address_is_named(kb):
    assert 0x8030 not in dict(rework.scan(kb))                      # $10 has no name yet: nothing to do
    kb.add_label(Label(NON_ROM, 0x10, "Frame_Tally", "ram", "candidate", "claude", 0.6))
    sent = dict(rework.scan(kb))
    assert "Frame_Tally" in sent[0x8030]
    assert dict(kb.db.execute("SELECT entry, status FROM routine_notes"))[0x8030] == "rework"
    unit = annotate.work_unit(annotate.Context.load(kb), 0x8030)
    assert "### Why this name was sent back" in unit and "Frame_Tally" in unit
    kb.db.execute("UPDATE routine_notes SET status='candidate'")    # re-annotated, kept the name
    assert 0x8030 not in dict(rework.scan(kb))                      # the same reason never re-queues it


def test_continue_names_are_sent_back_with_what_follows(kb):
    sent = dict(rework.scan(kb))
    assert "StoreLivesCount" in sent[0x8040] and "Stores A in Lives_Count" in sent[0x8040]
    assert 0x8042 not in sent


def test_value_in_a_name_points_at_its_probe_picture(kb, tmp_path):
    note(kb, 0x8040, "SetATo01", "Loads 1 into A.")
    kb.set_setting("export_dir", str(tmp_path / "out"))
    folder = tmp_path / "out" / "probes" / visual.probe_folder(kb, 0x12, 0)
    folder.mkdir(parents=True)
    (folder / "01.png").write_bytes(b"png")
    kb.add_media("probe", NON_ROM, 0x12, 0, "full", 1, b"png", "t")
    sent = dict(rework.scan(kb))
    assert "01.png" in sent[0x8040]


def test_placeholder_parts():
    assert rework.placeholder_parts("Store0InByte39") == ["raw number $39"]
    assert rework.placeholder_parts("ZeroXYThenContinueF142")[-1].startswith("'continue'")
    assert not rework.placeholder_parts("WaitSprite0Clear") and not rework.placeholder_parts("DrawBCDDigits")
    assert not rework.placeholder_parts("L81C5")                    # auto names are not placeholders


def test_evidence_scores_and_effective_confidence(kb):
    kb.add_label(Label(NON_ROM, 0x10, "Frame_Tally", "ram", "verified", "experiment", 0.9))
    kb.add_label(Label(NON_ROM, 0x12, "Lives_Count", "ram", "candidate", "claude", 0.8))
    kb.add_experiment("t", NON_ROM, 0x12, "lives", "{}", False, "{}")
    kb.db.execute("INSERT INTO traces (bank, entry, state_id, frame, rows, created) VALUES (0, ?, NULL, 1, '[]', 't')",
                  (0x8030,))
    note(kb, 0x8030, "BumpFrameTally", "Adds one to Frame_Tally.", conf=0.85)
    summary = confidence.refresh(kb)
    routines, ram = confidence.scores(kb, 0), confidence.scores(kb, NON_ROM)
    score, claimed, eff, f = routines[0x8030]
    assert f["traced"] == 1 and f["verified"] == 1 and f["ran"] == 0 and f["coherent"] == 1
    assert score == pytest.approx(0.1 + 0.15 + 0.1 + 0.1 * f["context"])      # traced, verified RAM, coherent, context
    assert eff == 0.85                                                          # coherent: the claim stands
    assert routines[0x8040][2] == pytest.approx(0.36)                           # placeholder name: 0.6 x 0.6
    assert ram[0x10][0] == 0.9 and ram[0x12][0] == pytest.approx(0.1)            # verified; -0.2 failed + writer + named
    assert summary["overconfident"] >= 1 and summary["incoherent"] == 1
    flags = {i["name"]: i["flags"] for i in review.items(kb)}
    assert "placeholder name" in flags["SetATo1ThenContinue"] and "low confidence" in flags["SetATo1ThenContinue"]
    assert "overconfident" in flags["Lives_Count"]
    assert confidence.effective(0.4, "approved") == 1.0 and confidence.effective(None, "candidate") == 0.5


MESEN = ROOT / "tools" / "mesen" / "Mesen.exe"
ENGINE = bytes([0x0A, 0xA8, 0x68, 0x85, 0x04, 0x68, 0x85, 0x05, 0xC8, 0xB1, 0x04, 0x85, 0x06,
                0xC8, 0xB1, 0x04, 0x85, 0x07, 0x6C, 0x06, 0x00])


@pytest.mark.skipif(not MESEN.exists(), reason="Mesen missing")
def test_indexed_jump_table_slot_is_found_by_running_it(tmp_path):
    # $8000: ldx #2 / lda $16,x / jsr engine / .word $8120, $8120 ; $8120: jmp $8000
    code = {0x8000: bytes([0xA2, 0x02, 0xB5, 0x16, 0x20, 0x00, 0x90, 0x20, 0x81, 0x20, 0x81]),
            0x8120: bytes([0x4C, 0x00, 0x80]), 0x9000: ENGINE}
    rom = romlib.parse(make_nes(make_prg(code, reset=0x8000)), "t.nes")
    path = tmp_path / "t.db"
    with KB(path) as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom))
        kb.commit()
        [d] = visual.dispatches(annotate.Context.load(kb))
    assert d.indexed == "x"
    state, slots = visual.find_slot(path, d, frames=2)
    assert state is None and slots[0][0] == 2


def test_same_code_in_another_project_becomes_a_hint(tmp_path):
    from renes import library

    def project(name, code, notes):
        rom = romlib.parse(make_nes(make_prg(code, reset=0x8000)), f"{name}.nes")
        with KB(tmp_path / f"{name}.db") as kb:
            kb.store_rom(rom)
            analyze.store(kb, analyze.analyze(rom))
            for entry, (label, summary, updated) in notes.items():
                kb.add_label(Label(0, entry, label, "routine", "candidate", "claude", 0.7), replace=True)
                kb.set_routine_note(0, entry, summary, "candidate", "claude", 0.7, updated)
            kb.add_label(Label(NON_ROM, 0x20, "Joypad_Buttons", "ram", "candidate", "claude", 0.7))
            kb.commit()
        return tmp_path / f"{name}.db"

    # read the joypad into a RAM byte: same code, different RAM address and location
    def joypad(ram):
        return bytes([0xA9, 0x01, 0x8D, 0x16, 0x40, 0xA9, 0x00, 0x8D, 0x16, 0x40, 0xA2, 0x08,
                      0xAD, 0x16, 0x40, 0x4A, 0x26, ram, 0xCA, 0xD0, 0xF7, 0x60])
    a = project("a", {0x8000: bytes([0x20, 0x00, 0x90, 0x4C, 0x00, 0x80]), 0x9000: joypad(0x20)},
                {0x9000: ("ReadJoypad", "Reads controller 1 into Joypad_Buttons.", "2026-01-01T00:00:00+00:00")})
    b = project("b", {0x8000: bytes([0x20, 0x00, 0xA0, 0x4C, 0x00, 0x80]), 0xA000: joypad(0x31)}, {})
    projects = {"a": a, "b": b}
    assert library.refresh(b, projects)["routines"] == 1
    with KB(b) as kb:
        [h] = library.hints(kb, 0xA000)
        assert h["name"] == "ReadJoypad" and h["kind"] == "same shape"
        assert {"ours": "$0031", "theirs": "$0020", "name": "Joypad_Buttons"} in h["ram"]
        assert "$0031 ~ Joypad_Buttons" in "\n".join(library.section(kb, 0xA000))
    with KB(a) as kb:                                 # names written after the reference was read stay home
        kb.set_setting("reference_seen", "2025-12-31T00:00:00+00:00 (test)")
        kb.commit()
    assert library.refresh(b, projects)["routines"] == 0
