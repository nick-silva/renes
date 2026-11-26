import json
import random

import pytest

from conftest import EXAMPLE_ROM, needs_cc65, needs_mesen
from renes.cli import main
from renes.explore import Archive, CellModel, Entry, _compact
from renes.kb import KB


def entry(cell: str, frame: int = 10, discovered: bool = False, code: int = 0, found_at: int = 0) -> Entry:
    return Entry(cell, frame, code, [[frame, "none"]], bytes(0x800), b"state", discovered, found_at)


def test_cell_model_picks_slow_bytes_not_constant_or_noisy_ones():
    cells = CellModel(lo=0.001, hi=0.2)
    rng = random.Random(0)
    prev = bytearray(0x800)
    for step in range(1000):
        cur = bytearray(prev)
        cur[0x10] = rng.randrange(256)       # noisy: changes every step
        if step % 100 == 0:
            cur[0x20] = (cur[0x20] + 1) % 8  # slow: level-like
        cells.observe(bytes(prev), bytes(cur))
        prev = cur
    assert cells.refit()
    assert cells.slow == (0x20,)
    assert cells.cell(bytes(prev)).startswith("020=")


def test_archive_keeps_shortest_path_per_cell_and_every_discovery():
    a = Archive()
    a.add(entry("x", frame=100))
    assert a.wants("x", 50, False) and not a.wants("x", 150, False)
    a.add(entry("x", frame=50))
    a.add(entry("x", frame=200, discovered=True, code=7))
    assert len(a) == 2 and a.entries["x"].frame == 50


def test_archive_evicts_most_visited_when_full():
    a = Archive(max_size=2)
    a.add(entry("a"))
    a.add(entry("b"))
    a.entries["a"].visits = 9
    a.add(entry("c"))
    assert set(a.entries) == {"b", "c"}


def test_rekey_merges_entries_that_now_share_a_cell():
    a = Archive()
    a.add(entry("old1", frame=30))
    a.add(entry("old2", frame=20))
    a.rekey(CellModel())  # no slow bytes yet: everything maps to "root"
    assert list(a.entries) == ["root"] and a.entries["root"].frame == 20


def test_compact_merges_repeated_buttons():
    assert _compact([[4, "a"], [8, "a"], [2, "b"]]) == [[12, "a"], [2, "b"]]


@needs_mesen
def test_savestate_bytes_round_trip_and_cdl_survives_load():
    from renes.emu.mesen import Mesen

    with Mesen(EXAMPLE_ROM) as m:
        m.step(30)
        state, ram = m.save_bytes(), m.ram()
        m.step(60, "start")
        m.step(200, "right")
        code_after, *_ = m.cdl_stats()
        m.load_bytes(state)
        assert m.ram() == ram
        code_reloaded, *_ = m.cdl_stats()
    assert code_reloaded == code_after  # coverage is debugger state, not rewound by loads


@needs_mesen
@needs_cc65
def test_short_exploration_records_coverage_and_proves_code(tmp_path):
    kb_path = tmp_path / "example.db"
    assert main(["init", str(EXAMPLE_ROM), "--kb", str(kb_path)]) == 0
    assert main(["explore", str(kb_path), "--frames", "3000", "--seed", "7"]) == 0
    with KB(kb_path) as kb:
        cdl = kb.coverage("nesPrgRom", "cdl")
        states = kb.db.execute("SELECT inputs, length(savestate) FROM states").fetchall()
        writes = kb.counters("nesInternalRam", "writeCount")
    assert sum(1 for f in cdl if f & 1) > 5000
    assert states and all(size > 1000 for _, size in states)
    assert json.loads(states[0][0])[0][0] >= 1
    assert sum(1 for w in writes if w) > 100
    assert main(["coverage", str(kb_path)]) == 0
    assert main(["analyze", str(kb_path)]) == 0
    with KB(kb_path) as kb:
        proven = kb.db.execute("SELECT count(*) FROM byte_class WHERE source='exec'").fetchone()[0]
    assert proven > 5000
    assert main(["build", str(kb_path), "--out", str(tmp_path / "out")]) == 0
    assert main(["coverage", str(kb_path), "--unclassified"]) == 0

    # a second run from an archived state: its states extend that state's inputs
    with KB(kb_path) as kb:
        sid, inputs = kb.db.execute("SELECT id, inputs FROM states ORDER BY frame DESC LIMIT 1").fetchone()
    assert main(["explore", str(kb_path), "--frames", "600", "--state", str(sid)]) == 0
    assert main(["explore", str(kb_path), "--frames", "300", "--setup", '[[60, "none"], [4, "start"]]']) == 0
    with KB(kb_path) as kb:
        runs = [r for (r,) in kb.db.execute("SELECT id FROM runs ORDER BY id")]
        from_state = [json.loads(i) for (i,) in kb.db.execute("SELECT inputs FROM states WHERE run_id=?", (runs[1],))]
        from_setup = [json.loads(i) for (i,) in kb.db.execute("SELECT inputs FROM states WHERE run_id=?", (runs[2],))]
    root = json.loads(inputs)
    assert from_state and all(i[:len(root) - 1] == root[:-1] for i in from_state)
    assert from_setup and all(i[1] == [4, "start"] for i in from_setup)
