"""Notes from the loop agents to whoever develops renes.

Agents running `/renes-cycle` hit tool limits, bugs and workarounds that only they see. They
record them here (`renes feedback add`); the developer lists the open ones, fixes them and closes
them with a reply. The next `round start` on that project prints the reply once, so the agent
learns what changed without the user relaying it.

The developer also talks back between fixes: `ack` tells the agents that filed notes they are being
worked on (status "working"), and `patch` publishes patch notes, what changed in renes, to every
agent; `--fixes` closes the notes a patch resolves. Each project's next round start prints the
messages it hasn't seen (`inbox`).

One shared file for all projects: work/feedback.sqlite3 (not *.db, so it isn't taken for a project).
"""
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("bug", "limit", "idea", "insight")
OPEN = ("open", "working")
SCHEMA = """CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, agent TEXT, project TEXT, kind TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', reply TEXT, replied TEXT, delivered TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    fixes TEXT NOT NULL DEFAULT '', projects TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS seen (project TEXT PRIMARY KEY, last_message INTEGER NOT NULL)"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(work: Path) -> sqlite3.Connection:
    db = sqlite3.connect(work / "feedback.sqlite3", timeout=10)
    db.executescript(SCHEMA)
    return db


def add(work: Path, kind: str, text: str, agent: str | None = None, project: str | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    db = connect(work)
    try:
        with db:
            return db.execute("INSERT INTO feedback (created, agent, project, kind, text) VALUES (?, ?, ?, ?, ?)",
                              (_now(), agent, project, kind, text.strip())).lastrowid
    finally:
        db.close()


def close(work: Path, fid: int, reply: str, status: str = "done") -> bool:
    db = connect(work)
    try:
        with db:
            return db.execute("UPDATE feedback SET status=?, reply=?, replied=? WHERE id=?",
                              (status, reply.strip(), _now(), fid)).rowcount == 1
    finally:
        db.close()


def items(work: Path, everything: bool = False, project: str | None = None) -> list[dict]:
    sql = "SELECT * FROM feedback WHERE 1=1" + ("" if everything else " AND status IN ('open', 'working')")
    params = []
    if project:
        sql += " AND project=?"
        params.append(project)
    db = connect(work)
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql + " ORDER BY id", params)]
    finally:
        db.close()


def latest_id(work: Path) -> int:
    if not (work / "feedback.sqlite3").exists():
        return 0
    db = connect(work)
    try:
        return db.execute("SELECT coalesce(max(id), 0) FROM feedback").fetchone()[0]
    finally:
        db.close()


def _pending(work: Path, project: str | None, after: int) -> list[dict]:
    if not (work / "feedback.sqlite3").exists():
        return []
    db = connect(work)
    try:
        db.row_factory = sqlite3.Row
        if project is None:      # the developer: notes filed since it started waiting
            rows = db.execute("SELECT * FROM feedback WHERE status='open' AND id>? ORDER BY id", (after,))
        else:                    # an agent: replies to its project not yet shown at a round start
            rows = db.execute("SELECT * FROM feedback WHERE project=? AND reply IS NOT NULL AND delivered IS NULL "
                              "ORDER BY id", (project,))
        return [dict(r) for r in rows]
    finally:
        db.close()


def wait(work: Path, project: str | None = None, after: int | None = None, poll: float = 5,
         timeout: float | None = None, sleep=time.sleep, clock=time.monotonic) -> list[dict]:
    """Block until there is something for the waiter, then return it ([] on timeout).

    Without a project: new open notes (id > after; default: those filed from now on). With one: replies
    to that project's notes. Run in the background, the waiting process exiting is the signal that
    wakes the developer or the agent.
    """
    after = latest_id(work) if after is None else after
    end = None if timeout is None else clock() + timeout
    while True:
        found = _pending(work, project, after)
        if found or (end is not None and clock() >= end):
            return found
        sleep(poll)


def undelivered(work: Path, project: str) -> list[dict]:
    """Replies to this project's notes not yet shown at a round start; marks them shown."""
    if not (work / "feedback.sqlite3").exists():
        return []
    db = connect(work)
    try:
        db.row_factory = sqlite3.Row
        with db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM feedback WHERE project=? AND reply IS NOT NULL AND delivered IS NULL ORDER BY id",
                (project,))]
            db.executemany("UPDATE feedback SET delivered=? WHERE id=?", [(_now(), r["id"]) for r in rows])
        return rows
    finally:
        db.close()


def _ids(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _message(db, kind: str, text: str, fixes: list[int], projects: list[str]) -> int:
    return db.execute("INSERT INTO messages (created, kind, text, fixes, projects) VALUES (?, ?, ?, ?, ?)",
                      (_now(), kind, text.strip(), ",".join(map(str, fixes)), ",".join(sorted(set(projects))))).lastrowid


def ack(work: Path, ids: list[int], text: str) -> int:
    """Tell the agents behind these notes that they are being worked on; returns the message id."""
    db = connect(work)
    try:
        with db:
            projects = [p for (p,) in db.execute(
                f"SELECT DISTINCT coalesce(project, '') FROM feedback WHERE id IN ({','.join('?' * len(ids))})", ids)]
            if len(ids) != db.execute(f"SELECT count(*) FROM feedback WHERE id IN ({','.join('?' * len(ids))})",
                                      ids).fetchone()[0]:
                raise ValueError("unknown feedback number")
            db.execute(f"UPDATE feedback SET status='working' WHERE status='open' AND id IN ({','.join('?' * len(ids))})",
                       ids)
            return _message(db, "ack", text, ids, [p for p in projects if p] or [])
    finally:
        db.close()


def patch(work: Path, text: str, fixes: list[int] = ()) -> int:
    """Publish a patch note to every project; the notes it fixes are closed with it as the reply."""
    db = connect(work)
    try:
        with db:
            mid = _message(db, "patch", text, list(fixes), [])
            db.executemany("UPDATE feedback SET status='done', reply=?, replied=? WHERE id=?",
                           [(f"fixed in patch note {mid}: {text.strip()}", _now(), f) for f in fixes])
            return mid
    finally:
        db.close()


def messages(work: Path, limit: int = 20) -> list[dict]:
    if not (work / "feedback.sqlite3").exists():
        return []
    db = connect(work)
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))][::-1]
    finally:
        db.close()


def inbox(work: Path, project: str) -> list[dict]:
    """Developer messages for this project not yet shown to it: patch notes for everyone, acks for its notes.
    Marks them shown. A project's first inbox starts from the latest message (no backlog of old news)."""
    if not (work / "feedback.sqlite3").exists():
        return []
    db = connect(work)
    try:
        db.row_factory = sqlite3.Row
        with db:
            row = db.execute("SELECT last_message FROM seen WHERE project=?", (project,)).fetchone()
            latest = db.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
            if row is None:
                own = db.execute("SELECT 1 FROM feedback WHERE project=? LIMIT 1", (project,)).fetchone()
                last = 0 if own else latest            # agents that filed notes get every reply to them
            else:
                last = row[0]
            rows = [dict(r) for r in db.execute("SELECT * FROM messages WHERE id>? ORDER BY id", (last,))
                    if not r["projects"] or project in r["projects"].split(",")]
            db.execute("INSERT INTO seen (project, last_message) VALUES (?, ?) "
                       "ON CONFLICT(project) DO UPDATE SET last_message=excluded.last_message", (project, latest))
        return rows
    finally:
        db.close()


def format_messages(rows: list[dict]) -> str:
    out = []
    for m in rows:
        notes = ", ".join(f"#{n}" for n in _ids(m["fixes"]))
        if m["kind"] == "ack":
            head = "developer is working on " + (f"your notes {notes}" if notes else "it")
        else:
            head = f"patch note {m['id']}" + (f" (fixes {notes})" if notes else "")
        out.append(f"renes {head}: {m['text']}")
    return "".join(line + "\n" for line in out)


def format_items(rows: list[dict]) -> str:
    if not rows:
        return "no feedback notes\n"
    out = []
    for r in rows:
        who = " / ".join(x for x in (r["project"], r["agent"]) if x) or "-"
        out.append(f"#{r['id']:<4} {r['status']:<7} {r['kind']:<7} {r['created'][:16]}  {who}")
        out.append(f"      {r['text']}")
        if r["reply"]:
            out.append(f"      -> {r['reply']}")
    return "\n".join(out) + "\n"
