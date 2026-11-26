"""Review evidence (M7): what each routine visibly does, and what it does line by line.

For every target routine:

  trace     one real call, recorded instruction by instruction with the registers before each
            line (A, X, Y, SP, flags), so the review page can show the code next to live values
  knock-out the same moment played twice from a savestate: normally, and with the routine's
            first byte patched to RTS. Screenshots of both, plus the labeled RAM that ends up
            different, show what the routine is responsible for. "Nothing changed" is evidence too

Both need a moment when the routine runs. A single pass over a playback (the in-sync part of a
TAS movie when one is configured, else a scripted run: start the game, walk and jump right)
records the frames each routine entry executes, keeping a savestate checkpoint every
CHECKPOINT frames to restart from.

Routines that never run during the playback get no evidence; the review page says so.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .emu.mesen import Mesen
from .kb import KB, NON_ROM

CHECKPOINT = 300
KNOCKOUT_FRAMES = 120
SCRIPTED = [(60, "none"), (4, "start"), (240, "none")] + [(24, "right+b"), (20, "right+a+b"), (8, "right")] * 40


@dataclass
class Playback:
    frames: list[str]            # buttons per frame
    resets: set[int]             # frames that start with a reset
    source: str


def scripted_playback() -> Playback:
    frames = [b for n, b in SCRIPTED for _ in range(n)]
    return Playback(frames, set(), "scripted: start, then run and jump right")


def movie_playback(path: Path, until: int | None, fixes: list[dict] | None = None) -> Playback:
    from . import tas

    movie = tas.load_movie(path)
    if fixes:
        movie = tas.apply_fixes(movie, fixes)
    end = min(len(movie.frames), until or len(movie.frames))
    return Playback(movie.frames[:end], {r for r in movie.resets if r < end}, f"TAS {path.parent.name} to frame {end}")


TAS_DIR = Path(__file__).resolve().parent.parent / "roms" / "tas"


def find_movie(path: Path, tas_dir: Path = TAS_DIR) -> Path | None:
    """A remembered movie that moved: `renes movies` keeps each in roms/tas/<id>M/, so look there by
    its folder (the TASVideos id) and by file name."""
    for candidate in (tas_dir / path.parent.name / path.name, *sorted(tas_dir.glob(f"*/{path.name}"))):
        if candidate.exists():
            return candidate
    return None


def playback_for(kb: KB, tas_dir: Path = TAS_DIR) -> Playback:
    raw = kb.setting("evidence_movie")
    if raw:
        cfg = json.loads(raw)
        path = Path(cfg["path"])
        if not path.exists():
            moved = find_movie(path, tas_dir)
            if moved is None:
                # scripted play would quietly cover less of the game: stop instead
                raise FileNotFoundError(f"evidence movie {path} is missing (not under {tas_dir} either); "
                                        f"pass --movie <file> to set it again")
            print(f"evidence movie found at its new place: {moved}")
            cfg["path"] = str(moved)
            kb.set_setting("evidence_movie", json.dumps(cfg))
            kb.commit()
            path = moved
        return movie_playback(path, cfg.get("until"), cfg.get("fixes"))
    return scripted_playback()


def _play(m: Mesen, pb: Playback, start: int, end: int) -> None:
    i = start
    while i < end:
        if i in pb.resets:
            m.reset()
        j = i + 1
        while j < end and pb.frames[j] == pb.frames[i] and j not in pb.resets:
            j += 1
        m.step(j - i, pb.frames[i])
        i = j


def unreached_entries(kb: KB) -> set[tuple[int, int]]:
    """(bank, entry) of routines the evidence playback never reaches (older KBs stored bare bank-0 entries)."""
    raw = json.loads(kb.setting("evidence_unreached") or "{}").get("entries", [])
    return {tuple(e) if isinstance(e, list) else (0, e) for e in raw}


@dataclass
class Collected:
    routines: int
    reached: int
    traced: int
    knocked: int


def collect(kb_path: Path, entries: list | None = None, frames: int = KNOCKOUT_FRAMES,
            log=print) -> Collected:
    """Record traces and knock-outs for `entries` (default: annotated routines without evidence).

    Entries are (bank, entry) pairs, or bare entries of bank 0. With several banks (AxROM) the
    routines are watched, traced and patched by PRG offset, so only the right bank's code counts."""
    with KB(kb_path) as kb:
        rom = kb.rom()
        pb = playback_for(kb)
        banks = {b.index: b for b in rom.banks()}
        multi = len(banks) > 1
        extents = {(b, e): last for b in banks for e, last, _ in kb.routines(b)}
        if entries is None:
            done = set(kb.db.execute("SELECT bank, entry FROM knockouts WHERE bank>=0"))
            entries = [(b, e) for b in banks for e, *_ in kb.routine_notes(b) if (b, e) not in done]
        entries = [e if isinstance(e, tuple) else (0, e) for e in entries]
        entries = [e for e in entries if e in extents]
        labels = {l.addr: l.name for l in kb.labels(NON_ROM) if l.addr < 0x800}
        # branch targets inside each routine: tracing when they first run shows the less common paths
        inner = {}
        for b in banks:
            for l in kb.labels(b):
                if l.kind == "code":
                    owner = next((e for e in entries if e[0] == b and e[1] < l.addr <= extents[e]), None)
                    if owner is not None:
                        inner.setdefault(owner, []).append((b, l.addr))

    def watched(be: tuple[int, int]) -> int:
        """The address the emulator watches: CPU address, or the PRG offset when banks share addresses."""
        b, addr = be
        return banks[b].offset + addr - banks[b].base if multi else addr
    memtype = "nesPrgRom" if multi else "nesMemory"
    log(f"evidence: {len(entries)} routines, playback = {pb.source} ({len(pb.frames)} frames)")
    rom_file = kb_path.with_suffix(".nes")
    rom_file.write_bytes(rom.raw)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    traced = knocked = 0
    try:
        with Mesen(rom_file) as m:
            # Pass 1: when does each routine first run? Keep checkpoints to restart from.
            with KB(kb_path) as kb:
                kb.set_progress("evidence", 0, None, f"playing back {pb.source} to find when routines run")
            m.watch_hits(sorted(watched(e) for e in extents), memtype=memtype)
            m.watch_hits(sorted(watched(a) for v in inner.values() for a in v), cap=1, memtype=memtype)
            # The bridge counts frames from emulator start, which is ahead of the playback index
            # (it takes commands only after the first frame): convert hits to playback indices.
            base = int(m.state()["frame"])
            checkpoints = [(0, m.save_bytes())]
            for start in range(0, len(pb.frames), CHECKPOINT):
                _play(m, pb, start, min(start + CHECKPOINT, len(pb.frames)))
                checkpoints.append((min(start + CHECKPOINT, len(pb.frames)), m.save_bytes()))
            by_watch = {watched(e): e for e in extents} | {watched(a): a for v in inner.values() for a in v}
            hits = {by_watch[a]: [max(f - base, 0) for f in fs] for a, fs in m.hit_frames().items() if a in by_watch}
            reached = [e for e in entries if e in hits]
            log(f"  {len(reached)} of {len(entries)} routines run during the playback")

            with KB(kb_path) as kb:
                # Routines this playback never reaches can't get evidence from it; the loop's
                # evidence backlog leaves them out (until the playback changes).
                prev = json.loads(kb.setting("evidence_unreached") or "{}")
                missed = unreached_entries(kb) if prev.get("source") == pb.source else set()
                missed = (missed | {e for e in entries if e not in hits}) - set(reached)
                kb.set_setting("evidence_unreached", json.dumps({"source": pb.source,
                                                                 "entries": [list(e) for e in sorted(missed)]}))
                kb.set_progress("evidence", 0, len(reached), "tracing and knocking out routines")
            for n_done, entry in enumerate(reached):
                if n_done:
                    with KB(kb_path) as kb:
                        kb.set_progress("evidence", n_done, len(reached), "tracing and knocking out routines")
                calls = hits[entry]
                # a busy routine is shown mid-gameplay (a quarter into its calls); a rare one at its first call
                hit = calls[len(calls) // 4] if len(calls) >= 8 else calls[0]
                cp_frame, cp_state = max((c for c in checkpoints if c[0] <= max(hit - 1, 0)), key=lambda c: c[0])
                # trace calls at several moments (incl. when each inner branch target first ran);
                # keep the call that covers the most lines
                finished, rows, trace_frame = False, [], hit
                moments = {calls[0], calls[len(calls) // 4]}
                moments |= {hits[a][0] for a in inner.get(entry, []) if a in hits and hits[a][0] >= calls[0]}
                for when in sorted(moments)[:8]:
                    t_frame, t_state = max((c for c in checkpoints if c[0] <= max(when - 1, 0)), key=lambda c: c[0])
                    m.load_bytes(t_state)
                    _play(m, pb, t_frame, when)            # up to the frame of interest...
                    m.trace(watched(entry), watched((entry[0], extents[entry])), memtype=memtype,
                            cpu_start=entry[1])            # ...then record the next call
                    _play(m, pb, when, min(when + 2, len(pb.frames)))
                    done_, rows_ = m.trace_log()
                    if len({r["pc"] for r in rows_}) > len({r["pc"] for r in rows}):
                        finished, rows, trace_frame = done_, rows_, when
                if rows:
                    traced += 1
                # knock-out: from just before the first call, normal vs patched to RTS
                m.load_bytes(cp_state)
                _play(m, pb, cp_frame, max(hit - 1, cp_frame))
                before = m.save_bytes()
                end = min(hit - 1 + frames, len(pb.frames)) if pb.frames else hit + frames
                shots = {}
                rams = {}
                bank, addr = entry
                offset = banks[bank].offset + addr - banks[bank].base if multi else (addr - 0x8000) % len(rom.prg)
                original = rom.prg[offset]
                for role in ("normal", "knocked"):
                    m.load_bytes(before)
                    if role == "knocked":
                        m.write(offset, bytes([0x60]), memtype="nesPrgRom")
                    mid = (max(hit - 1, cp_frame) + end) // 2
                    _play(m, pb, max(hit - 1, cp_frame), mid)
                    shots[role] = [(mid, m.screenshot_bytes())]
                    _play(m, pb, mid, end)
                    shots[role].append((end, m.screenshot_bytes()))
                    rams[role] = m.ram()
                    if role == "knocked":
                        m.write(offset, bytes([original]), memtype="nesPrgRom")
                effect = [{"addr": a, "name": labels[a], "normal": rams["normal"][a], "knocked": rams["knocked"][a]}
                          for a in sorted(labels) if rams["normal"][a] != rams["knocked"][a]]
                other = sum(1 for a in range(0x800) if a not in labels and rams["normal"][a] != rams["knocked"][a])
                differs = shots["normal"][-1][1] != shots["knocked"][-1][1]
                with KB(kb_path) as kb:
                    if rows:
                        kb.db.execute("INSERT OR REPLACE INTO traces (bank, entry, state_id, frame, rows, created) "
                                      "VALUES (?,?,?,?,?,?)", (bank, addr, None, trace_frame, json.dumps(
                                          {"finished": finished, "rows": rows}), created))
                    ko = kb.db.execute(
                        "INSERT INTO knockouts (bank, entry, state_id, frames, screen_differs, effect, created) "
                        "VALUES (?,?,?,?,?,?,?)", (bank, addr, None, end - hit + 1, int(differs),
                                                    json.dumps({"labeled": effect, "other_bytes": other}),
                                                    created)).lastrowid
                    for role, strip in shots.items():
                        for frame, png in strip:
                            kb.add_media("knockout", bank, addr, ko, role, frame, png, created)
                    kb.commit()
                knocked += 1
    finally:
        rom_file.unlink(missing_ok=True)
        with KB(kb_path) as kb:
            kb.set_progress("evidence", knocked, len(entries), f"{knocked} routines with evidence", finished=True)
    return Collected(len(entries), len(reached), traced, knocked)
