"""M5: A/B experiment checks, label promotion rules, and an end-to-end run in Mesen."""
import pytest

from conftest import EXAMPLE_ROM, EXAMPLE_SETUP, needs_mesen, rom_fact
from renes import experiment
from renes.experiment import CheckResult, Result, SpecError, Trace, evaluate, record
from renes.kb import KB, NON_ROM, Label
from renes.opcodes import OPCODES


def trace(series: dict[int, list[int]], screen: bytes = b"s") -> Trace:
    frames = len(next(iter(series.values())))
    ram = []
    for i in range(frames):
        frame = bytearray(16)
        for addr, values in series.items():
            frame[addr] = values[i] & 0xFF
        ram.append(bytes(frame))
    return Trace(ram, screen)


CONTROL = trace({1: [0, 1, 2, 3, 4], 2: [5, 5, 5, 5, 5], 3: [10, 10, 10, 10, 10], 4: [0, 0, 0, 0, 0]})
TREATED = trace({1: [0, 1, 2, 3, 4], 2: [5, 4, 3, 2, 1], 3: [10, 10, 10, 10, 30], 4: [0xF0, 0xF0, 0xF0, 0xF0, 0xF0]},
                screen=b"t")


@pytest.mark.parametrize("check, ok", [
    ({"check": "increments_by", "watch": "$01", "value": 1, "run": "control"}, True),
    ({"check": "increments_by", "watch": "$02", "value": -1}, True),          # 255 per step, mod 256
    ({"check": "increments_by", "watch": "$02", "value": 1}, False),
    ({"check": "constant", "watch": "$02", "run": "control"}, True),
    ({"check": "changes", "watch": "$02"}, True),
    ({"check": "same", "watch": "$01"}, True),
    ({"check": "differs", "watch": "$02"}, True),
    ({"check": "final_delta", "watch": "$03", "op": "==", "value": 20}, True),
    ({"check": "final_delta", "watch": "$04", "op": "==", "value": -16}, True),  # signed: $F0 - 0
    ({"check": "final_delta", "watch": "$03", "watch_hi": "$04", "op": ">", "value": 0x1000}, False),
    ({"check": "changed_bytes_ratio", "op": ">", "value": 1}, True),            # 3 bytes vs 1
    ({"check": "screen_differs"}, True),
    ({"check": "screen_same"}, False),
])
def test_checks(check, ok):
    assert evaluate(check, CONTROL, TREATED).passed is ok


def test_per_run_checks_start_after_the_poke():
    poked = trace({1: [0, 7, 7, 7, 7]})
    poked.after = 1
    assert evaluate({"check": "constant", "watch": "$01"}, CONTROL, poked).passed
    assert not evaluate({"check": "constant", "watch": "$01"}, CONTROL, trace({1: [0, 7, 7, 7, 7]})).passed


def test_to_frame_limits_checks_to_a_window():
    early, late = trace({1: [1, 2, 3, 4, 9, 9]}), trace({1: [1, 2, 3, 4, 5, 6]})   # diverge at frame 4
    assert not evaluate({"check": "same", "watch": "$01"}, late, early).passed
    r = evaluate({"check": "same", "watch": "$01", "to_frame": 3}, late, early)
    assert r.passed and "frames 0-3" in r.detail
    assert "diverge at frame 4" in evaluate({"check": "differs", "watch": "$01", "from_frame": 2}, late, early).detail
    assert evaluate({"check": "increments_by", "watch": "$01", "value": 1, "to_frame": 3}, CONTROL, early).passed


def test_changes_at_most_and_from_frame_tolerate_a_stray_write():
    blip = trace({1: [5, 5, 0, 5, 5, 5]})                 # the game reuses the slot once
    assert not evaluate({"check": "constant", "watch": "$01"}, CONTROL, blip).passed
    assert evaluate({"check": "changes_at_most", "watch": "$01", "value": 2}, CONTROL, blip).passed
    assert not evaluate({"check": "changes_at_most", "watch": "$01", "value": 1}, CONTROL, blip).passed
    assert evaluate({"check": "constant", "watch": "$01", "from_frame": 3}, CONTROL, blip).passed


def test_apu_checks_compare_register_writes_per_frame():
    quiet = Trace([b""] * 3, b"s", apu=[(), ((0x4015, 0x0F),), ((0x4000, 0x30),)])
    tune = Trace([b""] * 3, b"s", apu=[(), ((0x4015, 0x0F),), ((0x4000, 0xBF),)])
    r = evaluate({"check": "apu_differs"}, quiet, tune)
    assert r.passed and "frame 2: control $4000=30, treatment $4000=BF" in r.detail
    assert evaluate({"check": "apu_same", "to_frame": 1}, quiet, tune).passed
    with pytest.raises(SpecError):
        evaluate({"check": "apu_same"}, CONTROL, TREATED)


def test_vram_checks_compare_every_frame_not_just_the_last():
    still = [bytes(0x820)] * 3
    redraw = [bytes(0x820), bytes(5) + b"\x07" + bytes(0x81A), bytes(0x820)]   # drawn once, then cleared
    a, b = Trace([b""] * 3, b"s", vram=still), Trace([b""] * 3, b"s", vram=redraw)
    r = evaluate({"check": "vram_differs"}, a, b)
    assert r.passed and "frame 1 (PPU $2005: 00 vs 07)" in r.detail
    assert not evaluate({"check": "vram_same"}, a, b).passed
    with pytest.raises(SpecError):
        evaluate({"check": "vram_differs"}, CONTROL, TREATED)    # runs without VRAM recorded


def test_bad_specs_are_rejected():
    with pytest.raises(SpecError):
        evaluate({"check": "nonsense"}, CONTROL, TREATED)
    with pytest.raises(SpecError):
        evaluate({"check": "final_delta", "watch": "$03", "value": 1}, CONTROL, TREATED)  # no op
    with pytest.raises(SpecError):
        experiment.parse_inputs([[-1, "none"]])


@pytest.fixture
def kb(tmp_path):
    with KB(tmp_path / "e.db") as kb:
        kb.add_label(Label(NON_ROM, 0x10, "Cand", "ram", "candidate", "claude"))
        kb.add_label(Label(NON_ROM, 0x11, "Human", "ram", "approved", "human"))
        yield kb


def result(target: int, passed: bool) -> Result:
    return Result("t", target, passed, [CheckResult({"check": "constant"}, passed, "")], 10)


def test_passing_experiment_verifies_a_candidate(kb):
    r = result(0x10, True)
    record(kb, {"target": "$10", "alternative": "it is a timer, not a counter"}, r)
    assert r.promoted == "Cand" and kb.label_at(NON_ROM, 0x10).status == "verified"
    assert kb.experiments(NON_ROM, 0x10)[0][-1] == 1


def test_failing_or_diagnostic_experiments_do_not_promote(kb):
    record(kb, {"target": "$10"}, result(0x10, False))
    record(kb, {"target": "$10", "verifies": False}, result(0x10, True))
    assert kb.label_at(NON_ROM, 0x10).status == "candidate"
    assert len(kb.experiments(NON_ROM, 0x10)) == 2


def test_a_pass_without_a_competing_meaning_does_not_verify(kb):
    r = result(0x10, True)
    record(kb, {"target": "$10"}, r)
    assert r.promoted is None and "alternative" in r.note
    assert kb.label_at(NON_ROM, 0x10).status == "candidate"


def test_a_treatment_that_also_pokes_other_addresses_does_not_verify(kb):
    alt = {"target": "$10", "alternative": "it is a timer, not a counter"}
    r = result(0x10, True)
    record(kb, {**alt, "intervention": {"freeze": {"$10": 0}, "poke": {"$20": 2}}}, r)
    assert r.promoted is None and "$0020" in r.note and "shared" in r.note
    r = result(0x10, True)
    record(kb, {**alt, "intervention": {"freeze": {"$10": 0}}, "shared": {"poke": {"$20": 2}}}, r)
    assert r.promoted == "Cand"                       # setup in both runs is fine (feedback #36)


def test_an_invalidated_experiment_stops_counting_and_unverifies(kb):
    from renes.experiment import still_verified
    record(kb, {"target": "$10", "alternative": "a timer"}, result(0x10, True))
    [(eid, *_)] = kb.experiments(NON_ROM, 0x10)
    kb.invalidate_experiment(eid, "the control never jumped")
    assert kb.experiments(NON_ROM, 0x10) == [] and len(kb.experiments(include_invalid=True)) == 1
    assert not still_verified(kb, NON_ROM, 0x10)


def test_approved_labels_are_never_downgraded(kb):
    record(kb, {"target": "$11"}, result(0x11, True))
    assert kb.label_at(NON_ROM, 0x11).status == "approved"


@needs_mesen
def test_frame_counter_experiment_end_to_end(tmp_path):
    fc = rom_fact("frame_counter")      # a RAM byte the game adds 1 to every frame
    FC = f"${fc:04X}"
    from renes import rom as romlib

    kb_path = tmp_path / "example.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.load(EXAMPLE_ROM))
        kb.add_label(Label(NON_ROM, fc, "Frame_Counter", "ram", "candidate", "claude"))
    spec = {"name": "frame counter", "target": FC,
            "setup": EXAMPLE_SETUP, "inputs": [[30, "none"]],
            "intervention": {"freeze": {FC: 0x42}},
            "checks": [{"check": "increments_by", "watch": FC, "value": 1, "run": "control"},
                       {"check": "constant", "watch": FC}],
            "alternative": "a timer the game reloads: then freezing it would not hold it constant"}
    [r] = experiment.run_all(kb_path, [spec])
    assert r.passed and r.promoted == "Frame_Counter", [c.detail for c in r.checks]


@needs_mesen
def test_buttons_reach_the_game_on_each_controller():
    from renes.emu.mesen import Mesen

    m = Mesen(EXAMPLE_ROM)
    try:
        for frames, buttons in EXAMPLE_SETUP:       # from power-on into play
            m.step(frames, buttons)
        m.save("play")
        m.step(30, "right")
        p1 = m.ram()
        m.load("play")
        m.step(30, "none/right")                    # controller 2 only: player 1 doesn't walk
        p2 = m.ram()
        m.load("play")
        m.step(30, "none")
        assert p1 != m.ram() and p2 != p1
    finally:
        m.close()


@needs_mesen
def test_reset_steps_and_pokes_at_a_pc(tmp_path):
    fc = rom_fact("frame_counter")      # a RAM byte the game adds 1 to every frame
    FC = f"${fc:04X}"
    from renes import rom as romlib

    kb_path = tmp_path / "example.db"
    rom = romlib.load(EXAMPLE_ROM)
    with KB(kb_path) as kb:
        kb.store_rom(rom)
    nmi = rom.prg[0xFFFA - rom.prg_base] | rom.prg[0xFFFB - rom.prg_base] << 8
    setup = EXAMPLE_SETUP
    at_pc = {"name": "pc poke", "setup": setup, "inputs": [[30, "none"]],
             "intervention": {"poke_at_pc": {f"${nmi:04X}": {FC: 0x42}}},
             "checks": [{"check": "constant", "watch": FC, "from_frame": 1},   # after the first NMI
                        {"check": "changes", "watch": FC, "run": "control"}]}
    [r] = experiment.run_all(kb_path, [at_pc])
    assert r.passed, [c.detail for c in r.checks]

    with KB(kb_path) as kb:
        rom_file = tmp_path / "example.nes"
        rom_file.write_bytes(rom.raw)
        lab = experiment.Lab(kb, rom_file)
        try:
            lab.reach({"setup": setup})
            plain = lab.run([(20, "none")])
            reset = lab.run([(1, "reset"), (19, "none")])
        finally:
            lab.close()
    steps = lambda t: [(b[fc] - a[fc]) & 0xFF for a, b in zip(t.ram, t.ram[1:])]
    assert set(steps(plain)) == {1}                        # the frame counter counts every frame...
    assert set(steps(reset)) != {1}                        # ...and stalls and restarts after the reset


@needs_mesen
def test_shared_freeze_holds_in_both_runs_and_is_released_after(tmp_path):
    fc = rom_fact("frame_counter")      # a RAM byte the game adds 1 to every frame
    FC = f"${fc:04X}"
    SPARE = f"${rom_fact('spare_byte'):04X}"   # a RAM byte whose value changes nothing on screen
    from renes import rom as romlib

    kb_path = tmp_path / "example.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.load(EXAMPLE_ROM))
    setup = EXAMPLE_SETUP
    shared = {"name": "shared", "setup": setup, "inputs": [[30, "none"]],
              "shared": {"freeze": {FC: 0x42}}, "intervention": {"freeze": {FC: 0x10}},
              "checks": [{"check": "constant", "watch": FC, "run": "control"},
                         {"check": "constant", "watch": FC},
                         {"check": "final_delta", "watch": FC, "op": "==", "value": 0x10 - 0x42}]}
    after = {"name": "after", "setup": setup, "inputs": [[30, "none"]], "intervention": {"poke": {SPARE: 0}},
             "checks": [{"check": "increments_by", "watch": FC, "value": 1, "run": "control"}]}
    r1, r2 = experiment.run_all(kb_path, [shared, after])
    assert r1.passed, [c.detail for c in r1.checks]
    assert r2.passed, [c.detail for c in r2.checks]


@needs_mesen
def test_vram_and_writers_are_recorded_when_asked(tmp_path):
    fc = rom_fact("frame_counter")      # a RAM byte the game adds 1 to every frame
    FC = f"${fc:04X}"
    SPARE = f"${rom_fact('spare_byte'):04X}"   # a RAM byte whose value changes nothing on screen
    from renes import rom as romlib

    kb_path = tmp_path / "example.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.load(EXAMPLE_ROM))
    spec = {"name": "vram", "setup": EXAMPLE_SETUP, "inputs": [[20, "none"]],
            "intervention": {"poke": {SPARE: 0}}, "writers": [FC],
            "checks": [{"check": "vram_same"}, {"check": "increments_by", "watch": FC, "value": 1},
                       {"check": "apu_same"}]}
    [r] = experiment.run_all(kb_path, [spec])
    assert r.passed, [c.detail for c in r.checks]
    writes = int(r.checks[2].detail.split("(")[1].split()[0])
    assert writes >= 20                                     # the music and $4015 every frame
    [(addr, pc, n)] = r.writers["control"]                 # the frame counter has one writer, once a frame
    assert addr == fc and pc >= 0x8000 and n >= 19
    rom = romlib.load(EXAMPLE_ROM)
    assert OPCODES[rom.prg[pc - rom.prg_base]].mnemonic == "inc"   # the instruction, not the one after
