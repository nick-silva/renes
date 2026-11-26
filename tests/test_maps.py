"""Dashboard maps: per-byte ROM/RAM state, subsystem clusters, per-round snapshots."""
import base64
import zlib

import pytest

from conftest import make_nes, make_prg
from renes import analyze, maps, rom as romlib
from renes.kb import KB, NON_ROM, Label

# Reset: lda $0010 / sta a:$0011 / jsr $8010 / jmp $8000 ; $8010: inc $0010 / rts
CODE = {0x8000: bytes([0xA5, 0x10, 0x8D, 0x11, 0x00, 0x20, 0x10, 0x80, 0x4C, 0x00, 0x80]),
        0x8010: bytes([0xE6, 0x10, 0x60])}


@pytest.fixture
def kb(tmp_path):
    rom = romlib.parse(make_nes(make_prg(CODE, reset=0x8000)), "t.nes")
    with KB(tmp_path / "t.db") as kb:
        kb.store_rom(rom)
        analyze.store(kb, analyze.analyze(rom))
        kb.add_label(Label(0, 0x8010, "BumpCounter", "routine", "candidate", "claude", 0.4), replace=True)
        kb.set_routine_note(0, 0x8010, "Adds one to Frame_Tally each call.", "candidate", "claude", 0.4, "t")
        kb.add_label(Label(NON_ROM, 0x10, "Frame_Tally", "ram", "verified", "experiment", 0.9))
        kb.commit()
        yield kb


def test_rom_map_marks_annotated_bytes_by_confidence(kb):
    m = maps.rom_map(kb)
    state = base64.b64decode(m["state"])
    assert len(state) == m["size"] and m["base"] == 0x8000
    assert maps.ROM_STATES[state[0x10]] == "annotated, low confidence"     # $8010, confidence 0.4
    assert maps.ROM_STATES[state[0x00]] == "code (static)"                 # $8000: no note, no CDL
    r = {x["entry"]: x for x in m["routines"]}
    assert r[0x8010]["annotated"] and not r[0x8000]["annotated"]


def test_ram_map_ignores_the_baseline_every_byte_gets(kb):
    reads = [4] * 0x800                                # a reset loop that reads every byte
    reads[0x20] = 9
    kb.merge_counters("nesInternalRam", "readCount", reads)
    kb.commit()
    state = base64.b64decode(maps.ram_map(kb)["state"])
    assert maps.RAM_STATES[state[0x20]] == "read, unlabeled"
    assert maps.RAM_STATES[state[0x21]] == "never read"
    assert maps.RAM_STATES[state[0x10]] == "verified"


def test_subsystems_cluster_by_calls_and_name_groups_by_annotated_words(kb):
    g = maps.subsystems(kb)
    assert {n["id"] for n in g["nodes"]} == {0x8000, 0x8010}
    assert {"source": 0x8000, "target": 0x8010} in g["edges"]
    [cluster] = g["clusters"]
    assert cluster["routines"] == 2 and cluster["annotated"] == 1 and "bump" in cluster["name"]


def test_snapshots_store_compressed_state(kb):
    maps.record_snapshot(kb, 7, "t")
    round_id, rom, ram = kb.db.execute("SELECT round_id, rom, ram FROM map_snapshots").fetchone()
    assert round_id == 7 and zlib.decompress(rom) == maps.rom_state(kb)[0]
    assert len(zlib.decompress(ram)) == 0x800
