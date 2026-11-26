"""Project dashboard and human review (M7), served locally.

Tabs: Overview (pipeline stages, numbers, validation, the loop's next step), Research (review
Claude's game brief before it enters the loop, add your own context), Review (approve, correct
or send back annotations) and Activity (running tasks with progress, recent commands and events).
The page polls: everything is read fresh from the knowledge base, so it follows the CLI and the
agent loop running outside the page.


`python -m renes review <kb>` serves the page on 127.0.0.1 only. Screenshots and code come from a
copyrighted ROM, so nothing is published: the page, its images and its API never leave this
machine. Decisions are written straight into the knowledge base:

  approve   the label (and routine summary) becomes `approved`: locked against the annotator
  rename    the human's name becomes `approved`; summaries and comments that used the old name
            are updated (propagation)
  hint      a note for the annotator. A routine is marked `rework` and goes to the front of the
            annotation queue; its next work unit shows the old summary and the hint
  reject    a RAM label is removed; a routine is sent back for rework

Every decision is also recorded in the `reviews` table.
"""
import json
import re
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import annotate, confidence
from .analyze import PRG_BANK
from .kb import KB, NON_ROM
from .render import symbols_for

APP = Path(__file__).with_name("review_app.html")
ICON = Path(__file__).with_name("favicon.svg")
ACTIONS = {"approve", "rename", "hint", "reject"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _experiments(kb: KB) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for eid, created, bank, addr, name, spec, passed, results, invalid in kb.db.execute(
            "SELECT id, created, bank, addr, name, spec, passed, results, invalid FROM experiments ORDER BY id"):
        if addr is None:
            continue
        verifies = json.loads(spec).get("verifies", True)
        out.setdefault(addr, []).append({
            "id": eid, "name": name, "passed": bool(passed), "diagnostic": not verifies, "created": created,
            "invalid": invalid,
            "checks": json.loads(results),
            "media": [{"id": m, "role": r, "frame": f} for m, r, f in kb.media("experiment", eid)]})
    return out


def routine_id(bank: int, entry: int) -> str:
    """Review item id of a routine: "r<entry>" in bank 0, "r<bank>:<entry>" in other banks."""
    return f"r{entry}" if bank == PRG_BANK else f"r{bank}:{entry}"


def parse_id(item_id: str) -> tuple[str, int, int]:
    """(kind, bank, addr) of a review item id ("m<addr>", "r<entry>" or "r<bank>:<entry>")."""
    kind, rest = item_id[0], item_id[1:]
    if kind == "m":
        return kind, NON_ROM, int(rest)
    bank, _, addr = rest.rpartition(":")
    return kind, int(bank) if bank else PRG_BANK, int(addr)


def items(kb: KB) -> list[dict]:
    """Everything reviewable, most in need of review first."""
    ctxs = annotate.contexts(kb)
    exps = _experiments(kb)
    has_ko = set(kb.db.execute("SELECT DISTINCT bank, entry FROM knockouts WHERE bank>=0"))
    reads = kb.counters("nesInternalRam", "readCount") or []
    writes = kb.counters("nesInternalRam", "writeCount") or []
    checked = {b: confidence.scores(kb, b) for b in [*(c.bank for c in ctxs), NON_ROM]}

    def judged(bank: int, addr: int, conf, flags: list[str]) -> float | None:
        """Effective confidence (claim, discounted when incoherent) plus its flags."""
        score, claimed, eff, factors = checked[bank].get(addr, (None, conf, conf, {}))
        if eff is not None and eff < 0.5:
            flags.append("low confidence")
        if bank != NON_ROM and factors and not factors.get("coherent"):
            flags.append("placeholder name")
        if score is not None and claimed is not None and claimed - score >= confidence.OVERCONFIDENT:
            flags.append("overconfident")
        return eff

    out = []
    for ctx in ctxs:
        labels = {l.addr: l for l in kb.labels(ctx.bank)}
        for entry, summary, status, source, conf in kb.routine_notes(ctx.bank):
            l = labels.get(entry)
            flags = []
            if not ctx.executed(entry):
                flags.append("never ran")
            eff = judged(ctx.bank, entry, conf, flags)
            if status == "rework":
                flags.append("sent back")
            out.append({"id": routine_id(ctx.bank, entry), "type": "routine", "bank": ctx.bank, "addr": entry,
                        "name": l.name if l else f"${entry:04X}", "status": l.status if l and l.status != "auto" else status,
                        "note_status": status, "confidence": eff, "claimed": conf, "summary": summary, "flags": flags,
                        "evidence": (ctx.bank, entry) in has_ko})
    for l in kb.labels(NON_ROM):
        if l.kind != "ram":
            continue
        e = exps.get(l.addr, [])
        flags = []
        if any(not x["passed"] and not x["diagnostic"] for x in e):
            flags.append("failed experiment")
        eff = judged(NON_ROM, l.addr, l.confidence, flags)
        if (l.size or 1) > 1:
            flags.append(f"size {l.size}")
        comment = next((t for k, t in kb.comments(NON_ROM).get(l.addr, []) if k == "line"), "")
        out.append({"id": f"m{l.addr}", "type": "ram", "bank": NON_ROM, "addr": l.addr, "name": l.name,
                    "status": l.status, "confidence": eff, "claimed": l.confidence, "summary": comment, "size": l.size,
                    "flags": flags, "experiments": len(e), "evidence": any(x["media"] for x in e),
                    "accesses": reads[l.addr] + writes[l.addr] if l.addr < len(reads) else None})
    rank = {"rework": 0, "candidate": 1, "auto": 1, "verified": 2, "approved": 3}
    out.sort(key=lambda i: (rank.get(i["status"], 1), -len(i["flags"]), i["confidence"] if i["confidence"] is not None else 0.5,
                            i["addr"]))
    return out


def _checked(kb: KB, bank: int, addr: int) -> dict | None:
    row = confidence.scores(kb, bank).get(addr)
    return None if row is None else {"evidence": row[0], "effective": row[2], "factors": confidence.describe(row[3])}


def detail(kb: KB, item_id: str) -> dict:
    kind, bank, addr = parse_id(item_id)
    if kind == "m":
        l = kb.label_at(NON_ROM, addr)
        if l is None:
            raise KeyError(item_id)
        users = []                                  # RAM is shared: its users in every bank
        for c in annotate.contexts(kb):
            names = {x.addr: x.name for x in kb.labels(c.bank)}
            users += [names.get(e, c.ref(e)) for e, (last, _) in sorted(c.routines.items()) if _touches(c, e, last, addr)]
        return {"id": item_id, "type": "ram", "name": l.name, "addr": addr, "status": l.status, "size": l.size,
                "confidence": l.confidence, "checked": _checked(kb, NON_ROM, addr),
                "comment": next((t for k, t in kb.comments(NON_ROM).get(addr, []) if k == "line"), ""),
                "experiments": _experiments(kb).get(addr, []),
                "used_by": users[:20],
                "hints": [t for t, _ in kb.hints(NON_ROM, addr)],
                "symbols": [l.name] + [u for u in users if not u.startswith(("$", "B"))]}
    if bank not in annotate.banks(kb):
        raise KeyError(item_id)
    ctx = annotate.Context.load(kb, bank)
    if addr not in ctx.routines:
        raise KeyError(item_id)
    last = ctx.routines[addr][0]
    sym = symbols_for(kb, bank)
    labels = {l.addr: l for l in kb.labels(bank)}
    listing = annotate._listing(ctx, sym, labels, kb.comments(bank), addr, last)
    trace_row = kb.db.execute("SELECT frame, rows FROM traces WHERE bank=? AND entry=?", (bank, addr)).fetchone()
    visits: dict[int, list[tuple[dict, dict | None]]] = {}
    trace_info = None
    if trace_row:
        t = json.loads(trace_row[1])
        rows = t["rows"]
        for i, r in enumerate(rows):
            visits.setdefault(r["pc"], []).append((r, rows[i + 1] if i + 1 < len(rows) else None))
        trace_info = {"frame": trace_row[0], "instructions": len(rows), "finished": t["finished"]}
    lines = []
    for text in listing.lines:
        a = int(text[1:5], 16) if text.startswith("$") else None
        v = visits.get(a, []) if a is not None else []
        lines.append({"text": text, "addr": a, "visits": len(v),
                      "before": v[0][0] if v else None, "after": v[0][1] if v else None})
    ko = kb.db.execute("SELECT id, frames, screen_differs, effect FROM knockouts WHERE bank=? AND entry=? "
                       "ORDER BY id DESC LIMIT 1", (bank, addr)).fetchone()
    knockout = None
    if ko:
        effect = json.loads(ko[3])
        knockout = {"frames": ko[1], "screen_differs": bool(ko[2]), "labeled": effect["labeled"],
                    "other_bytes": effect["other_bytes"],
                    "media": [{"id": m, "role": r, "frame": f} for m, r, f in kb.media("knockout", ko[0])]}
    note = kb.db.execute("SELECT summary, status, confidence FROM routine_notes WHERE bank=? AND entry=?",
                         (bank, addr)).fetchone()
    names = {x.addr: x.name for x in kb.labels(bank)}
    return {"id": item_id, "type": "routine", "name": labels[addr].name if addr in labels else f"${addr:04X}",
            "addr": addr, "last": last, "summary": note[0] if note else "", "note_status": note[1] if note else None,
            "confidence": note[2] if note else None, "checked": _checked(kb, bank, addr),
            "status": labels[addr].status if addr in labels else "auto",
            "executed": ctx.executed(addr), "lines": lines, "trace": trace_info, "knockout": knockout,
            "callers": sorted({names.get(c, f"${c:04X}") for c, _, _ in ctx.callers.get(addr, [])}),
            "callees": sorted({names.get(c, f"${c:04X}") for c, _, _ in ctx.callees.get(addr, [])}),
            "hints": [t for t, _ in kb.hints(bank, addr)],
            # names in this listing that the Code tab defines, so the page can link them there
            "symbols": sorted(set(re.findall(r"[A-Za-z_]\w+", " ".join(x["text"] for x in lines)))
                              & ({x.name for x in kb.labels(bank)} | {x.name for x in kb.labels(NON_ROM)}))}


def _touches(ctx, entry: int, last: int, ram: int) -> bool:
    from .opcodes import OPCODES, ZEROPAGE_MODES, ABSOLUTE_MODES, operand_value
    pc = entry
    while pc <= last:
        if ctx.kinds.get(pc) != "op":
            pc += 1
            continue
        op = OPCODES.get(ctx.prg[pc - ctx.base])
        if op is None:
            break
        if op.mode in ZEROPAGE_MODES | ABSOLUTE_MODES and operand_value(op, ctx.prg, pc - ctx.base) == ram:
            return True
        pc += op.size
    return False


def decide(kb: KB, item_id: str, action: str, value: str = "", note: str = "") -> dict:
    """Apply one reviewer decision. Returns {"ok": bool, "message": str}."""
    if action not in ACTIONS:
        return {"ok": False, "message": f"unknown action {action}"}
    kind, bank, addr = parse_id(item_id)
    label = kb.label_at(bank, addr)
    message = ""
    if action == "approve":
        if label:
            kb.set_label_status(bank, addr, "approved")
        if bank != NON_ROM:
            kb.db.execute("UPDATE routine_notes SET status='approved', source='human' WHERE bank=? AND entry=?", (bank, addr))
            kb.db.execute("UPDATE comments SET status='approved' WHERE bank=? AND addr BETWEEN ? AND ?",
                          (bank, addr, kb.db.execute("SELECT last FROM routines WHERE bank=? AND entry=?",
                                                     (bank, addr)).fetchone()[0]))
        message = f"approved {label.name if label else hex(addr)}"
    elif action == "rename":
        new = value.strip()
        problem = annotate._check_name(kb, new, bank, addr)
        if problem and not problem.startswith("would overwrite"):   # the human may rename anything
            return {"ok": False, "message": f"{new}: {problem}"}
        if label is None:
            return {"ok": False, "message": "nothing to rename"}
        old = kb.rename_label(bank, addr, new, "approved", "human")
        message = f"renamed {old} -> {new} (approved; summaries and comments updated)"
    elif action == "hint":
        if not value.strip():
            return {"ok": False, "message": "the hint is empty"}
        kb.add_hint(bank, addr, value.strip()[:400], "human", _now())
        if bank != NON_ROM:
            kb.db.execute("UPDATE routine_notes SET status='rework' WHERE bank=? AND entry=?", (bank, addr))
            message = "hint saved; routine sent back to the front of the annotation queue"
        else:
            message = "hint saved; it appears in the work units of routines using this variable"
    elif action == "reject":
        if bank == NON_ROM and label:
            kb.db.execute("DELETE FROM labels WHERE bank=? AND addr=?", (bank, addr))
            kb.db.execute("DELETE FROM comments WHERE bank=? AND addr=?", (bank, addr))
            if value.strip():
                kb.add_hint(bank, addr, f"rejected name {label.name}: {value.strip()[:300]}", "human", _now())
            message = f"removed {label.name}"
        else:
            kb.db.execute("UPDATE routine_notes SET status='rework' WHERE bank=? AND entry=?", (bank, addr))
            kb.add_hint(bank, addr, f"rejected: {value.strip() or 'annotation is wrong'}"[:400], "human", _now())
            message = "routine sent back for rework"
    kb.db.execute("INSERT INTO reviews (created, bank, addr, item, action, value, note) VALUES (?,?,?,?,?,?,?)",
                  (_now(), bank, addr, item_id, action, value, note))
    kb.commit()
    return {"ok": True, "message": message}


# ---------------------------------------------------------------------------------------------
# Local server


# Host names the dashboard answers to (direct, or through a local reverse proxy such as Caddy).
# Anything else is refused, which blocks DNS-rebinding pages from reaching the API.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "renes.localhost", "renes", "[::1]"}


def find_projects(source: Path) -> dict[str, Path]:
    """The knowledge bases the dashboard serves: one file, or every *.db in a folder (re-scanned on
    each request, so a new `renes init` shows up without a restart; backups are skipped)."""
    paths = [source] if source.is_file() else sorted(source.glob("*.db"))
    return {p.stem: p for p in paths if "backup" not in p.stem.lower()}


def project_summary(stem: str, path: Path) -> dict:
    """One project card for the project list: the ROM and the headline progress numbers."""
    from . import cycle

    with KB(path) as kb:
        rom = kb.rom()
        cdl = kb.coverage("nesPrgRom", "cdl") or b""
        reference = kb.setting("reference")
        s = cycle.status(kb, has_reference=bool(reference and Path(reference).exists()))
        last = kb.db.execute("SELECT max(started) FROM activity").fetchone()[0]
        verified = sum(1 for l in kb.labels(NON_ROM) if l.kind == "ram" and l.status == "verified")
        rounds = kb.db.execute("SELECT count(*) FROM rounds").fetchone()[0]
        from . import audit, claims, grade
        owner = claims.owner(kb)
        audited, graded = audit.latest(kb), grade.latest(kb)
    quality = {"audit": audited and {"when": audited["created"], "items": audited["audited"], **{
                   k: audited["all"][k] for k in ("supported_rate", "consistent_rate", "unsupported")}},
               "grading": graded and {"when": graded["created"], "pairs": graded["graded"], **{
                   k: graded["all"][k] for k in ("same_rate", "right_behaviour_rate", "wrong")}}}
    return {"owner": owner, "quality": quality, "id": stem, "name": rom.name, "mapper": rom.mapper, "prg": len(rom.prg), "sha1": rom.sha1,
            "executed": sum(1 for f in cdl if f & 1), "annotated": s.annotated, "routines": s.routines,
            "ram_verified": verified, "rounds": rounds, "next": s.recommendation, "reason": s.reason,
            "last_activity": last, "modified": path.stat().st_mtime}


def make_handler(source: Path, build):
    """`source` is one knowledge base or a folder of them; `build(kb_path)` rebuilds one project."""
    lock = threading.Lock()

    def pick(requested: str | None) -> tuple[str, Path]:
        projects = find_projects(source)
        if not projects:
            raise KeyError("no knowledge bases found")
        if requested in projects:
            return requested, projects[requested]
        stem = max(projects, key=lambda k: projects[k].stat().st_mtime)   # default: the most recently changed
        return stem, projects[stem]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            if name in ALLOWED_HOSTS:
                return True
            self._send(421, b"unknown host", "text/plain")
            return False

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self._send(200, APP.read_bytes(), "text/html; charset=utf-8")
                    return
                if url.path in ("/favicon.svg", "/favicon.ico"):
                    self._send(200, ICON.read_bytes(), "image/svg+xml")
                    return
                if url.path == "/api/projects":
                    with lock:
                        cards = [project_summary(k, v) for k, v in find_projects(source).items()]
                    self._json({"projects": cards, "default": pick(None)[0] if cards else None})
                    return
                stem, kb_path = pick(query.get("p", [None])[0])
                if url.path == "/api/items":
                    with lock, KB(kb_path) as kb:
                        self._json({"kb": kb_path.name, "project": stem, "items": items(kb)})
                elif url.path == "/api/questions":
                    from . import questions
                    with lock, KB(kb_path) as kb:
                        self._json({"project": stem, "groups": questions.questions(kb)})
                elif url.path == "/api/item":
                    with lock, KB(kb_path) as kb:
                        self._json(detail(kb, query["id"][0]))
                elif url.path in ("/api/overview", "/api/research", "/api/activity"):
                    fn = {"/api/overview": overview, "/api/research": research, "/api/activity": activity}[url.path]
                    with lock, KB(kb_path) as kb:
                        self._json({**fn(kb), "project": stem})
                elif url.path in ("/api/rommap", "/api/rammap", "/api/graph"):
                    from . import maps
                    fn = {"/api/rommap": maps.rom_map, "/api/rammap": maps.ram_map, "/api/graph": maps.subsystems}[url.path]
                    with lock, KB(kb_path) as kb:
                        self._json({**fn(kb), "project": stem})
                elif url.path == "/api/asm":
                    with lock, KB(kb_path) as kb:
                        view = asm_view(kb, query.get("v", [""])[0])
                    view["file"] = f"out/{kb_path.stem}/{kb_path.stem}.asm"
                    self._json(view)
                elif url.path.startswith("/media/"):
                    with lock, KB(kb_path) as kb:
                        row = kb.db.execute("SELECT png FROM media WHERE id=?", (int(url.path[7:].split(".")[0]),)).fetchone()
                    if row:
                        self._send(200, bytes(row[0]), "image/png")
                    else:
                        self._send(404, b"not found", "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")
            except (KeyError, ValueError) as e:
                self._json({"error": str(e)}, 404)

        def do_POST(self):
            if not self._host_ok():
                return
            # JSON only: a cross-site page cannot send this without a CORS preflight, which we never grant.
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                self._send(415, b"application/json required", "text/plain")
                return
            url = urlparse(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            try:
                _, kb_path = pick(parse_qs(url.query).get("p", [body.get("project")])[0])
            except KeyError as e:
                self._json({"ok": False, "message": str(e)}, 404)
                return
            if url.path == "/api/decision":
                with lock, KB(kb_path) as kb:
                    self._json(decide(kb, body["id"], body["action"], body.get("value", ""), body.get("note", "")))
            elif url.path == "/api/brief":
                with lock, KB(kb_path) as kb:
                    self._json(brief_action(kb, body["action"], body.get("id"), body.get("text", ""),
                                            body.get("kind", "context"), body.get("source", "")))
            elif url.path == "/api/build":
                with lock:
                    self._json(build(kb_path))
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(source: Path, build, port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(source, build))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    names = ", ".join(find_projects(source)) or "none yet"
    print(f"dashboard: {url}  (local only; Ctrl+C to stop)  projects: {names}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------------------------
# Dashboard: overview, research and activity (polled by the page)


def _since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


def overview(kb: KB) -> dict:
    from . import cycle

    rom = kb.rom()
    prg = len(rom.prg)
    cdl = kb.coverage("nesPrgRom", "cdl") or b""
    executed = sum(1 for f in cdl if f & 1)
    classes = [k for b in annotate.banks(kb) for k in kb.classes(b).values()]
    code = sum(1 for k in classes if k in ("op", "operand"))
    data = sum(1 for k in classes if k in ("data", "word", "word_hi"))
    reference = kb.setting("reference")
    s = cycle.status(kb, has_reference=bool(reference and Path(reference).exists()))
    ram_status: dict[str, int] = {}
    for l in kb.labels(NON_ROM):
        if l.kind == "ram":
            ram_status[l.status] = ram_status.get(l.status, 0) + 1
    routine_status = dict(kb.db.execute("SELECT status, count(*) FROM routine_notes WHERE bank>=0 GROUP BY status"))
    exp_total, exp_passed = kb.db.execute("SELECT count(*), coalesce(sum(passed), 0) FROM experiments WHERE invalid IS NULL").fetchone()
    evidence = kb.db.execute("SELECT count(DISTINCT entry) FROM knockouts").fetchone()[0]
    benches = []
    for bid, created, _ref, results in kb.benchmarks():
        r = json.loads(results)
        benches.append({"id": bid, "created": created,
                        "split": r["classification"]["agreement_on_classified"],
                        "classified": r["classification"]["classified_share"],
                        "names": r["naming"]["all"]["match_rate"],
                        "verified": (r["naming"]["by_status"].get("verified") or {}).get("match_rate")})
    notes = sorted(u for (u,) in kb.db.execute("SELECT updated FROM routine_notes WHERE bank>=0"))
    step = max(1, len(notes) // 60)
    curve = [{"t": notes[i], "n": i + 1} for i in range(0, len(notes), step)]
    if notes and curve[-1]["n"] != len(notes):
        curve.append({"t": notes[-1], "n": len(notes)})
    brief = dict(kb.db.execute("SELECT coalesce(status, 'proposed'), count(*) FROM brief GROUP BY 1").fetchall())
    runs = kb.db.execute("SELECT coalesce(kind, 'fuzz'), count(*) FROM runs WHERE frames IS NOT NULL GROUP BY 1").fetchall()
    approved = routine_status.get("approved", 0) + ram_status.get("approved", 0)
    stages = [
        {"name": "ROM", "state": "done", "detail": f"{rom.name}: mapper {rom.mapper}, PRG {prg // 1024} KB"},
        {"name": "Exploration", "state": "done" if executed else "todo",
         "detail": f"{executed} code bytes executed ({executed / prg:.0%} of PRG); runs: "
                   + ", ".join(f"{k} x{n}" for k, n in runs)},
        {"name": "Analysis", "state": "done" if classes else "todo",
         "detail": f"{(code + data) / prg:.0%} classified ({code} code, {data} data), {prg - code - data} bytes unknown"},
        {"name": "Research", "state": "todo" if not s.researched else "waiting" if s.brief_pending else "done",
         "detail": (f"{sum(brief.values())} facts: " + ", ".join(f"{n} {k}" for k, n in sorted(brief.items())))
                   if brief else "not researched yet"},
        {"name": "Annotation", "state": "done" if s.annotated >= s.routines else "active",
         "detail": f"{s.annotated} of {s.routines} routines ({s.annotated / max(s.routines, 1):.0%}); {s.ready} ready now"},
        {"name": "Experiments", "state": "active" if exp_total else "todo",
         "detail": f"{exp_total} run, {exp_passed} passed; {ram_status.get('verified', 0)} RAM labels verified"},
        {"name": "Evidence", "state": "active" if evidence else "todo",
         "detail": f"{evidence} routines with screenshots and traces; {s.without_evidence} annotated without"},
        {"name": "Review", "state": "active" if approved else "todo",
         "detail": f"{routine_status.get('approved', 0)} routines and {ram_status.get('approved', 0)} RAM labels approved; "
                   f"{routine_status.get('rework', 0)} sent back"},
        {"name": "Benchmark", "state": "active" if benches else "todo",
         "detail": (f"latest: code/data split {benches[-1]['split']:.0%} agreement, names {benches[-1]['names']:.0%} match"
                    if benches else "no reference configured" if not reference else "not run yet")},
    ]
    return {"rom": {"name": rom.name, "mapper": rom.mapper, "prg": prg, "sha1": rom.sha1},
            "next": {"stage": s.recommendation, "reason": s.reason},
            "stages": stages,
            "numbers": {"routines": s.routines, "annotated": s.annotated, "ready": s.ready,
                        "code_bytes": code, "data_bytes": data, "prg": prg, "executed": executed,
                        "ram_labels": ram_status, "routine_notes": routine_status,
                        "experiments": exp_total, "experiments_passed": exp_passed, "evidence": evidence},
            "build": json.loads(kb.setting("last_build") or "null"), "benchmarks": benches,
            "annotated_curve": curve,
            "runs": [{"id": i, "kind": k or "fuzz", "started": st, "seconds": w, "frames": f, "run_code": rc,
                      "new_code": nc, "total": total}
                     for i, k, st, w, f, rc, nc, total in kb.db.execute(
                         "SELECT id, kind, started, wall_seconds, frames, run_code, new_code, code_bytes FROM runs "
                         "WHERE frames IS NOT NULL ORDER BY id DESC LIMIT 12")],
            "rounds": [{**r, "gains": None if r["gains"] is None
                                                  else [f"+{n} {cycle.METRICS[k]}" for k, n in r["gains"].items()]}
                       for r in cycle.rounds(kb, 12)],
            "research_mode": kb.setting("research_mode") or ("benchmark" if reference else "open")}


def asm_view(kb: KB, since: str = "") -> dict:
    """The annotated disassembly rendered from the knowledge base right now (what the next build
    writes), with each label's address, kind, status and review item. `since` = an earlier version:
    an unchanged text is not sent again."""
    import hashlib
    from .render import render

    text = render(kb).asm
    version = hashlib.sha1(text.encode()).hexdigest()[:12]
    build = json.loads(kb.setting("last_build") or "null")
    if since == version:
        return {"version": version, "unchanged": True, "build": build}
    labels = {}
    for bank in annotate.banks(kb):
        notes = {e for e, *_ in kb.routine_notes(bank)}
        for l in kb.labels(bank):
            review = routine_id(bank, l.addr) if l.kind in ("routine", "vector") and l.addr in notes else None
            labels[l.name] = [l.addr, l.kind, l.status, l.confidence, review]
    for l in kb.labels(NON_ROM):
        labels[l.name] = [l.addr, l.kind, l.status, l.confidence, f"m{l.addr}" if l.kind == "ram" else None]
    return {"version": version, "text": text, "labels": labels, "build": build}


BRIEF_KINDS = ("mechanic", "vocabulary", "level", "event", "control", "context", "other")


def research(kb: KB) -> dict:
    from . import codes

    facts = [{"id": i, "kind": k, "text": t, "source": src, "status": st or "proposed", "author": a or "claude",
              "created": c} for i, k, t, src, st, a, c in
             kb.db.execute("SELECT id, kind, text, source, status, author, created FROM brief ORDER BY kind, id")]
    reference = kb.setting("reference")
    return {"mode": kb.setting("research_mode") or ("benchmark" if reference else "open"), "facts": facts,
            "kinds": BRIEF_KINDS,
            "codes": [c for c in codes.research_listing(kb).splitlines() if not c.startswith("(no ")],
            "test_codes": kb.db.execute("SELECT count(*) FROM codes WHERE use='test'").fetchone()[0]}


def brief_action(kb: KB, action: str, fact_id: int | None = None, text: str = "", kind: str = "context",
                 source: str = "") -> dict:
    """The human's review of the research brief. Only accepted facts reach the annotator."""
    if action in ("accept", "reject") and fact_id is not None:
        kb.db.execute("UPDATE brief SET status=? WHERE id=?", ("accepted" if action == "accept" else "rejected", fact_id))
        message = f"fact #{fact_id} {action}ed"
    elif action == "accept_all":
        n = kb.db.execute("UPDATE brief SET status='accepted' WHERE status IS NULL OR status='proposed'").rowcount
        message = f"accepted {n} facts; the loop can continue"
    elif action == "edit" and fact_id is not None:
        if not text.strip():
            return {"ok": False, "message": "the text is empty"}
        kb.db.execute("UPDATE brief SET text=?, status='accepted' WHERE id=?", (text.strip()[:600], fact_id))
        message = f"fact #{fact_id} edited and accepted"
    elif action == "add":
        if not text.strip():
            return {"ok": False, "message": "the text is empty"}
        if kind not in BRIEF_KINDS:
            return {"ok": False, "message": f"kind must be one of {', '.join(BRIEF_KINDS)}"}
        kb.db.execute("INSERT INTO brief (kind, text, source, created, status, author) VALUES (?,?,?,?,'accepted','user')",
                      (kind, text.strip()[:600], source.strip() or "added by the reviewer", _now()))
        message = "added your note to the brief"
    elif action == "delete" and fact_id is not None:
        kb.db.execute("DELETE FROM brief WHERE id=? AND author='user'", (fact_id,))
        message = f"deleted your note #{fact_id}"
    else:
        return {"ok": False, "message": f"unknown action {action}"}
    kb.commit()
    return {"ok": True, "message": message}


def activity(kb: KB) -> dict:
    tasks = []
    for task, done, total, note, started, updated, finished in kb.db.execute(
            "SELECT task, done, total, note, started, updated, finished FROM progress ORDER BY updated DESC LIMIT 12"):
        age = _since(updated)
        state = "finished" if finished else ("running" if age is not None and age < 600 else "stalled")
        tasks.append({"task": task, "done": done, "total": total, "note": note, "started": started,
                      "updated": updated, "finished": finished, "state": state})
    commands = [{"command": c, "args": a, "started": s, "finished": f, "exit_code": e, "summary": sm, "agent": ag,
                 "running": f is None and (_since(s) or 0) < 7200}
                for c, a, s, f, e, sm, ag in kb.db.execute(
                    "SELECT command, args, started, finished, exit_code, summary, agent FROM activity ORDER BY id DESC LIMIT 40")]
    names = {(l.bank, l.addr): l.name for b in annotate.banks(kb) for l in kb.labels(b)}
    events = []
    for bank, entry, updated, status in kb.db.execute(
            "SELECT bank, entry, updated, status FROM routine_notes WHERE bank>=0 ORDER BY updated DESC LIMIT 15"):
        events.append({"t": updated, "kind": "annotation", "text": f"{names.get((bank, entry), hex(entry))} ({status})"})
    for created, name, passed in kb.db.execute("SELECT created, name, passed FROM experiments ORDER BY id DESC LIMIT 10"):
        events.append({"t": created, "kind": "experiment", "text": f"{'PASS' if passed else 'FAIL'}: {name}"})
    for created, action, value in kb.db.execute("SELECT created, action, value FROM reviews ORDER BY id DESC LIMIT 10"):
        events.append({"t": created, "kind": "review", "text": f"{action} {value or ''}".strip()})
    for created, results in kb.db.execute("SELECT created, results FROM benchmarks ORDER BY id DESC LIMIT 5"):
        r = json.loads(results)
        match = r["naming"]["all"]["match_rate"]
        events.append({"t": created, "kind": "benchmark",
                       "text": f"names match {match:.0%}" if match is not None else "benchmark run"})
    from . import cycle
    for r in cycle.rounds(kb, 10):
        if r["finished"]:
            events.append({"t": r["finished"], "kind": "round", "text": cycle.format_round(r)})
    events.sort(key=lambda e: e["t"] or "", reverse=True)
    return {"tasks": tasks, "commands": commands, "events": events[:40]}
