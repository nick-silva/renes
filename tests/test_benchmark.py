"""M8: reference parsing against the ROM, name similarity, and aggregate-only scoring."""
import pytest

from conftest import make_nes, make_prg
from renes import rom as romlib
from renes.benchmark import ReferenceError, format_results, parse_reference, score, similarity, tokens
from renes.kb import KB, NON_ROM, Label

# $8000: lda #$01 / sta $0300 / rts ; $8006: .db 1,2 ; $8008: .dw $8000
PRG = make_prg({0x8000: bytes([0xA9, 0x01, 0x8D, 0x00, 0x03, 0x60, 0x01, 0x02, 0x00, 0x80])}, reset=0x8000)
SOURCE = """
PlayerSpeed = $57
FrameTimer  = %00001001
Derived     = PlayerSpeed+1
HwReg       = $2000
       .index 8
       .org $8000
StartGame:  lda #FrameTimer  ; comment (an immediate: FrameTimer is a constant, not RAM)
            sta PlayerSpeed
Exit:       rts
SpeedTable: .db 1, 2
PtrTable:
            .dw StartGame
"""


def test_reference_is_walked_alongside_the_rom():
    ref = parse_reference(SOURCE, PRG, 0x8000)
    assert ref.instructions == ref.aligned == 3
    assert ref.code == set(range(0x8000, 0x8006)) and ref.data == set(range(0x8006, 0x800A))
    assert ref.names == {0x8000: ["StartGame"], 0x8005: ["Exit"], 0x8006: ["SpeedTable"], 0x8008: ["PtrTable"]}
    assert ref.ram == {0x57: ["PlayerSpeed"]}         # no hardware, no expressions, no constants
    assert ref.constants == 1


def test_a_reference_for_another_rom_is_rejected():
    with pytest.raises(ReferenceError):
        parse_reference(SOURCE.replace("rts", "rti"), PRG, 0x8000)


def test_tokens_and_similarity():
    assert tokens("Player_X_Pos") == tokens("PlayerXPosition") == {"player", "x", "position"}
    assert similarity("Enemy_Y_Speed", "EnemyYSpeed") == 1.0
    assert similarity("Frame_Counter", "TimerControl") == 0.0
    assert 0 < similarity("Player_Speed", "PlayerXSpeed") < 1


def test_scores_are_aggregates_without_reference_names(tmp_path):
    with KB(tmp_path / "b.db") as kb:
        kb.store_rom(romlib.parse(make_nes(PRG), "t.nes"))
        kb.set_classes([(0, a, "op" if a in (0x8000, 0x8002, 0x8005) else "operand", "exec")
                        for a in range(0x8000, 0x8006)])
        kb.add_label(Label(0, 0x8000, "BeginPlay", "routine", "candidate", "claude", 0.8))
        kb.add_label(Label(NON_ROM, 0x57, "Player_Speed", "ram", "verified", "claude", 0.9))
        kb.add_label(Label(NON_ROM, 0x09, "Frame_Timer", "ram", "candidate", "claude", 0.4))
        kb.add_label(Label(0, 0x8005, "ReturnFromThing", "code", "candidate", "claude", 0.5))
        results, rows = score(kb, parse_reference(SOURCE, PRG, 0x8000))
    assert results["reference"]["shared_exits_skipped"] == 1        # names on a lone RTS aren't paired
    assert not any(r[3] == "ReturnFromThing" for r in rows)
    c = results["classification"]
    assert c["agreement_on_classified"] == 1.0 and c["code_recall"] == 1.0
    assert results["naming"]["by_kind"]["ram"]["match_rate"] == 1.0
    assert results["naming"]["by_kind"]["routine"]["match_rate"] == 0.0
    text = format_results(results, previous=results)
    for secret in ("StartGame", "SpeedTable", "PtrTable", "PlayerSpeed", "FrameTimer"):
        assert secret not in text and secret not in str(results)
    assert {r[4] for r in rows} >= {"StartGame", "PlayerSpeed"}   # pairs exist only for the details file


def test_cycle_decides_the_next_stage():
    from renes.cycle import Status, decide

    def st(**kw):
        base = dict(annotated=50, routines=400, ready=10, since_experiment=5, since_benchmark=5,
                    untested=[("X", 1, 9)] * 6, unreached_routines=30)
        return Status(**{**base, **kw})

    assert decide(st(since_benchmark=None), True)[0] == "benchmark"
    assert decide(st(since_benchmark=None), False)[0] == "annotate"       # no reference configured
    assert decide(st(since_benchmark=45), True)[0] == "benchmark"
    assert decide(st(since_experiment=30), True)[0] == "experiment"
    assert decide(st(since_experiment=30, untested=[]), True)[0] == "annotate"
    assert decide(st(ready=0), True)[0] == "explore"
    assert decide(st(ready=0, unreached_routines=0), True)[0] == "review"
    assert decide(st(researched=False), True)[0] == "research"
    assert decide(st(brief_pending=3), True)[0] == "review"
    assert decide(st(without_evidence=25), True)[0] == "evidence"


def test_cycle_stops_when_rounds_make_no_progress():
    from renes.cycle import Status, decide

    s = Status(annotated=50, routines=400, ready=0, since_experiment=5, since_benchmark=5, unreached_routines=30)
    assert decide(s, False)[0] == "explore"
    s.idle_rounds = ["explore"]
    assert decide(s, False)[0] == "explore"                  # one empty round: try again
    s.idle_rounds = ["explore", "explore"]
    stage, reason = decide(s, False)
    assert stage == "review" and "no progress" in reason
    s.ready, s.idle_rounds = 5, ["explore", "explore"]        # a different stage is due: carry on
    assert decide(s, False)[0] == "annotate"
    s.idle_rounds = ["annotate", "explore", "evidence"]
    assert decide(s, False)[0] == "review"


def test_semantic_grading_round_trip_stores_aggregates_only(tmp_path):
    import json
    from renes import grade

    (tmp_path / "ref.asm").write_text(SOURCE, encoding="latin-1")
    kb_path = tmp_path / "b.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.parse(make_nes(PRG), "t.nes"))
        kb.set_classes([(0, a, "op" if a in (0x8000, 0x8002, 0x8005) else "operand", "exec")
                        for a in range(0x8000, 0x8006)])
        kb.add_label(Label(0, 0x8000, "BeginPlay", "routine", "candidate", "claude", 0.8))
        kb.add_label(Label(NON_ROM, 0x57, "Player_Speed", "ram", "verified", "claude", 0.9))
        kb.set_setting("reference", str(tmp_path / "ref.asm"))
        kb.commit()
    out = tmp_path / "grading"
    assert grade.prepare(kb_path, out, sample=10)["pairs"] == 2
    pairs = json.loads((out / "packet.json").read_text())["pairs"]
    assert {p["ours"] for p in pairs} == {"BeginPlay", "Player_Speed"} and all(p["reference_source"] for p in pairs)
    verdict = {p["id"]: ("same" if p["kind"] == "ram" else "entity") for p in pairs}
    (out / "verdicts.json").write_text(json.dumps([{"id": i, "verdict": v} for i, v in verdict.items()]))
    (out / "verdicts_b.json").write_text(json.dumps([{"id": i, "verdict": "same"} for i in verdict]))
    results = grade.record(kb_path, out)
    assert results["all"]["same_rate"] == 0.5 and results["all"]["right_behaviour_rate"] == 1.0
    assert results["agreement"]["b"]["rate"] == 0.5
    with KB(kb_path) as kb:
        stored = json.dumps(grade.latest(kb))
    assert "BeginPlay" not in stored and "StartGame" not in stored          # aggregates only
