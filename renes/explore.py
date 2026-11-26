"""Stage A explorer: Go-Explore-style input fuzzing over a savestate archive.

Nothing here knows the game. Progress is measured by the emulator's code/data log (CDL):
a step that executes PRG bytes never executed before is a *discovery*, and the state it
reached joins the archive as a frontier entry. States are also grouped into *cells* keyed on
RAM bytes that change rarely but do change (mode, level, area, lives...), learned online.
"""
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .emu.mesen import Mesen
from .kb import KB, LIVE_STATES

# (buttons, weight). Start/select are rare but kept: pause menus and title screens are code too.
ACTIONS: list[tuple[str, float]] = [
    ("none", 1.0), ("right", 3.0), ("left", 1.5), ("up", 1.0), ("down", 1.0), ("a", 1.5), ("b", 1.0),
    ("right+a", 3.0), ("right+b", 2.0), ("right+a+b", 3.0), ("left+a", 1.5), ("left+b", 1.0),
    ("up+a", 0.5), ("down+a", 0.5), ("start", 0.3), ("select", 0.2),
]
DURATIONS = (4, 8, 16, 32, 64)
STICKY = 0.5          # chance of repeating the previous buttons
EPISODE = (10, 40)    # actions per episode from a selected archive entry
ARCHIVE_MAX = 1000
REFIT_EVERY = 300     # actions between cell-model refits
FRONTIER_WINDOW = 2000  # actions during which a discovery keeps a selection bonus


class CellModel:
    """Learns which RAM bytes are 'slow' (rarely changing state) and keys cells on their values."""

    def __init__(self, size: int = 0x800, max_bytes: int = 12, lo: float = 0.0005, hi: float = 0.05):
        self.changes = [0] * size
        self.values: list[set[int]] = [set() for _ in range(size)]
        self.observations = 0
        self.max_bytes, self.lo, self.hi = max_bytes, lo, hi
        self.slow: tuple[int, ...] = ()

    def observe(self, prev: bytes, cur: bytes) -> None:
        self.observations += 1
        for i, (a, b) in enumerate(zip(prev, cur)):
            if a != b:
                self.changes[i] += 1
                if len(self.values[i]) <= 64:
                    self.values[i].update((a, b))

    def refit(self) -> bool:
        n = max(1, self.observations)
        candidates = sorted(
            (self.changes[i] / n, i) for i in range(len(self.changes))
            if self.lo < self.changes[i] / n < self.hi and 2 <= len(self.values[i]) <= 32)
        slow = tuple(sorted(i for _, i in candidates[:self.max_bytes]))
        changed, self.slow = slow != self.slow, slow
        return changed

    def cell(self, ram: bytes) -> str:
        return ",".join(f"{i:03x}={ram[i]:02x}" for i in self.slow) or "root"


@dataclass
class Entry:
    cell: str
    frame: int
    new_code: int          # global CDL code-byte count when this state was reached
    inputs: list[list]     # [[frames, buttons], ...] from power-on
    ram: bytes
    state: bytes
    discovered: bool       # reached by a step that executed new code
    found_at: int          # action counter at discovery
    visits: int = 0

    @property
    def key(self) -> str:
        return f"{self.cell}#c{self.new_code}" if self.discovered else self.cell


class Archive:
    def __init__(self, max_size: int = ARCHIVE_MAX):
        self.entries: dict[str, Entry] = {}
        self.max_size = max_size

    def __len__(self) -> int:
        return len(self.entries)

    def wants(self, cell: str, frame: int, discovered: bool) -> bool:
        if discovered:
            return True
        existing = self.entries.get(cell)
        return existing is None or frame < existing.frame

    def add(self, entry: Entry) -> None:
        old = self.entries.get(entry.key)
        if old is not None:
            entry.visits = old.visits
        self.entries[entry.key] = entry
        while len(self.entries) > self.max_size:
            worst = max(self.entries.values(), key=lambda e: (e.visits, -e.found_at))
            del self.entries[worst.key]

    def select(self, rng: random.Random, now: int) -> Entry:
        entries = list(self.entries.values())
        weights = [(3.0 if e.discovered and now - e.found_at < FRONTIER_WINDOW else 1.0) / math.sqrt(1 + e.visits)
                   for e in entries]
        return rng.choices(entries, weights)[0]

    def rekey(self, cells: CellModel) -> None:
        old, self.entries = self.entries, {}
        for e in sorted(old.values(), key=lambda e: e.frame):  # shortest trajectory wins a cell
            e.cell = cells.cell(e.ram)
            if e.key not in self.entries:
                self.entries[e.key] = e


def _compact(inputs: list[list]) -> list[list]:
    out: list[list] = []
    for n, b in inputs:
        if out and out[-1][1] == b:
            out[-1][0] += n
        else:
            out.append([n, b])
    return out


def run_metrics(cdl: bytes, baseline: bytes) -> tuple[int, int]:
    """(code bytes this run executed, of which no earlier run had executed)."""
    run_code = sum(1 for f in cdl if f & 0x01)
    new_code = sum(1 for i, f in enumerate(cdl) if f & 0x01 and not (i < len(baseline) and baseline[i]))
    return run_code, new_code


@dataclass
class RunResult:
    run_id: int
    frames: int
    actions: int
    seconds: float
    code_bytes: int
    data_bytes: int
    sub_entries: int
    archive_size: int


def explore(kb_path: Path, minutes: float, seed: int, max_frames: int | None = None,
            log=print, state: int | None = None, setup: list | None = None) -> RunResult:
    """Fuzz from power-on, or from an archived `state` or after `setup` inputs ([[frames, buttons], ...]
    played from power-on) to explore a part of the game random play rarely reaches."""
    rng = random.Random(seed)
    with KB(kb_path) as kb:
        rom = kb.rom()
        start = None
        if state is not None:
            start = kb.db.execute(f"SELECT savestate, inputs, frame, {LIVE_STATES} FROM states WHERE id=?",
                                  (state,)).fetchone()
            if start is None or not start[3]:
                raise SystemExit(f"explore: no archived state {state}" if start is None else
                                 f"explore: state {state} is from a retired run")
        baseline = kb.code_mask()
        run_id = kb.add_run(datetime.now(timezone.utc).isoformat(timespec="seconds"), seed)
        kb.commit()
    rom_file = kb_path.with_suffix(".nes")
    rom_file.write_bytes(rom.raw)

    cells, archive = CellModel(), Archive()
    actions = frames_total = 0
    started = last_log = last_prog = time.perf_counter()
    deadline = started + minutes * 60

    with Mesen(rom_file) as m:
        m.step(1)
        root_inputs = [[1, "none"]]
        if start is not None:                    # the state's own inputs keep every entry replayable
            m.load_bytes(bytes(start[0]))
            root_inputs = json.loads(start[1])
        for n, buttons in setup or []:
            if buttons == "reset":
                raise SystemExit("explore: a setup can't press reset (archived inputs must replay)")
            m.step(int(n), buttons)
            root_inputs.append([int(n), buttons])
        code, data, _, _ = m.cdl_stats()
        best = code
        archive.add(Entry("root", sum(n for n, _ in root_inputs), code, _compact(root_inputs), m.ram(),
                          m.save_bytes(), True, 0))

        try:
            while time.perf_counter() < deadline and (max_frames is None or frames_total < max_frames):
                entry = archive.select(rng, actions)
                entry.visits += 1
                m.load_bytes(entry.state)
                frame, inputs, prev = entry.frame, list(entry.inputs), entry.ram
                buttons = "none"
                for _ in range(rng.randint(*EPISODE)):
                    if rng.random() >= STICKY:
                        buttons = rng.choices([a for a, _ in ACTIONS], [w for _, w in ACTIONS])[0]
                    n = rng.choice(DURATIONS)
                    m.step(n, buttons)
                    frame += n
                    frames_total += n
                    actions += 1
                    inputs.append([n, buttons])

                    code, data, _, _ = m.cdl_stats()
                    ram = m.ram()
                    cells.observe(prev, ram)
                    prev = ram
                    discovered = code > best
                    best = max(best, code)
                    cell = cells.cell(ram)
                    if archive.wants(cell, frame, discovered):
                        archive.add(Entry(cell, frame, code, _compact(inputs), ram, m.save_bytes(),
                                          discovered, actions))
                    if actions % REFIT_EVERY == 0 and cells.refit():
                        archive.rekey(cells)

                now = time.perf_counter()
                if now - last_prog >= 5:
                    last_prog = now
                    with KB(kb_path) as kb:
                        kb.set_progress("explore", int(now - started), int(minutes * 60), f"code {best}, archive {len(archive)}")
                if now - last_log >= 30:
                    last_log = now
                    log(f"  {now - started:6.0f}s  frames={frames_total:>8}  "
                        f"fps={frames_total / (now - started):5.0f}  code={best:>5} "
                        f"({best / len(rom.prg):.1%})  archive={len(archive)}  slow-bytes={len(cells.slow)}")
        finally:
            seconds = time.perf_counter() - started
            cdl = m.cdl()
            counts = {(mt, kind): m.counters(mt, kind)
                      for mt, kind in (("nesPrgRom", "execCount"), ("nesPrgRom", "readCount"),
                                       ("nesInternalRam", "readCount"), ("nesInternalRam", "writeCount"))}
            with KB(kb_path) as kb:
                merged = kb.merge_cdl("nesPrgRom", cdl)
                for (mt, kind), values in counts.items():
                    kb.merge_counters(mt, kind, values)
                kb.replace_states(run_id, [
                    (e.cell, e.frame, e.new_code, e.visits, json.dumps(e.inputs, separators=(",", ":")),
                     e.ram, e.state) for e in archive.entries.values()])
                run_code, new_code = run_metrics(cdl, baseline)
                code = sum(1 for f in merged if f & 0x01)
                data = sum(1 for f in merged if f & 0x02)
                subs = sum(1 for f in merged if f & 0x08)
                kb.set_progress("explore", int(seconds), int(minutes * 60), f"code {code}", finished=True)
                kb.finish_run(run_id, frames=frames_total, actions=actions, wall_seconds=round(seconds, 1),
                              code_bytes=code, data_bytes=data, archive_size=len(archive),
                              kind="fuzz", run_code=run_code, new_code=new_code,
                              notes=json.dumps({"start_state": state, "setup": setup}) if state or setup else None)
    rom_file.unlink(missing_ok=True)
    return RunResult(run_id, frames_total, actions, seconds, code, data, subs, len(archive))
