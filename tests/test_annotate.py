"""M4: work units, bottom-up queue, proposal validation, atomic apply with round-trip guard."""
import pytest

from conftest import make_nes, make_prg, needs_cc65
from renes import analyze, annotate, rom as romlib
from renes.kb import KB, NON_ROM, Label
from renes.render import render

# Reset: lda $0010 (zp) / sta a:$0011 (abs) / jsr $8010 / jmp $8000 ; $8010: inc $0010 / rts
CODE = {0x8000: bytes([0xA5, 0x10, 0x8D, 0x11, 0x00, 0x20, 0x10, 0x80, 0x4C, 0x00, 0x80]),
        0x8010: bytes([0xE6, 0x10, 0x60])}


@pytest.fixture
def kb_path(tmp_path):
    rom = romlib.parse(make_nes(make_prg(CODE, reset=0x8000)), "t.nes")
    path = tmp_path / "t.db"
    with KB(path) as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom))
    return path


def run_apply(kb_path, tmp_path, proposals):
    return annotate.apply(kb_path, proposals, tmp_path / "out")


def test_queue_is_bottom_up(kb_path):
    with KB(kb_path) as kb:
        ctx = annotate.Context.load(kb)
        assert annotate.queue(ctx)[:2] == [0x8010, 0x8000]  # callee before caller


def test_work_unit_shows_code_callers_and_ram(kb_path):
    with KB(kb_path) as kb:
        unit = annotate.work_unit(annotate.Context.load(kb), 0x8010)
    assert "Called from: NMI ($8005 jsr)" in unit  # all vectors point at $8000; NMI is named first
    assert "inc $10" in unit and "$0010  rw" in unit


@needs_cc65
def test_apply_names_routine_and_ram_and_still_round_trips(kb_path, tmp_path):
    report, ok = run_apply(kb_path, tmp_path, [{
        "routine": "$8010", "name": "BumpCounter", "summary": "Increments the frame counter.",
        "lines": {"$8010": "count one more frame"},
        "ram": {"$0010": {"name": "Frame_Counter", "comment": "frames since reset"},
                "$0011": {"name": "Counter_Copy"}},
        "confidence": 0.6}])
    assert ok, report.rejected
    with KB(kb_path) as kb:
        asm = render(kb).asm
        ctx = annotate.Context.load(kb)
        assert "summary $8010" in report.accepted
        assert ctx.notes[0x8010] == "Increments the frame counter."
        assert "Increments the frame counter." in annotate.work_unit(ctx, 0x8000)  # callers see it
    assert "BumpCounter:" in asm and "jsr BumpCounter" in asm
    assert "lda Frame_Counter" in asm          # zero page stays zero page
    assert "sta a:Counter_Copy" in asm         # absolute stays absolute
    assert "count one more frame" in asm and "? frames since reset" in asm


def test_text_over_the_limit_is_cut_with_a_warning(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "s" * 450,
                                                "notes": ["fine", "n" * 150], "lines": {"$8010": "short"}}])
    assert ok and len(report.warnings) == 2
    assert report.warnings[0].startswith("summary $8010: 450 characters, cut to 400")
    assert report.warnings[1].startswith("note 2 of $8010: 150 characters, cut to 140")


def test_an_empty_line_comment_removes_the_old_one(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "x", "lines": {"$8010": "never seen"}}])
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "x", "lines": {"$8010": ""}}])
    assert ok and "comment $8010 removed" in report.accepted
    with KB(kb_path) as kb:
        assert 0x8010 not in kb.comments(0)


@pytest.mark.parametrize("name, reason", [
    ("lda", "reserved"), ("Sub8000", "auto-generated"), ("9Lives", "letters"), ("PPUCTRL", "already names"),
])
def test_bad_names_are_rejected(kb_path, tmp_path, monkeypatch, name, reason):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    with KB(kb_path) as kb:
        kb.add_label(Label(NON_ROM, 0x2000, "PPUCTRL", "hardware", "builtin", "hardware"))
        kb.commit()
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "$8010", "name": name, "summary": "x"}])
    assert ok
    assert any(reason in r for r in report.rejected)


def test_comment_must_be_on_an_instruction_inside_the_routine(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "x",
                                                "lines": {"$8011": "inside an operand", "$8000": "other routine"}}])
    assert ok and len(report.rejected) == 2


def test_verified_labels_are_protected(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    with KB(kb_path) as kb:
        kb.add_label(Label(0, 0x8010, "HumanName", "routine", "verified", "human"), replace=True)
        kb.commit()
    report, _ = run_apply(kb_path, tmp_path, [{"routine": "$8010", "name": "MachineName", "summary": "Counts frames."}])
    assert any("verified" in r and "proposed for review" in r for r in report.rejected)
    with KB(kb_path) as kb:
        assert kb.label_at(0, 0x8010).name == "HumanName"
        assert kb.rename_proposals() == [(0, 0x8010, "HumanName", "MachineName", "Counts frames.", None)]
        from renes import questions
        [you] = [g for g in questions.questions(kb) if g["id"] == "you"]
        assert any(i["text"] == "rename HumanName -> MachineName?" for i in you["items"])
        run_apply(kb_path, tmp_path, [{"ram": {"$0010": {"name": "Frame_Counter"}}}])
        kb.set_label_status(NON_ROM, 0x0010, "verified")
        kb.commit()
    run_apply(kb_path, tmp_path, [{"ram": {"$0010": {"name": "Tick_Count", "why": "ticks, not frames"}}}])
    with KB(kb_path) as kb:
        assert kb.rename_proposals()[-1][3:5] == ("Tick_Count", "ticks, not frames")
        assert 0x0010 not in kb.comments(NON_ROM)            # the reason is not a comment
        kb.rename_label(0, 0x8010, "MachineName", "approved", "human")
        assert [p[3] for p in kb.rename_proposals()] == ["Tick_Count"]   # MachineName is done


def test_routines_can_be_named_instead_of_addressed(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    run_apply(kb_path, tmp_path, [{"routine": "$8010", "name": "BumpThing", "summary": "first"}])
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "BumpThing", "summary": "second"},
                                               {"routine": "NoSuchRoutine", "summary": "x"}])
    assert ok and any("NoSuchRoutine: neither an address nor a routine name" in r for r in report.rejected)
    with KB(kb_path) as kb:
        assert kb.db.execute("SELECT summary FROM routine_notes WHERE entry=?", (0x8010,)).fetchone()[0] == "second"


def test_ram_only_proposals_fix_labels_without_re_annotating_a_routine(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    report, ok = run_apply(kb_path, tmp_path, [
        {"ram": {"$0300": {"name": "Cursor_Frame", "comment": "drawn by the NMI"}}},
        {"ram": {"$0301": {"name": "X"}}, "summary": "stray"}])
    assert ok and "ram comment $0300" in report.accepted
    assert any("only \"ram\" entries" in r for r in report.rejected)
    with KB(kb_path) as kb:
        assert kb.label_at(NON_ROM, 0x0300).name == "Cursor_Frame"
        assert kb.routine_notes(0) == []                       # no routine counted as re-annotated


def test_a_verified_ram_label_takes_a_new_size_and_comment_and_says_so(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    with KB(kb_path) as kb:
        kb.add_label(Label(NON_ROM, 0x0300, "Top_Score_Digits", "ram", "verified", "claude", 0.7, 5))
        kb.commit()
    report, _ = run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "x", "ram": {
        "$0300": {"name": "Top_Score_Digits", "size": 10, "comment": "two 5-digit scores"},
        "$0310": {"size": 4}}}])
    assert "size of verified label Top_Score_Digits: 5 -> 10 bytes" in report.accepted
    assert "ram comment $0300" in report.accepted
    assert any("$0310: a size needs the label's name" in r for r in report.rejected)
    with KB(kb_path) as kb:
        label = kb.label_at(NON_ROM, 0x0300)
        assert (label.size, label.status, label.source) == (10, "verified", "claude")


def make_kb(tmp_path, code, **vectors):
    rom = romlib.parse(make_nes(make_prg(code, **vectors)), "t.nes")
    path = tmp_path / "q.db"
    with KB(path) as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom))
    return path


def test_queue_prefers_substantial_routines_over_tiny_fragments(tmp_path):
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x20, 0x20, 0x80, 0x4C, 0x00, 0x80]),
            0x8010: bytes([0xA9, 0x01, 0x60]),                                  # tiny: lda / rts
            0x8020: bytes([0xA2, 0x00, 0xE8, 0xE0, 0x08, 0xD0, 0xFB, 0x60])}   # loop: ldx/inx/cpx/bne/rts
    with KB(make_kb(tmp_path, code, reset=0x8000)) as kb:
        order = annotate.queue(annotate.Context.load(kb))
    assert order.index(0x8020) < order.index(0x8010)


def test_work_unit_shows_fall_in_entries_and_jump_targets(tmp_path):
    code = {0x8000: bytes([0x20, 0x10, 0x80, 0x20, 0x12, 0x80, 0x20, 0x30, 0x80, 0x4C, 0x00, 0x80]),
            0x8010: bytes([0xA9, 0x05]),                            # lda #5, falls into $8012
            0x8012: bytes([0x85, 0x20, 0x4C, 0x33, 0x80]),          # sta $20 / jmp $8033
            0x8030: bytes([0xA9, 0x00, 0x60, 0xE6, 0x21, 0x60])}    # lda #0 / rts ; $8033: inc $21 / rts
    with KB(make_kb(tmp_path, code, reset=0x8000)) as kb:
        unit = annotate.work_unit(annotate.Context.load(kb), 0x8012)
    assert "### Alternate entries that fall into this routine" in unit and "lda #$05" in unit
    assert "### Code at jumps/branches leaving this routine" in unit and "inc $21" in unit


@needs_cc65
def test_sized_ram_label_renders_offsets_and_round_trips(tmp_path):
    # lda $0200,x / sta $0204,x / lda $0011 (abs, zp address) / jmp $8000
    code = {0x8000: bytes([0xBD, 0x00, 0x02, 0x9D, 0x04, 0x02, 0xAD, 0x11, 0x00, 0x4C, 0x00, 0x80])}
    kb_path = make_kb(tmp_path, code, reset=0x8000)
    report, ok = annotate.apply(kb_path, [{
        "routine": "$8000", "summary": "x",
        "ram": {"$0200": {"name": "Sprite_Buffer", "size": 256}, "$0010": {"name": "Pair", "size": 2}}}],
        tmp_path / "out")
    assert ok, report.rejected
    with KB(kb_path) as kb:
        asm = render(kb).asm
    assert "lda Sprite_Buffer,x" in asm and "sta Sprite_Buffer+4,x" in asm
    assert "lda a:Pair+1" in asm  # absolute mode kept for an offset into a zero-page variable


def test_ram_size_must_stay_in_ram(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify", lambda *a: type("R", (), {"identical": True})())
    report, _ = run_apply(kb_path, tmp_path, [{"routine": "$8010", "summary": "x",
                                                "ram": {"$07F0": {"name": "Too_Big", "size": 32}}}])
    assert any("stay in RAM" in r for r in report.rejected)


def test_old_databases_gain_the_label_size_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE labels (bank INTEGER NOT NULL, addr INTEGER NOT NULL, name TEXT NOT NULL UNIQUE, "
               "kind TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, confidence REAL, "
               "PRIMARY KEY (bank, addr))")
    db.execute("INSERT INTO labels VALUES (-1, 16, 'Old', 'ram', 'candidate', 'claude', NULL)")
    db.commit()
    db.close()
    with KB(path) as kb:
        assert kb.label_named("Old").size is None
        kb.add_label(Label(-1, 32, "New", "ram", "candidate", "claude", None, 4))
        assert kb.label_named("New").size == 4


def test_failed_verification_rolls_everything_back(kb_path, tmp_path, monkeypatch):
    monkeypatch.setattr(annotate, "build_and_verify",
                        lambda *a: type("R", (), {"identical": False, "first_diff": 0x20})())
    report, ok = run_apply(kb_path, tmp_path, [{"routine": "$8010", "name": "BumpCounter", "summary": "x"}])
    assert not ok
    with KB(kb_path) as kb:
        assert kb.label_named("BumpCounter") is None
        assert kb.routine_notes(0) == []
