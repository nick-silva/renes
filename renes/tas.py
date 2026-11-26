"""TAS replay: play a tool-assisted speedrun movie in Mesen as an exploration source.

A TAS (tasvideos.org) is a per-frame input log that plays a whole game in minutes. Replaying it
records CDL coverage for every level the run visits and archives a savestate every N frames, so
experiments and the Stage B agent can start anywhere in the game.

Movies are made in other emulators (FCEUX .fm2, BizHawk .bk2), so a replay can *desync*: one
lag frame counted differently and a frame-precise trick fails. The replay therefore writes a
screenshot every few thousand frames for a human (or Claude) to check, `--until` stops before a
known desync, and `--fix` applies resync edits (insert/delete frames at given positions), the
usual way TASers port movies between emulators.

Movie input files are inputs only (no ROM bytes), but they are third-party works: keep them in
roms/tas/ (git-ignored) with their publication id, and credit the authors.
"""
import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .emu.mesen import Mesen
from .explore import run_metrics
from .kb import KB, NON_ROM

FM2_ORDER = ["right", "left", "down", "up", "start", "select", "b", "a"]   # RLDUTSBA
BK2_NAMES = {"Up": "up", "Down": "down", "Left": "left", "Right": "right", "Start": "start",
             "Select": "select", "B": "b", "A": "a"}
SAMPLE_EVERY = 30      # frames between RAM samples for the rare-change analysis


class MovieError(ValueError):
    pass


@dataclass
class Movie:
    header: dict[str, str]
    frames: list[str]                  # buttons per frame: "none", "right+b", or "right+b/a" (controller 2)
    resets: set[int] = field(default_factory=set)   # frames that start with a reset

    @property
    def rom_md5(self) -> bytes | None:
        value = self.header.get("romChecksum", "")
        return base64.b64decode(value.split(":", 1)[1]) if value.startswith("base64:") else None


def _buttons(p1: str, p2: str) -> str:
    """One frame of input for the bridge: "a+right", or "a+right/b" when controller 2 presses B."""
    return (p1 or "none") + (f"/{p2}" if p2 else "")


def parse_fm2(text: str) -> Movie:
    header, frames, resets = {}, [], set()
    for line in text.splitlines():
        if line.startswith("|"):
            fields = line.split("|")
            if len(fields) < 3:
                raise MovieError(f"bad input line {line!r}")
            command = int(fields[1] or 0)
            if command & 0x03:          # 1 = soft reset, 2 = power cycle: both become a reset here
                resets.add(len(frames))
            pads = ["+".join(n for n, c in zip(FM2_ORDER, f.ljust(8, ".")) if c not in ". ")
                    for f in fields[2:4]]
            frames.append(_buttons(pads[0], pads[1] if len(pads) > 1 else ""))
        elif line.strip():
            key, _, value = line.partition(" ")
            header[key] = value.strip()
    if header.get("binary", "0") != "0":
        raise MovieError("binary fm2 movies are not supported")
    if header.get("palFlag", "0") != "0":
        raise MovieError("PAL movies are not supported")
    if not frames:
        raise MovieError("no input frames")
    return Movie(header, frames, resets)


def parse_bk2(data: bytes) -> Movie:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        log = z.read("Input Log.txt").decode("utf-8", "replace")
        header = {}
        if "Header.txt" in z.namelist():
            for line in z.read("Header.txt").decode("utf-8", "replace").splitlines():
                key, _, value = line.partition(" ")
                header[key] = value.strip()
    keys: list[str] = []
    frames, resets = [], set()
    for line in log.splitlines():
        if line.startswith("LogKey:"):
            keys = [k.lstrip("#") for k in line[len("LogKey:"):].split("|") if k]
        elif line.startswith("|"):
            if not keys:
                raise MovieError("bk2 input log has no LogKey")
            chars = line.replace("|", "")
            pressed = {k for k, c in zip(keys, chars) if c not in ". "}
            if pressed & {"Reset", "Power"}:
                resets.add(len(frames))
            pads = ["+".join(BK2_NAMES[k[3:]] for k in keys if k in pressed and k.startswith(port) and k[3:] in BK2_NAMES)
                    for port in ("P1 ", "P2 ")]
            frames.append(_buttons(*pads))
    if not frames:
        raise MovieError("no input frames")
    return Movie(header, frames, resets)


def load_movie(path: Path) -> Movie:
    data = path.read_bytes()
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
        if "Input Log.txt" in names:
            return parse_bk2(data)
        # a TASVideos download wraps the movie file (a .bk2 is itself a zip)
        movies = [n for n in names if n.lower().endswith((".fm2", ".bk2"))]
        if len(movies) != 1:
            raise MovieError(f"{path.name}: expected one .fm2 or .bk2 inside the zip, found {names}")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            inner = z.read(movies[0])
        if movies[0].lower().endswith(".bk2"):
            return parse_bk2(inner)
        return parse_fm2(inner.decode("utf-8", "replace"))
    return parse_fm2(data.decode("utf-8", "replace"))


def apply_fixes(movie: Movie, fixes: list[dict]) -> Movie:
    """Resync edits, applied from the last frame backwards so positions refer to the original movie.

    [{"frame": 17950, "insert": 1}] inserts one empty frame before original frame 17950;
    [{"frame": 17950, "delete": 2}] drops original frames 17950-17951.
    """
    frames, resets = list(movie.frames), set(movie.resets)
    for fix in sorted(fixes, key=lambda f: -int(f["frame"])):
        at = int(fix["frame"])
        if not 0 <= at <= len(frames):
            raise MovieError(f"fix frame {at} outside the movie")
        n = int(fix.get("insert", 0)) or -int(fix.get("delete", 0))
        if n > 0:
            frames[at:at] = [fix.get("buttons", "none")] * n
        elif n < 0:
            del frames[at:at - n]
        resets = {r + n if r >= at else r for r in resets if not (n < 0 and at <= r < at - n)}
    return Movie(movie.header, frames, resets)


@dataclass
class TasResult:
    run_id: int
    frames: int
    seconds: float
    run_code: int
    new_code: int
    code_bytes: int
    states: int
    shots: list[Path]


def replay(kb_path: Path, movie: Movie, name: str, until: int | None = None, every: int = 600,
           shots_every: int = 3000, shots_dir: Path | None = None, force: bool = False,
           note: dict | None = None, log=print) -> tuple[TasResult, list[tuple[int, bytes]]]:
    """Play the movie; archive a state every `every` frames. Returns the result and RAM samples."""
    import time

    with KB(kb_path) as kb:
        rom = kb.rom()
        md5 = hashlib.md5(rom.raw[16:]).digest()
        if movie.rom_md5 is not None and movie.rom_md5 != md5 and not force:
            raise MovieError("the movie was made for a different ROM (checksum mismatch); use --force to try anyway")
        baseline = kb.code_mask()
        run_id = kb.add_run(datetime.now(timezone.utc).isoformat(timespec="seconds"), 0)
        kb.commit()
    rom_file = kb_path.with_suffix(".nes")
    rom_file.write_bytes(rom.raw)
    end = min(len(movie.frames), until or len(movie.frames))
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)
    states, samples, shots = [], [], []
    started = last_log = last_prog = time.perf_counter()
    with Mesen(rom_file) as m:
        i = 0
        while i < end:
            if i in movie.resets:
                m.reset()
            j = i + 1      # run identical inputs together, but stop at every frame of interest
            while j < end and movie.frames[j] == movie.frames[i] and j not in movie.resets \
                    and j % SAMPLE_EVERY and j % every and not (shots_dir and j % shots_every == 0):
                j += 1
            m.step(j - i, movie.frames[i])
            i = j
            if i % SAMPLE_EVERY == 0 or i == end:
                ram = m.ram()
                samples.append((i, ram))
                if i % every == 0 or i == end:
                    states.append((f"tas:{name}@{i}", i, 0, 0, json.dumps([[0, f"tas {name}"], [i, "movie"]]),
                                   ram, m.save_bytes()))
            if shots_dir and (i % shots_every == 0 or i == end):
                path = shots_dir / f"f{i:05d}.png"
                m.screenshot(path)
                shots.append(path)
            now = time.perf_counter()
            if now - last_prog >= 5:
                last_prog = now
                with KB(kb_path) as kb:
                    kb.set_progress(f"tas {name}", i, end, "replaying the movie")
            if now - last_log >= 30:
                last_log = now
                log(f"  frame {i}/{end} ({i / max(now - started, 1e-9):.0f} fps)")
        seconds = time.perf_counter() - started
        cdl = m.cdl()
        counts = {(mt, kind): m.counters(mt, kind)
                  for mt, kind in (("nesPrgRom", "execCount"), ("nesPrgRom", "readCount"),
                                   ("nesInternalRam", "readCount"), ("nesInternalRam", "writeCount"))}
    rom_file.unlink(missing_ok=True)
    run_code, new_code = run_metrics(cdl, baseline)
    with KB(kb_path) as kb:
        merged = kb.merge_cdl("nesPrgRom", cdl)
        for (mt, kind), values in counts.items():
            kb.merge_counters(mt, kind, values)
        kb.replace_states(run_id, states)
        total = sum(1 for f in merged if f & 0x01)
        notes = {"movie": {k: movie.header.get(k) for k in ("romFilename", "comment", "rerecordCount", "emuVersion")},
                 "name": name, "frames": end, "of": len(movie.frames), **(note or {})}
        kb.set_progress(f"tas {name}", end, end, f"{run_code} code bytes ({new_code} new)", finished=True)
        kb.finish_run(run_id, frames=end, actions=0, wall_seconds=round(seconds, 1), code_bytes=total,
                      data_bytes=sum(1 for f in merged if f & 0x02), archive_size=len(states),
                      kind=f"tas:{name}", run_code=run_code, new_code=new_code, notes=json.dumps(notes))
    return TasResult(run_id, end, seconds, run_code, new_code, total, len(states), shots), samples


def rare_changes(samples: list[tuple[int, bytes]], kb: KB, max_changes: int = 48, top: int = 40) -> str:
    """RAM bytes that change only a few times across the whole movie: level, world, lives, mode...

    Each line shows the value timeline (frame:value). Bytes whose changes line up with each other
    probably belong to the same event (a level transition), which makes them experiment targets.
    """
    names = {l.addr: f"{l.name} [{l.status}]" for l in kb.labels(NON_ROM)}
    rows = []
    size = len(samples[0][1]) if samples else 0
    for addr in range(size):
        timeline, prev = [], None
        for frame, ram in samples:
            if ram[addr] != prev:
                timeline.append((frame, ram[addr]))
                prev = ram[addr]
        changes = len(timeline) - 1
        if 2 <= changes <= max_changes and len({v for _, v in timeline}) >= 2:
            rows.append((changes, addr, timeline))
    rows.sort(key=lambda r: (r[0], r[1]))
    # A run that never dies or restarts moves level/world/area counters only upward, in steps.
    # The first value is skipped: RAM holds power-on garbage until the game clears it.
    rising = [r for r in rows if len(r[2]) >= 3 and all(b[1] > a[1] for a, b in zip(r[2][1:], r[2][2:]))]
    others = [r for r in rows if r not in rising]
    out = [f"Rare-change RAM: {len(rows)} bytes changed 2-{max_changes} times over {samples[-1][0] if samples else 0} "
           f"frames (sampled every {SAMPLE_EVERY}).", ""]

    def show(section: list) -> None:
        for changes, addr, timeline in section[:top]:
            shown = " ".join(f"{f}:{v:02X}" for f, v in timeline[:14]) + (" ..." if len(timeline) > 14 else "")
            out.append(f"${addr:04X} {changes:>3} changes  {names.get(addr, '-')}")
            out.append(f"      {shown}")

    out += [f"## Rising in steps ({len(rising)}): only ever increase after boot. Level/world/area/progress "
            "candidates if the run is in sync", ""]
    show(rising)
    out += ["", f"## Other rare changes ({len(others)}), fewest changes first", ""]
    show(others)
    return "\n".join(out) + "\n"
