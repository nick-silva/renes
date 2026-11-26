"""Knowledge base: the single source of truth. The .asm file is rendered from it.

Addresses are keyed by (bank, addr). bank = 0 for NROM PRG-ROM, -1 for non-ROM space
(RAM, hardware registers), so bank-switched games fit later without a schema change.
"""
import sqlite3
from array import array
from dataclasses import dataclass
from pathlib import Path

from .rom import Rom

NON_ROM = -1

SCHEMA = """
CREATE TABLE IF NOT EXISTS rom (
    sha1 TEXT PRIMARY KEY, name TEXT NOT NULL, mapper INTEGER NOT NULL, mirroring TEXT NOT NULL,
    header BLOB NOT NULL, prg BLOB NOT NULL, chr BLOB NOT NULL
);
-- kind: op (instruction start) | operand | data | word (16-bit pointer, low byte)
CREATE TABLE IF NOT EXISTS byte_class (
    bank INTEGER NOT NULL, addr INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (bank, addr)
);
-- status ladder: builtin | auto | candidate | verified | approved
-- size: bytes covered (arrays, multi-byte variables); NULL = 1. References inside render as Name+n.
CREATE TABLE IF NOT EXISTS labels (
    bank INTEGER NOT NULL, addr INTEGER NOT NULL, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    status TEXT NOT NULL, source TEXT NOT NULL, confidence REAL, size INTEGER,
    PRIMARY KEY (bank, addr)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY, bank INTEGER NOT NULL, addr INTEGER NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_addr ON comments (bank, addr);
-- Routine extents and call graph (M3). how: vector | jsr | table | cdl ; kind: jsr | jmp | table
CREATE TABLE IF NOT EXISTS routines (
    bank INTEGER NOT NULL, entry INTEGER NOT NULL, last INTEGER NOT NULL, how TEXT NOT NULL,
    PRIMARY KEY (bank, entry)
);
CREATE TABLE IF NOT EXISTS calls (
    bank INTEGER NOT NULL, caller INTEGER NOT NULL, callee INTEGER NOT NULL, site INTEGER NOT NULL,
    kind TEXT NOT NULL, PRIMARY KEY (bank, site, callee)
);
-- Routine annotations (M4): one per annotated routine; the summary is shown to its callers.
CREATE TABLE IF NOT EXISTS routine_notes (
    bank INTEGER NOT NULL, entry INTEGER NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
    source TEXT NOT NULL, confidence REAL, updated TEXT NOT NULL, PRIMARY KEY (bank, entry)
);
-- Experiments (M5): A/B runs in the emulator. A passed experiment can promote its target label.
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, bank INTEGER, addr INTEGER, name TEXT NOT NULL,
    spec TEXT NOT NULL, passed INTEGER NOT NULL, results TEXT NOT NULL
);
-- Dynamic evidence (M2). Coverage arrays are merged across runs: CDL flags OR'd, counters summed.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started TEXT NOT NULL, seed INTEGER, frames INTEGER, actions INTEGER,
    wall_seconds REAL, code_bytes INTEGER, data_bytes INTEGER, archive_size INTEGER
);
-- memtype: nesPrgRom | nesInternalRam ; kind: cdl | readCount | writeCount | execCount
CREATE TABLE IF NOT EXISTS coverage (
    memtype TEXT NOT NULL, kind TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY (memtype, kind)
);
-- Explorer archive: every state is reproducible by replaying `inputs` from power-on.
CREATE TABLE IF NOT EXISTS states (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, cell TEXT NOT NULL, frame INTEGER NOT NULL,
    new_code INTEGER NOT NULL, visits INTEGER NOT NULL, inputs TEXT NOT NULL,
    ram BLOB NOT NULL, savestate BLOB NOT NULL
);
-- Stage B explorer (M6): one row per macro-action the policy chose.
CREATE TABLE IF NOT EXISTS decisions (
    run_id INTEGER NOT NULL, step INTEGER NOT NULL, action TEXT NOT NULL, policy TEXT NOT NULL,
    cell TEXT NOT NULL, frames INTEGER NOT NULL, new_code INTEGER NOT NULL, new_cell INTEGER NOT NULL,
    progress INTEGER, stuck REAL, note TEXT, PRIMARY KEY (run_id, step)
);
-- Benchmark (M8): aggregate scores against a reference disassembly (never per-name pairs).
-- Review evidence (M7). media: screenshots (experiment control/treatment strips, knock-outs).
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, bank INTEGER, addr INTEGER, ref INTEGER, role TEXT NOT NULL,
    frame INTEGER NOT NULL, png BLOB NOT NULL, created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_ref ON media (kind, ref);
-- One recorded call of a routine: registers before each instruction (JSON rows).
CREATE TABLE IF NOT EXISTS traces (
    bank INTEGER NOT NULL, entry INTEGER NOT NULL, state_id INTEGER, frame INTEGER, rows TEXT NOT NULL,
    created TEXT NOT NULL, PRIMARY KEY (bank, entry)
);
-- Knock-out: the routine patched to return at once vs normal play; effect = RAM labels that differ.
CREATE TABLE IF NOT EXISTS knockouts (
    id INTEGER PRIMARY KEY, bank INTEGER NOT NULL, entry INTEGER NOT NULL, state_id INTEGER, frames INTEGER NOT NULL,
    screen_differs INTEGER NOT NULL, effect TEXT NOT NULL, created TEXT NOT NULL
);
-- Human review decisions and hints for the annotator.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, bank INTEGER, addr INTEGER, item TEXT NOT NULL,
    action TEXT NOT NULL, value TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS hints (
    id INTEGER PRIMARY KEY, bank INTEGER NOT NULL, addr INTEGER NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
    created TEXT NOT NULL
);
-- Game research (M7): facts about the game with their sources, and cheat codes.
-- use: annotate (shown in work units) | test (evaluation only, never shown to the annotator)
CREATE TABLE IF NOT EXISTS brief (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, description TEXT NOT NULL, source TEXT NOT NULL,
    use TEXT NOT NULL, created TEXT NOT NULL
);
-- Dashboard (M7): long-running task progress and an activity log of CLI commands.
CREATE TABLE IF NOT EXISTS progress (
    task TEXT PRIMARY KEY, done INTEGER NOT NULL, total INTEGER, note TEXT, started TEXT NOT NULL,
    updated TEXT NOT NULL, finished TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, args TEXT NOT NULL, started TEXT NOT NULL, finished TEXT,
    exit_code INTEGER, summary TEXT
);
-- Work loop rounds (/renes-cycle): what each round did and the project metrics before/after (JSON).
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY, stage TEXT NOT NULL, reason TEXT NOT NULL, started TEXT NOT NULL, finished TEXT,
    before TEXT NOT NULL, after TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Evidence-based confidence (confidence.py): score from collected evidence vs the annotator's claim.
CREATE TABLE IF NOT EXISTS evidence (
    bank INTEGER NOT NULL, addr INTEGER NOT NULL, score REAL NOT NULL, claimed REAL, effective REAL,
    factors TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (bank, addr)
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, reference TEXT NOT NULL, results TEXT NOT NULL
);
-- A new name for a verified/approved label, which only a person may rename (Questions tab).
CREATE TABLE IF NOT EXISTS rename_proposals (
    id INTEGER PRIMARY KEY, bank INTEGER NOT NULL, addr INTEGER NOT NULL, old TEXT NOT NULL,
    name TEXT NOT NULL, why TEXT, agent TEXT, created TEXT NOT NULL
);
"""
# Columns added after the first schema: (table, column, type).
ADDED_COLUMNS = [
    ("labels", "size", "INTEGER"),
    ("runs", "kind", "TEXT"),          # fuzz | agent:<policy>
    ("runs", "run_code", "INTEGER"),   # code bytes this run executed (its own CDL)
    ("runs", "new_code", "INTEGER"),   # code bytes no earlier run had executed
    ("runs", "notes", "TEXT"),         # JSON: stop reason, Jev usage, watched value ranges
    ("brief", "status", "TEXT"),       # proposed (by Claude) | accepted | rejected; NULL = proposed
    ("brief", "author", "TEXT"),       # claude | user
    ("rounds", "agent", "TEXT"),       # who ran the round (claims.py)
    ("activity", "agent", "TEXT"),     # who ran the command (RENES_AGENT, else the project's claim)
    ("runs", "retired", "TEXT"),       # why its archived states are no longer offered (NULL = in use)
    ("experiments", "invalid", "TEXT"),  # why the result proves nothing (NULL = valid); no longer evidence
]
# archived states of runs that aren't retired: SELECT ... FROM states WHERE {LIVE_STATES}
LIVE_STATES = "run_id NOT IN (SELECT id FROM runs WHERE retired IS NOT NULL)"
# button input reached no game in this window (UTC), so runs started in it played without input
INPUT_BUG = ("2026-10-21T16:23", "2026-10-22T21:08")


@dataclass(frozen=True)
class Label:
    bank: int
    addr: int
    name: str
    kind: str
    status: str
    source: str
    confidence: float | None = None
    size: int | None = None


LABEL_COLS = "bank, addr, name, kind, status, source, confidence, size"


class KB:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, kind in ADDED_COLUMNS:
            cols = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> "KB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- ROM ---
    def store_rom(self, rom: Rom) -> None:
        self.db.execute("DELETE FROM rom")
        self.db.execute("INSERT INTO rom VALUES (?,?,?,?,?,?,?)",
                        (rom.sha1, rom.name, rom.mapper, rom.mirroring, rom.header, rom.prg, rom.chr))

    def rom(self) -> Rom:
        row = self.db.execute("SELECT name, header, prg, chr, mapper, mirroring, sha1 FROM rom").fetchone()
        if row is None:
            raise LookupError("knowledge base has no ROM; run 'init' first")
        name, header, prg, chr_, mapper, mirroring, sha1 = row
        return Rom(name, bytes(header), bytes(prg), bytes(chr_), mapper, mirroring, sha1)

    # --- byte classification ---
    def set_classes(self, rows: list[tuple[int, int, str, str]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO byte_class VALUES (?,?,?,?)", rows)

    def classes(self, bank: int) -> dict[int, str]:
        return dict(self.db.execute("SELECT addr, kind FROM byte_class WHERE bank=?", (bank,)))

    def clear_analysis(self, bank: int) -> None:
        """Drop machine-derived analysis; keeps builtin, candidate, verified and approved labels."""
        self.db.execute("DELETE FROM byte_class WHERE bank=?", (bank,))
        self.db.execute("DELETE FROM labels WHERE bank=? AND status IN ('auto', 'builtin') AND source != 'hardware'",
                        (bank,))
        self.db.execute("DELETE FROM routines WHERE bank=?", (bank,))
        self.db.execute("DELETE FROM calls WHERE bank=?", (bank,))

    def set_routines(self, bank: int, rows: list[tuple[int, int, str]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO routines VALUES (?,?,?,?)", [(bank, *r) for r in rows])

    def routines(self, bank: int) -> list[tuple[int, int, str]]:
        return list(self.db.execute("SELECT entry, last, how FROM routines WHERE bank=? ORDER BY entry", (bank,)))

    def set_calls(self, bank: int, rows: list[tuple[int, int, int, str]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?)", [(bank, *r) for r in rows])

    def calls(self, bank: int) -> list[tuple[int, int, int, str]]:
        return list(self.db.execute("SELECT caller, callee, site, kind FROM calls WHERE bank=? ORDER BY site",
                                    (bank,)))

    def set_routine_note(self, bank: int, entry: int, summary: str, status: str, source: str,
                         confidence: float | None, updated: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO routine_notes VALUES (?,?,?,?,?,?,?)",
                        (bank, entry, summary, status, source, confidence, updated))

    def routine_notes(self, bank: int) -> list[tuple[int, str, str, str, float | None]]:
        return list(self.db.execute(
            "SELECT entry, summary, status, source, confidence FROM routine_notes WHERE bank=? ORDER BY entry",
            (bank,)))

    def add_experiment(self, created: str, bank: int | None, addr: int | None, name: str, spec: str,
                       passed: bool, results: str) -> int:
        return self.db.execute(
            "INSERT INTO experiments (created, bank, addr, name, spec, passed, results) VALUES (?,?,?,?,?,?,?)",
            (created, bank, addr, name, spec, int(passed), results)).lastrowid

    def experiments(self, bank: int | None = None, addr: int | None = None,
                    include_invalid: bool = False) -> list[tuple]:
        """(id, created, bank, addr, name, passed) newest first, optionally for one address.
        Experiments marked invalid are left out unless asked for: they are no evidence."""
        sql = "SELECT id, created, bank, addr, name, passed FROM experiments WHERE "
        sql += "1" if include_invalid else "invalid IS NULL"
        if addr is not None:
            return list(self.db.execute(sql + " AND bank=? AND addr=? ORDER BY id DESC", (bank, addr)))
        return list(self.db.execute(sql + " ORDER BY id DESC"))

    def invalid_experiments(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT id, invalid FROM experiments WHERE invalid IS NOT NULL"))

    def invalidate_experiment(self, exp_id: int, reason: str) -> tuple[int | None, int | None] | None:
        """Mark an experiment as proving nothing; returns its (bank, addr) target, or None if unknown."""
        row = self.db.execute("SELECT bank, addr FROM experiments WHERE id=?", (exp_id,)).fetchone()
        if row:
            self.db.execute("UPDATE experiments SET invalid=? WHERE id=?", (reason, exp_id))
        return row

    def set_label_status(self, bank: int, addr: int, status: str) -> None:
        self.db.execute("UPDATE labels SET status=? WHERE bank=? AND addr=?", (status, bank, addr))

    def propose_rename(self, bank: int, addr: int, old: str, name: str, why: str, agent: str | None,
                       created: str) -> None:
        self.db.execute("DELETE FROM rename_proposals WHERE bank=? AND addr=? AND name=?", (bank, addr, name))
        self.db.execute("INSERT INTO rename_proposals (bank, addr, old, name, why, agent, created) "
                        "VALUES (?,?,?,?,?,?,?)", (bank, addr, old, name, why, agent, created))

    def rename_proposals(self) -> list[tuple[int, int, str, str, str, str]]:
        """Open proposals: (bank, addr, old, name, why, agent) whose label still has the old name."""
        return [r for r in self.db.execute(
            "SELECT p.bank, p.addr, p.old, p.name, p.why, p.agent FROM rename_proposals p JOIN labels l "
            "ON l.bank=p.bank AND l.addr=p.addr AND l.name=p.old ORDER BY p.id")]

    def label_at(self, bank: int, addr: int) -> Label | None:
        row = self.db.execute(f"SELECT {LABEL_COLS} FROM labels WHERE bank=? AND addr=?",
                              (bank, addr)).fetchone()
        return Label(*row) if row else None

    def label_named(self, name: str) -> Label | None:
        row = self.db.execute(f"SELECT {LABEL_COLS} FROM labels WHERE name=?", (name,)).fetchone()
        return Label(*row) if row else None

    def delete_comments(self, bank: int, addr: int, kind: str, source: str) -> None:
        self.db.execute("DELETE FROM comments WHERE bank=? AND addr=? AND kind=? AND source=?",
                        (bank, addr, kind, source))

    # --- labels ---
    def add_label(self, label: Label, replace: bool = False) -> bool:
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        cur = self.db.execute(f"{verb} INTO labels ({LABEL_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                              (label.bank, label.addr, label.name, label.kind, label.status,
                               label.source, label.confidence, label.size))
        return cur.rowcount == 1

    def labels(self, bank: int | None = None) -> list[Label]:
        sql = f"SELECT {LABEL_COLS} FROM labels"
        rows = self.db.execute(sql + " WHERE bank=? ORDER BY addr", (bank,)) if bank is not None \
            else self.db.execute(sql + " ORDER BY bank, addr")
        return [Label(*r) for r in rows]

    # --- comments ---
    def add_comment(self, bank: int, addr: int, kind: str, text: str, status: str, source: str) -> None:
        self.db.execute("INSERT INTO comments (bank, addr, kind, text, status, source) VALUES (?,?,?,?,?,?)",
                        (bank, addr, kind, text, status, source))

    def comments(self, bank: int) -> dict[int, list[tuple[str, str]]]:
        out: dict[int, list[tuple[str, str]]] = {}
        for addr, kind, text in self.db.execute(
                "SELECT addr, kind, text FROM comments WHERE bank=? ORDER BY id", (bank,)):
            out.setdefault(addr, []).append((kind, text))
        return out

    # --- dynamic evidence ---
    def coverage(self, memtype: str, kind: str) -> bytes | None:
        row = self.db.execute("SELECT data FROM coverage WHERE memtype=? AND kind=?", (memtype, kind)).fetchone()
        return bytes(row[0]) if row else None

    def merge_cdl(self, memtype: str, cdl: bytes) -> bytes:
        old = self.coverage(memtype, "cdl")
        merged = cdl if old is None else bytes(a | b for a, b in zip(old, cdl, strict=True))
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?)", (memtype, "cdl", merged))
        return merged

    def merge_counters(self, memtype: str, kind: str, counts: list[int]) -> None:
        old = self.coverage(memtype, kind)
        total = array("Q", counts)
        if old is not None:
            prev = array("Q", old)
            total = array("Q", (a + b for a, b in zip(prev, total, strict=True)))
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?)", (memtype, kind, total.tobytes()))

    def counters(self, memtype: str, kind: str) -> list[int] | None:
        data = self.coverage(memtype, kind)
        return None if data is None else list(array("Q", data))

    def add_run(self, started: str, seed: int) -> int:
        return self.db.execute("INSERT INTO runs (started, seed) VALUES (?,?)", (started, seed)).lastrowid

    def finish_run(self, run_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def replace_states(self, run_id: int, rows: list[tuple]) -> None:
        """rows: (cell, frame, new_code, visits, inputs_json, ram, savestate)."""
        self.db.execute("DELETE FROM states WHERE run_id=?", (run_id,))
        self.db.executemany(
            "INSERT INTO states (run_id, cell, frame, new_code, visits, inputs, ram, savestate) "
            "VALUES (?,?,?,?,?,?,?,?)", [(run_id, *r) for r in rows])

    def add_decisions(self, run_id: int, rows: list[tuple]) -> None:
        """rows: (step, action, policy, cell, frames, new_code, new_cell, progress, stuck, note)."""
        self.db.executemany(
            "INSERT OR REPLACE INTO decisions (run_id, step, action, policy, cell, frames, new_code, new_cell, "
            "progress, stuck, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(run_id, *r) for r in rows])

    def add_media(self, kind: str, bank: int | None, addr: int | None, ref: int | None, role: str, frame: int,
                  png: bytes, created: str) -> None:
        self.db.execute("INSERT INTO media (kind, bank, addr, ref, role, frame, png, created) VALUES (?,?,?,?,?,?,?,?)",
                        (kind, bank, addr, ref, role, frame, png, created))

    def media(self, kind: str, ref: int) -> list[tuple[int, str, int]]:
        """(id, role, frame) of the screenshots for one experiment/knock-out, in order."""
        return list(self.db.execute("SELECT id, role, frame FROM media WHERE kind=? AND ref=? ORDER BY role, frame",
                                    (kind, ref)))

    def add_hint(self, bank: int, addr: int, text: str, source: str, created: str) -> None:
        self.db.execute("INSERT INTO hints (bank, addr, text, source, created) VALUES (?,?,?,?,?)",
                        (bank, addr, text, source, created))

    def hints(self, bank: int, addr: int) -> list[tuple[str, str]]:
        return list(self.db.execute("SELECT text, source FROM hints WHERE bank=? AND addr=? ORDER BY id", (bank, addr)))

    def rename_label(self, bank: int, addr: int, new: str, status: str, source: str) -> str | None:
        """Rename and propagate: summaries and comments that mention the old name are updated."""
        old = self.label_at(bank, addr)
        if old is None:
            return None
        self.db.execute("UPDATE labels SET name=?, status=?, source=? WHERE bank=? AND addr=?",
                        (new, status, source, bank, addr))
        if old.name != new:
            self.propagate_name(old.name, new)
        return old.name

    def propagate_name(self, old: str, new: str) -> int:
        """Replace whole-word mentions of a renamed label in summaries and comments."""
        import re
        pattern = re.compile(r"\b" + re.escape(old) + r"\b")
        changed = 0
        for table, key, col in (("routine_notes", "rowid", "summary"), ("comments", "id", "text")):
            for rid, text in self.db.execute(f"SELECT {key}, {col} FROM {table} WHERE {col} LIKE ?", (f"%{old}%",)).fetchall():
                updated = pattern.sub(new, text)
                if updated != text:
                    self.db.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?", (updated, rid))
                    changed += 1
        return changed

    def set_progress(self, task: str, done: int, total: int | None = None, note: str = "",
                     finished: bool = False) -> None:
        """Progress of a long-running task, polled by the dashboard. Commits immediately."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO progress (task, done, total, note, started, updated, finished) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(task) DO UPDATE SET done=excluded.done, total=excluded.total, note=excluded.note, "
            "updated=excluded.updated, finished=excluded.finished, "
            "started=CASE WHEN progress.finished IS NOT NULL THEN excluded.started ELSE progress.started END",
            (task, done, total, note, now, now, now if finished else None))
        self.db.commit()

    def setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

    def add_benchmark(self, created: str, reference: str, results: str) -> None:
        self.db.execute("INSERT INTO benchmarks (created, reference, results) VALUES (?,?,?)",
                        (created, reference, results))

    def benchmarks(self) -> list[tuple[int, str, str, str]]:
        """(id, created, reference, results JSON), oldest first."""
        return list(self.db.execute("SELECT id, created, reference, results FROM benchmarks ORDER BY id"))

    def code_mask(self) -> bytes:
        """1 per PRG byte executed as code in any recorded run, else 0."""
        cdl = self.coverage("nesPrgRom", "cdl") or b""
        return bytes(f & 0x01 for f in cdl)

    def commit(self) -> None:
        self.db.commit()
