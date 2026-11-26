"""M6: Stage B plan parsing, policies (Jev with a fake transport, never the network), and a short run."""
import json
import random

import pytest

from conftest import EXAMPLE_ROM, needs_mesen, rom_fact
from renes import agent
from renes.agent import (BUILTINS, JevPolicy, NoveltyPolicy, Outcome, PlanError, Stats, View, default_plan,
                         jev_state, parse_plan, plan_json)
from renes.kb import KB, NON_ROM, Label


@pytest.fixture
def kb(tmp_path):
    with KB(tmp_path / "a.db") as kb:
        kb.add_label(Label(NON_ROM, 0x86, "Player_X", "ram", "verified", "claude"))
        kb.add_label(Label(NON_ROM, 0x6D, "Player_Page", "ram", "candidate", "claude"))
        kb.add_label(Label(NON_ROM, 0x200, "Sprites", "ram", "verified", "claude", size=256))
        yield kb


def test_default_plan_watches_verified_single_bytes_and_parses(kb):
    data = default_plan(kb)
    assert data["watch"] == ["Player_X"]
    plan = parse_plan(json.loads(plan_json(data)), kb)
    assert plan.watch == {"Player_X": 0x86}
    assert set(BUILTINS) <= set(plan.menu()) and not set(BUILTINS) & set(plan.menu(builtins=False))


def test_plan_resolves_labels_addresses_and_progress(kb):
    plan = parse_plan({"actions": {"go": {"inputs": [[10, "right"]]}}, "watch": ["Player_Page", "$0009"],
                       "progress": {"lo": "Player_X", "hi": "$006D"}, "freeze": {"$075A": 3}}, kb)
    assert plan.watch == {"Player_Page": 0x6D, "ram_0009": 9}
    assert plan.progress == (0x86, 0x6D) and plan.freeze == {0x75A: 3}
    ram = bytearray(0x800)
    ram[0x6D], ram[0x86] = 2, 40
    assert agent._progress(plan, bytes(ram)) == 2 * 256 + 40


@pytest.mark.parametrize("data", [
    {"actions": {}},
    {"actions": {"Bad Id": {"inputs": [[10, "a"]]}}},
    {"actions": {"restore_best": {"inputs": [[10, "a"]]}}},
    {"actions": {"long": {"inputs": [[601, "a"]]}}},
    {"actions": {"go": {"inputs": [[10, "a"]]}}, "watch": ["Nope"]},
    {"actions": {"go": {"inputs": [[10, "a"]]}}, "watch": ["$8000"]},
    {"actions": {"go": {"inputs": [[10, "a"]]}}, "builtins": ["teleport"]},
], ids=["no-actions", "bad-id", "builtin-id", "too-long", "unknown-label", "not-ram", "bad-builtin"])
def test_bad_plans_are_rejected(kb, data):
    with pytest.raises(PlanError):
        parse_plan(data, kb)


def plan_and_view(kb):
    plan = parse_plan({"actions": {"left": {"inputs": [[10, "left"]]}, "right": {"inputs": [[10, "right"]]}},
                       "watch": ["Player_X"]}, kb)
    return plan, View("c1", 3, {"Player_X": 40}, None, None)


def test_novelty_prefers_actions_that_recently_found_something(kb):
    plan, view = plan_and_view(kb)
    stats = Stats()
    for _ in range(4):
        stats.add("c1", Outcome("left", 10, 0, False, None, 0))
        stats.add("c1", Outcome("right", 10, 30, True, None, 0))
    assert NoveltyPolicy(random.Random(0)).choose(plan, view, stats).action == "right"
    assert Stats.reward(Outcome("x", 1, 3000, False, None, 0)) < 5   # bounded: bursts don't dominate


def test_jev_request_is_named_state_only_and_answer_is_used(kb):
    plan, view = plan_and_view(kb)
    sent = []

    def post(url, body, key):
        sent.append(body)
        return 200, {"answers": {"next_action": {"choice": "left", "probabilities": {"left": 0.9}},
                                 "is_stuck": {"noul": 0.2}}, "usage": {"input_tokens": 500}}

    jev = JevPolicy("k", 5, NoveltyPolicy(random.Random(0)), post=post, log=lambda *_: None)
    d = jev.choose(plan, view, Stats())
    assert (d.action, d.policy, d.stuck) == ("left", "jev", 0.2)
    body = sent[0]
    assert set(body["questions"]["next_action"]["criteria"]) == {"left", "right", *BUILTINS}
    assert body["state"]["game"] == {"Player_X": 40} and len(json.dumps(body)) < 4000
    assert jev.usage["input_tokens"] == 500


def test_jev_hides_restores_right_after_a_restore(kb):
    plan, view = plan_and_view(kb)
    stats = Stats(just_restored=True)
    assert set(jev_state(plan, view, stats)) >= {"goal", "game", "recent_actions"}
    jev = JevPolicy("k", 5, NoveltyPolicy(random.Random(0)), post=lambda *a: (200, {}), log=lambda *_: None)
    assert set(jev.body(plan, view, stats)["questions"]["next_action"]["criteria"]) == {"left", "right"}


def test_jev_falls_back_on_errors_budget_and_bad_choices(kb):
    plan, view = plan_and_view(kb)
    waits = []
    replies = iter([(529, "busy"), (200, {"answers": {"next_action": {"choice": "teleport"}}}),
                    (402, "insufficient credit")])
    jev = JevPolicy("k", 3, NoveltyPolicy(random.Random(0)), post=lambda *a: next(replies),
                    sleep=waits.append, log=lambda *_: None)
    d = jev.choose(plan, view, Stats())                # 529 retried, then an unknown choice
    assert d.policy == "novelty" and waits == [1] and "teleport" in d.note
    d = jev.choose(plan, view, Stats())                # 402: Jev is switched off for the run
    assert d.policy == "novelty" and "402" in jev.disabled
    assert jev.choose(plan, view, Stats()).note == "jev off" and jev.calls == 2

    budget = JevPolicy("k", 0, NoveltyPolicy(random.Random(0)), post=lambda *a: 1 / 0, log=lambda *_: None)
    assert budget.choose(plan, view, Stats()).note == "jev budget"


@needs_mesen
def test_short_agent_run_records_decisions_states_and_report(tmp_path):
    from renes import rom as romlib

    x_lo, x_hi = rom_fact("player_x"), rom_fact("player_x_page")
    kb_path = tmp_path / "example.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.load(EXAMPLE_ROM))
        kb.add_label(Label(NON_ROM, x_lo, "Object_X_Position", "ram", "verified", "claude"))
        kb.add_label(Label(NON_ROM, x_hi, "Object_X_Page", "ram", "candidate", "claude"))
        data = default_plan(kb)
        data["progress"] = {"lo": "Object_X_Position", "hi": "Object_X_Page"}
        data["restore_after"] = 5
        plan = agent.parse_plan(data, kb)
    r = agent.run_agent(kb_path, plan, "novelty", steps=20, seed=2, log=lambda *_: None)
    assert r.steps == 20 and r.run_code > 5000 and r.new_code == r.run_code   # empty KB: all code is new
    with KB(kb_path) as kb:
        assert kb.db.execute("SELECT count(*) FROM decisions WHERE run_id=?", (r.run_id,)).fetchone()[0] == 20
        assert kb.db.execute("SELECT count(*) FROM states WHERE run_id=?", (r.run_id,)).fetchone()[0] >= 1
        kind, = kb.db.execute("SELECT kind FROM runs WHERE id=?", (r.run_id,)).fetchone()
    assert kind == "agent:novelty"
    text = agent.report(kb_path)
    assert f"run {r.run_id} (agent:novelty)" in text and "## Actions" in text and "Best progress" in text
