"""M7: review decisions, name propagation, hints in work units, game brief, Game Genie codes."""
import json

import pytest

from conftest import make_nes, make_prg
from renes import analyze, annotate, codes, review, rom as romlib
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
        kb.add_label(Label(NON_ROM, 0x10, "Frame_Tally", "ram", "candidate", "claude", 0.6))
        kb.add_comment(0, 0x8010, "line", "Frame_Tally += 1", "candidate", "claude")
        kb.commit()
        yield kb


def test_items_put_low_confidence_candidates_first_and_detail_has_code(kb):
    its = review.items(kb)
    assert its[0]["id"] == f"r{0x8010}" and "low confidence" in its[0]["flags"]
    d = review.detail(kb, f"r{0x8010}")
    assert d["name"] == "BumpCounter" and any("inc Frame_Tally" in l["text"] for l in d["lines"])
    assert review.detail(kb, f"m{0x10}")["used_by"] == ["NMI", "BumpCounter"]     # in address order


def test_rename_is_approved_and_propagates_into_summaries_and_comments(kb):
    r = review.decide(kb, f"m{0x10}", "rename", "Frame_Counter")
    assert r["ok"], r
    assert kb.label_at(NON_ROM, 0x10).name == "Frame_Counter" and kb.label_at(NON_ROM, 0x10).status == "approved"
    assert kb.routine_notes(0)[0][1] == "Adds one to Frame_Counter each call."
    assert kb.comments(0)[0x8010][0][1] == "Frame_Counter += 1"
    assert not review.decide(kb, f"m{0x10}", "rename", "lda")["ok"]          # reserved word
    assert kb.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1


def test_propagation_only_replaces_whole_words(kb):
    kb.set_routine_note(0, 0x8000, "Uses Frame_Tally and Frame_Tally_Hi.", "candidate", "claude", 0.5, "t")
    kb.propagate_name("Frame_Tally", "Ticks")
    assert dict((e, s) for e, s, *_ in kb.routine_notes(0))[0x8000] == "Uses Ticks and Frame_Tally_Hi."


def test_hint_sends_routine_back_to_the_front_of_the_queue(kb):
    assert review.decide(kb, f"r{0x8010}", "hint", "It counts frames, not tallies.")["ok"]
    ctx = annotate.Context.load(kb)
    assert annotate.queue(ctx)[0] == 0x8010 and 0x8010 in ctx.rework
    unit = annotate.work_unit(ctx, 0x8010)
    assert "Sent back by the reviewer" in unit and "It counts frames, not tallies." in unit


def test_approve_locks_and_reject_removes_a_ram_label(kb):
    review.decide(kb, f"r{0x8010}", "approve")
    assert kb.label_at(0, 0x8010).status == "approved"
    assert kb.routine_notes(0)[0][2] == "approved"
    review.decide(kb, f"m{0x10}", "reject", "it is not a tally")
    assert kb.label_at(NON_ROM, 0x10) is None
    assert kb.hints(NON_ROM, 0x10)[0][0].startswith("rejected name Frame_Tally")


def test_brief_facts_show_up_in_matching_work_units(kb):
    kb.db.execute("INSERT INTO brief (kind, text, source, created, status) VALUES ('mechanic', 'The frame tally resets at 100.', 'manual p.3', 't', 'accepted')")
    kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES ('mechanic', 'The frame tally is unreviewed.', 'x', 't')")
    kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES ('mechanic', 'Unrelated fact about secret doors.', 'x', 't')")
    unit = annotate.work_unit(annotate.Context.load(kb), 0x8010)
    assert "The frame tally resets at 100." in unit and "secret doors" not in unit
    assert "unreviewed" not in unit                      # only facts the human accepted reach the annotator


def test_game_genie_decoding():
    g = codes.decode("GOSSIP")                     # the classic documentation example
    assert (g.address, g.value, g.compare) == (0xD1DD, 0x14, None)
    for addr, val, cmp in ((0x8000, 0x00, None), (0xFFFF, 0xFF, 0xAB), (0x9C37, 0x5A, 0x00)):
        d = codes.decode(codes.encode(addr, val, cmp))
        assert (d.address, d.value, d.compare) == (addr, val, cmp)
    with pytest.raises(codes.CodeError):
        codes.decode("HELLO1")


def test_test_codes_are_scored_without_showing_descriptions(kb, tmp_path):
    inc_code = codes.encode(0x8010, 0xEA, 0xE6)        # patch 'inc' (opcode $E6) at $8010
    other = codes.encode(0x8000, 0xEA, 0x99)           # compare byte does not match this ROM
    added, problems = codes.add_codes(kb, [f"{inc_code} Stop the frame tally from counting",
                                           f"{other} Something else", "BADCODE nope"], "test list", "test")
    assert added == 2 and problems
    result = codes.evaluate(kb, tmp_path / "details.md")
    assert result["codes"] == 2 and result["apply_to_this_rom"] == 1 and result["in_annotated_routines"] == 1
    assert result["description_overlap_mean"] > 0
    assert "Stop the frame tally" not in str(result) and "Stop the frame tally" in (tmp_path / "details.md").read_text()
    assert codes.research_listing(kb) == "(no research codes)"


@__import__("conftest").needs_mesen
def test_evidence_traces_and_knocks_out_a_boot_routine(tmp_path):
    from conftest import EXAMPLE_ROM, rom_fact
    from renes import evidence
    from renes.cli import main

    boot = rom_fact("boot_routine")                # a routine the reset code calls in the first 30 frames
    kb_path = tmp_path / "example.db"
    assert main(["init", str(EXAMPLE_ROM), "--kb", str(kb_path)]) == 0
    with KB(kb_path) as kb:
        entries = [e for e, _, _ in kb.routines(0)]
        assert boot in entries                     # static analysis finds it from reset
    r = evidence.collect(kb_path, [boot], frames=30, log=lambda *_: None)
    assert r.reached == r.traced == r.knocked == 1
    with KB(kb_path) as kb:
        rows = kb.db.execute("SELECT rows FROM traces WHERE entry=?", (boot,)).fetchone()
        assert rows and f'"pc": {boot}' in rows[0]   # the first traced instruction is the entry
        assert len(kb.media("knockout", kb.db.execute("SELECT id FROM knockouts").fetchone()[0])) == 4


def test_dashboard_research_gate_and_activity(kb):
    kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES ('mechanic', '100 coins give a life', 'manual', 't')")
    kb.commit()
    r = review.research(kb)
    assert r["facts"][0]["status"] == "proposed" and r["mode"] == "open"
    assert not review.brief_action(kb, "add", text="  ")["ok"]
    assert review.brief_action(kb, "add", text="Focus on water levels", kind="context")["ok"]
    assert {f["status"] for f in review.research(kb)["facts"]} == {"proposed", "accepted"}
    assert review.brief_action(kb, "accept_all")["ok"]
    assert {f["status"] for f in review.research(kb)["facts"]} == {"accepted"}
    kb.set_progress("evidence", 3, 10, "working")
    a = review.activity(kb)
    assert a["tasks"][0]["state"] == "running" and a["tasks"][0]["done"] == 3
    o = review.overview(kb)
    assert o["numbers"]["annotated"] == 1 and any(s["name"] == "Research" and s["state"] == "done" for s in o["stages"])


def test_server_refuses_foreign_hosts_and_non_json_posts(kb, tmp_path):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    kb_path = tmp_path / "t.db"
    server = ThreadingHTTPServer(("127.0.0.1", 0), review.make_handler(kb_path, lambda path: {"ok": True, "message": ""}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, host, body=None, ctype=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": host}
        if ctype:
            headers["Content-Type"] = ctype
        c.request(method, path, body=body, headers=headers)
        return c.getresponse().status

    try:
        assert call("GET", "/api/items", f"127.0.0.1:{port}") == 200
        assert call("GET", "/api/items", "renes.localhost") == 200
        assert call("GET", "/api/items", "evil.example") == 421
        body = '{"action": "accept_all"}'
        assert call("POST", "/api/brief", "renes.localhost", body, "text/plain") == 415
        assert call("POST", "/api/brief", "renes.localhost", body, "application/json") == 200
    finally:
        server.shutdown()


def test_rounds_record_duration_and_progress_and_detect_stalls(kb):
    from renes import cycle

    s = cycle.status(kb)
    s.recommendation = "annotate"
    kb.db.execute("UPDATE routine_notes SET updated='2026-01-01T00:00:00+00:00'")    # annotated before the rounds
    cycle.start_round(kb, s)
    kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES ('mechanic', 'x', 'manual', '2026')")
    r = cycle.end_round(kb, "added a fact")
    assert r["gains"] == {"brief": 1} and r["seconds"] is not None and r["note"] == "added a fact"
    assert cycle.idle_rounds(kb) == []
    for _ in range(2):
        cycle.start_round(kb, s)
        cycle.end_round(kb)
    assert cycle.idle_rounds(kb) == [s.recommendation] * 2
    kb.set_setting("loop_resumed", "9999")                   # `round resume` clears the stall
    assert cycle.idle_rounds(kb) == []
    assert review.overview(kb)["rounds"][0]["gains"] == []
    assert cycle.end_round(kb) is None


def test_code_view_renders_live_and_skips_unchanged_text(kb):
    v = review.asm_view(kb)
    assert "BumpCounter:" in v["text"] and v["labels"]["BumpCounter"][4] == f"r{0x8010}"
    assert v["labels"]["Frame_Tally"][1] == "ram"
    again = review.asm_view(kb, since=v["version"])
    assert again["unchanged"] and "text" not in again
    kb.add_label(Label(0, 0x8010, "CountFrames", "routine", "candidate", "claude", 0.5), replace=True)
    assert review.asm_view(kb, since=v["version"])["version"] != v["version"]


def test_dashboard_serves_every_project_in_a_folder(kb, tmp_path):
    import http.client
    import shutil
    import threading
    from http.server import ThreadingHTTPServer

    kb.commit()
    shutil.copy(tmp_path / "t.db", tmp_path / "second.db")
    shutil.copy(tmp_path / "t.db", tmp_path / "old_backup.db")          # backups are not projects
    assert set(review.find_projects(tmp_path)) == {"t", "second"}
    built = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), review.make_handler(tmp_path, lambda path: built.append(path.stem) or {"ok": True}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=body, headers={"Host": "localhost", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    try:
        status, d = call("GET", "/api/projects")
        assert status == 200 and {c["id"] for c in d["projects"]} == {"t", "second"}
        assert all(c["routines"] >= 1 and "next" in c for c in d["projects"])
        assert call("GET", "/api/overview?p=second")[1]["project"] == "second"
        assert call("GET", "/api/items?p=t")[1]["project"] == "t"
        for path in ("/api/rommap", "/api/rammap", "/api/graph"):
            status, d = call("GET", path + "?p=second")
            assert status == 200 and d["project"] == "second"
        call("POST", "/api/build?p=second", "{}")
        assert built == ["second"]
    finally:
        server.shutdown()


def test_open_questions_board_groups_what_is_unexplained(kb):
    from renes import confidence, questions

    kb.add_experiment("t", NON_ROM, 0x10, "tally", json.dumps({"alternative": "x"}), False, "{}")
    kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES ('mechanic', 'x', 'manual', 't')")
    confidence.refresh(kb)
    groups = {g["id"]: g for g in questions.questions(kb)}
    assert set(groups) >= {"failed", "overconfident", "placeholder", "tables", "never_ran", "unknown", "busy_ram", "you"}
    assert groups["failed"]["items"][0]["text"] == "Frame_Tally" and groups["failed"]["items"][0]["link"]["target"] == f"m{0x10}"
    assert groups["never_ran"]["count"] == 1                     # BumpCounter: no CDL in this fixture
    assert groups["you"]["items"][0]["text"] == "1 research facts"


def test_one_loop_per_project_claims(kb, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from renes import claims

    monkeypatch.delenv("RENES_AGENT", raising=False)
    assert claims.claim(kb, "loop-a")[0] and claims.owner(kb)["agent"] == "loop-a"
    ok, message = claims.claim(kb, "other")
    assert not ok and "claimed by loop-a" in message
    ok, message = claims.claim(kb, "other", force=True)
    assert ok and "took over from loop-a, forced" in message
    stale = (datetime.now(timezone.utc) - claims.TTL - timedelta(minutes=1)).isoformat(timespec="seconds")
    kb.set_setting(claims.SETTING, json.dumps({"agent": "gone", "since": stale, "seen": stale}))
    ok, message = claims.claim(kb, "fresh")
    assert ok and "took over from gone" in message
    assert not claims.release(kb, "someone")[0] and claims.release(kb, "fresh")[0] and claims.owner(kb) is None


def test_round_start_refuses_a_project_claimed_by_another_agent(kb, tmp_path, capsys, monkeypatch):
    from renes import claims
    from renes.cli import main

    monkeypatch.delenv("RENES_AGENT", raising=False)
    claims.claim(kb, "first")
    kb.commit()
    path = str(tmp_path / "t.db")
    assert main(["round", "start", path, "--agent", "second"]) == 3
    assert "claimed by first" in capsys.readouterr().out
    assert main(["round", "start", path, "--agent", "first"]) == 0
    with KB(path) as k2:
        assert k2.db.execute("SELECT agent FROM rounds ORDER BY id DESC").fetchone()[0] == "first"


def test_feedback_reply_reaches_the_agent_once_at_round_start(kb, tmp_path, capsys, monkeypatch):
    from renes import cli, feedback

    monkeypatch.delenv("RENES_AGENT", raising=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "work").mkdir()
    kb.commit()
    path = str(tmp_path / "t.db")
    assert cli.main(["feedback", "add", "--kind", "limit", "--project", path, "--agent", "a", "no shared freeze"]) == 0
    assert "no shared freeze" in feedback.format_items(feedback.items(tmp_path / "work"))
    assert cli.main(["feedback", "close", "--id", "1", "added \"shared\""]) == 0
    assert feedback.items(tmp_path / "work") == []                  # closed notes leave the open list
    capsys.readouterr()
    cli.main(["round", "start", path, "--agent", "a"])
    assert 'reply to your feedback #1 (done): added "shared"' in capsys.readouterr().out
    cli.main(["round", "end", path, "--agent", "a"])
    cli.main(["round", "start", path, "--agent", "a"])
    assert "reply to your feedback" not in capsys.readouterr().out   # shown once


def test_feedback_wait_wakes_the_developer_on_new_notes_and_the_agent_on_replies(tmp_path):
    from renes import feedback

    work = tmp_path
    feedback.add(work, "bug", "old note", "a", "game")
    ticks = []

    def sleep(_):                     # the other side acts while the waiter sleeps
        ticks.append(1)
        if len(ticks) == 2:
            feedback.add(work, "limit", "new note", "a", "game")
        if len(ticks) == 4:
            feedback.close(work, 2, "fixed")

    [note] = feedback.wait(work, sleep=sleep)                       # old open notes don't wake it
    assert note["text"] == "new note" and len(ticks) == 2
    [reply] = feedback.wait(work, project="game", sleep=sleep)
    assert reply["reply"] == "fixed" and len(ticks) == 4
    assert feedback.wait(work, project="other", timeout=0) == []     # timed out


def test_never_ran_claims_are_flagged_once_the_code_has_run(kb):
    from renes import annotate, rework

    kb.set_routine_note(0, 0x8010, "Adds one to Frame_Tally; never ran during exploration.", "candidate",
                        "claude", 0.4, "t")
    ctx = annotate.Context.load(kb)
    assert rework.stale_claims(kb, ctx) == []                      # still true: nothing executed
    ctx.executed = lambda addr: True
    assert [(name, where) for _, name, _, where in rework.stale_claims(kb, ctx)] == [("BumpCounter", 'summary "never ran"')]
    kb.set_routine_note(0, 0x8010, "Adds one to Frame_Tally.", "candidate", "claude", 0.4, "t")
    kb.add_comment(0, 0x8010, "line", "unused path", "candidate", "claude")
    ctx.notes = annotate.Context.load(kb).notes
    assert rework.stale_claims(kb, ctx)[0][3] == 'line comment $8010 "unused"'


def test_a_moved_evidence_movie_is_found_under_roms_tas(tmp_path):
    from pathlib import Path

    from renes import evidence

    tas = tmp_path / "tas"
    (tas / "1234M").mkdir(parents=True)
    (tas / "1234M" / "run.bk2").write_bytes(b"x")
    assert evidence.find_movie(Path(r"C:\old\movies\1234M\run.bk2"), tas) == tas / "1234M" / "run.bk2"
    assert evidence.find_movie(Path(r"C:\old\elsewhere\run.bk2"), tas) == tas / "1234M" / "run.bk2"
    assert evidence.find_movie(Path(r"C:\old\x\other.bk2"), tas) is None


def test_feedback_text_can_come_from_a_file(tmp_path, monkeypatch):
    from renes import cli, feedback

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "work").mkdir()
    note = tmp_path / "note.txt"
    note.write_text('poke "$0125" -- then --goal fails', encoding="utf-8")
    assert cli.main(["feedback", "add", "--kind", "idea", "--file", str(note)]) == 0
    assert feedback.items(tmp_path / "work")[0]["text"] == 'poke "$0125" -- then --goal fails'


def test_acks_reach_the_reporting_project_and_patch_notes_reach_everyone_once(tmp_path):
    from renes import feedback

    work = tmp_path
    feedback.add(work, "bug", "apply drops sizes", "a", "game_a")
    feedback.add(work, "idea", "probe diff", "b", "game_b")
    assert feedback.inbox(work, "newgame") == []                     # registers newgame: no old news later
    feedback.ack(work, [1], "looking at apply")
    assert [n["status"] for n in feedback.items(work)] == ["working", "open"]   # still listed as open
    feedback.patch(work, "apply reports size changes", [1])
    pin = feedback.format_messages(feedback.inbox(work, "game_a"))
    assert "working on your notes #1: looking at apply" in pin and "patch note 2 (fixes #1)" in pin
    assert [m["kind"] for m in feedback.inbox(work, "game_b")] == ["patch"]   # not the other project's ack
    assert [m["kind"] for m in feedback.inbox(work, "newgame")] == ["patch"]
    assert feedback.inbox(work, "game_a") == []                     # shown once
    assert feedback.items(work, everything=True)[0]["reply"].startswith("fixed in patch note 2")
    with pytest.raises(ValueError):
        feedback.ack(work, [99], "no such note")


def test_states_lists_archived_states_matching_ram_values(kb, tmp_path, capsys):
    from renes.cli import main

    for sid, mode in ((1, 0), (2, 1), (3, 1)):
        ram = bytearray(0x800)
        ram[0x10] = mode
        kb.db.execute("INSERT INTO states (id, run_id, cell, frame, new_code, visits, inputs, ram, savestate) "
                      "VALUES (?, 1, '', ?, 0, 0, '', ?, x'')", (sid, sid * 100, bytes(ram)))
    kb.commit()
    assert main(["states", str(tmp_path / "t.db"), "frame_tally=1"]) == 0     # labels match any case
    out = capsys.readouterr().out
    assert out.startswith("2 of 3 archived states") and "   300 " in out
    assert main(["states", str(tmp_path / "t.db"), "$0010=2"]) == 1


def test_states_of_retired_runs_are_left_out(kb, tmp_path, capsys):
    from renes.cli import main
    from renes.experiment import Lab, SpecError

    for run, started in ((1, "2026-10-20T10:00:00+00:00"), (2, "2026-10-22T00:31:30+00:00")):
        kb.db.execute("INSERT INTO runs (id, started) VALUES (?, ?)", (run, started))
        kb.db.execute("INSERT INTO states (id, run_id, cell, frame, new_code, visits, inputs, ram, savestate) "
                      "VALUES (?, ?, '', 0, 0, 0, '', x'00', x'')", (run, run))
    kb.commit()
    db = str(tmp_path / "t.db")
    assert main(["states", db, "--retire", "input-bug"]) == 0
    assert "retired runs 2 (1 archived states)" in capsys.readouterr().out
    main(["states", db])
    assert capsys.readouterr().out.startswith("1 of 1 archived states (1 from retired runs left out)")
    lab = Lab.__new__(Lab)                       # no emulator: the state is refused before it loads
    with KB(tmp_path / "t.db") as lab.kb, pytest.raises(SpecError, match="retired run"):
        lab.m, lab.shared = None, {}
        lab.reach({"state": 2})
    assert main(["states", db, "--restore", "2"]) == 0
    main(["states", db])
    assert "\n2 of 2 archived states\n" in capsys.readouterr().out


def test_audit_round_trip_stores_aggregates_only(kb, tmp_path):
    from renes import audit

    kb.commit()
    path, out = tmp_path / "t.db", tmp_path / "audit"
    s = audit.prepare(path, out, routines=5, ram=5)
    assert s == {"items": 2, "routines": 1, "ram": 1}
    items = json.loads((out / "packet.json").read_text())["items"]
    routine = next(i for i in items if i["type"] == "routine")
    assert routine["name"] == "BumpCounter" and any("Frame_Tally" in line for line in routine["code"])
    assert all("code" in x and x["in"] for x in routine["leads_to"] if "addr" in x)
    ram = next(i for i in items if i["type"] == "ram")
    assert ram["name"] == "Frame_Tally" and ram["uses"]
    whole = (out / ram["uses"][0]["listing"]).read_text()
    assert all(line in whole for line in ram["uses"][0]["lines"])
    (out / "verdicts.json").write_text(json.dumps([{"id": routine["id"], "verdict": "supported"},
                                                   {"id": ram["id"], "verdict": "plausible"}]))
    results = audit.record(path, out)
    assert results["all"]["supported_rate"] == 0.5 and results["all"]["consistent_rate"] == 1.0
    assert results["all"]["plausible_rate"] == 0.5 and "plausible 50%" in audit.format_results(results)
    with KB(path) as k2:
        assert "BumpCounter" not in json.dumps(audit.latest(k2))


def test_unsupported_audit_items_go_back_with_the_reason_once(kb, tmp_path):
    from renes import audit

    kb.commit()
    path, out = tmp_path / "t.db", tmp_path / "audit"
    audit.prepare(path, out, routines=5, ram=5)
    items = json.loads((out / "packet.json").read_text())["items"]
    routine = next(i for i in items if i["type"] == "routine")
    verdicts = [{"id": i["id"], "verdict": "unsupported" if i is routine else "supported",
                 "why": "never stores to Frame_Tally" if i is routine else ""} for i in items]
    (out / "verdicts.json").write_text(json.dumps(verdicts))
    assert audit.record(path, out)["sent_back"] == 1
    with KB(path) as k2:
        assert k2.db.execute("SELECT status FROM routine_notes WHERE entry=?", (0x8010,)).fetchone()[0] == "rework"
        unit = annotate.work_unit(annotate.Context.load(k2), 0x8010)
        assert "independent audit" in unit and "never stores to Frame_Tally" in unit
    assert audit.record(path, out)["sent_back"] == 0                      # the same reason only once
    assert audit.prepare(path, out, like=out / "packet.json")["items"] == len(items)   # --repeat: same items


@pytest.mark.skipif(__import__("shutil").which("ca65") is None and not (__import__("renes.verify", fromlist=["CC65_BIN"]).CC65_BIN / "ca65.exe").exists(), reason="cc65 missing")
def test_finishing_checks_and_export(kb, tmp_path):
    from renes import cycle, finish

    kb.set_setting("last_build", json.dumps({"ok": True, "message": "byte-identical"}))
    kb.set_setting("research_done", "t")
    note_all = [e for e, _, _ in kb.routines(0)]
    for e in note_all:
        kb.set_routine_note(0, e, f"Routine at ${e:04X}.", "candidate", "claude", 0.6, "2026-01-01T00:00:00+00:00")
    kb.commit()
    s = cycle.status(kb)
    names = {c.name: c for c in s.finish}
    assert names["annotated"].ok and not names["audit"].ok and names["audit"].todo == "audit"
    assert s.recommendation in ("probe", "evidence", "audit")          # the loop works through the checks
    target = finish.export(tmp_path / "t.db", tmp_path / "out")
    assert {"README.md", "routines.md", "ram.md", "t.asm", "t.cfg"} <= {p.name for p in target.iterdir()}
    assert not (target / "t.nes").exists() and "Frame_Tally" in (target / "ram.md").read_text()


def test_an_audit_stays_current_until_enough_routines_change(kb):
    from renes import audit, finish

    kb.db.execute(audit.SCHEMA)
    kb.db.execute("INSERT INTO audits (created, sample, results) VALUES (?, 60, ?)",
                  ("2026-01-02T00:00:00+00:00", json.dumps({"audited": 60, "all": {"supported_rate": 0.8}})))

    def audit_check(changed: int):
        kb.db.execute("DELETE FROM routine_notes")
        for i in range(changed):
            kb.set_routine_note(0, 0x9000 + i, "x", "candidate", "claude", 0.6, "2026-01-03T00:00:00+00:00")
        return next(c for c in finish.checklist(kb) if c.name == "audit")

    assert audit_check(3).todo != "audit" and "3 routines re-annotated since" in audit_check(3).detail
    assert audit_check(finish.AUDIT_STALE).todo == "audit"
