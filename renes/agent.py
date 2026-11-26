"""Stage B explorer (M6): a policy picks macro-actions from a menu, using labeled game state.

Stage A (explore.py) presses random buttons. Stage B plays in *macro-actions* ("run and jump
right", "go back to a frontier state") chosen by a policy that sees the game through the RAM
labels the pipeline has already named:

  novelty  local, free: upper-confidence bound on how much new code each action found here
  random   uniform over the menu; the ablation that shows whether the chooser matters
  jev      TypeSafe Jev System One: a `choice` question over the menu plus an `is_stuck` noul.
           Calls are capped (--max-calls); on budget, credit or network errors the run falls
           back to `novelty` instead of failing

The plan (work/explore_plan.json) holds the goal, the menu, the watched variables, a progress
measure and the start state (with optional pokes/freezes to skip ahead). The coach, Claude in
the /renes-explore skill, rewrites the plan between runs from the report this module writes.

Loop protection: when nothing new happens for `restore_after` steps the agent jumps back to a
frontier state; after `coach_after` steps without anything new it stops and asks for the coach.
"""
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .annotate import parse_addr
from .emu.mesen import Mesen
from .experiment import parse_inputs
from .explore import Archive, CellModel, Entry, _compact, run_metrics
from .kb import KB, NON_ROM

ROOT = Path(__file__).resolve().parent.parent
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_USD_PER_MTOK = 0.042   # input tokens; output is free
MAX_OPTIONS = 255          # Jev `choice` limit
MAX_ACTION_FRAMES = 600
REFIT_EVERY = 40           # steps between cell-model refits
CURVE_EVERY = 25           # steps between full-CDL snapshots for the coverage curve
RECENT = 6                 # recent outcomes shown to the policy
DOOMED_AFTER = 3           # restores of a progress record that led nowhere before it is skipped
STUCK_STREAK = 4           # consecutive "stuck" verdicts (>= 0.85) from the policy before a forced restore
WINDOW = 4                # recent results per action the novelty policy scores
DEFAULT_SETUP = [[60, "none"], [4, "start"], [240, "none"]]

BUILTINS = {
    "restore_frontier": "Go back to a recently discovered state and try something else from there",
    "restore_best": "Go back to the state with the most progress so far",
}
DEFAULT_ACTIONS = {
    "walk_right": ("Hold right for 30 frames", [[30, "right"]]),
    "run_right": ("Hold right and B for 30 frames", [[30, "right+b"]]),
    "jump_right": ("Jump while moving right", [[4, "right"], [24, "right+a"], [8, "right"]]),
    "run_jump_right": ("Long running jump to the right", [[12, "right+b"], [30, "right+a+b"], [8, "right+b"]]),
    "hop": ("Short jump in place", [[10, "a"], [14, "none"]]),
    "walk_left": ("Hold left for 30 frames", [[30, "left"]]),
    "jump_left": ("Jump while moving left", [[4, "left"], [24, "left+a"], [8, "left"]]),
    "down": ("Hold down for 30 frames (duck, enter, climb down)", [[30, "down"]]),
    "up": ("Hold up for 30 frames (climb, enter)", [[30, "up"]]),
    "press_a": ("Tap A, then wait", [[4, "a"], [26, "none"]]),
    "press_b": ("Tap B, then wait", [[4, "b"], [26, "none"]]),
    "wait": ("Press nothing for 30 frames", [[30, "none"]]),
    "press_start": ("Tap Start, then wait (title screens, pause)", [[4, "start"], [40, "none"]]),
    "press_select": ("Tap Select, then wait (menus)", [[4, "select"], [40, "none"]]),
}
ACTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class PlanError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------
# Plan


@dataclass
class Plan:
    goal: str
    start: dict
    actions: dict[str, tuple[str, list[tuple[int, str]]]]   # id -> (description, inputs)
    watch: dict[str, int]                                   # name -> RAM address
    progress: tuple[int, int | None] | None = None          # (lo address, hi address or None)
    poke: dict[int, int] = field(default_factory=dict)
    freeze: dict[int, int] = field(default_factory=dict)
    restore_after: int = 25
    coach_after: int = 150
    builtins: tuple[str, ...] = tuple(BUILTINS)

    def menu(self, builtins: bool = True) -> dict[str, str]:
        extra = {b: BUILTINS[b] for b in self.builtins} if builtins else {}
        return {**{a: d for a, (d, _) in self.actions.items()}, **extra}


def default_plan(kb: KB) -> dict:
    """A generic starting plan: watch every verified/approved one-byte RAM label."""
    watch = [l.name for l in kb.labels(NON_ROM)
             if l.kind == "ram" and l.status in ("verified", "approved") and (l.size or 1) == 1]
    return {
        "goal": "Explore the game: reach code, screens and areas not seen before. Winning is not required.",
        "start": {"setup": DEFAULT_SETUP},
        "poke": {}, "freeze": {},
        "watch": watch,
        "progress": None,
        "actions": {a: {"description": d, "inputs": i} for a, (d, i) in DEFAULT_ACTIONS.items()},
        "builtins": list(BUILTINS),
        "restore_after": 25,
        "coach_after": 150,
    }


def plan_json(data: dict) -> str:
    """indent=2 JSON with innermost lists ([30, "right"]) kept on one line, for easy hand editing."""
    text = json.dumps(data, indent=2)
    return re.sub(r"\[\s+([^\[\]{}]*?)\s+\]",
                  lambda m: "[" + ", ".join(x.strip() for x in m.group(1).split(",")) + "]", text) + "\n"


def _resolve(kb: KB, ref) -> int:
    if isinstance(ref, str) and not ref.strip().startswith(("$", "0x")) and not ref.strip().isdigit():
        label = kb.label_named(ref.strip())
        if label is None or label.bank != NON_ROM:
            raise PlanError(f"unknown RAM label {ref!r}")
        return label.addr
    try:
        addr = parse_addr(ref)
    except ValueError:
        raise PlanError(f"bad address {ref!r}") from None
    if not 0 <= addr < 0x800:
        raise PlanError(f"{ref!r} is not internal RAM ($0000-$07FF)")
    return addr


def parse_plan(data: dict, kb: KB) -> Plan:
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")
    actions = {}
    for aid, a in (data.get("actions") or {}).items():
        if not ACTION_ID.match(aid) or aid in BUILTINS:
            raise PlanError(f"bad action id {aid!r} (lowercase letters, digits, _; not a builtin)")
        try:
            inputs = parse_inputs(a.get("inputs"))
        except (ValueError, TypeError, IndexError) as e:
            raise PlanError(f"action {aid}: bad inputs ({e})") from None
        frames = sum(n for n, _ in inputs)
        if not 1 <= frames <= MAX_ACTION_FRAMES:
            raise PlanError(f"action {aid}: {frames} frames (allowed 1-{MAX_ACTION_FRAMES})")
        actions[aid] = (str(a.get("description") or aid)[:200], inputs)
    if not actions:
        raise PlanError("the plan has no actions")
    builtins = tuple(data.get("builtins", list(BUILTINS)))
    if unknown := set(builtins) - set(BUILTINS):
        raise PlanError(f"unknown builtins {sorted(unknown)}")
    if len(actions) + len(builtins) > MAX_OPTIONS:
        raise PlanError(f"at most {MAX_OPTIONS} actions")

    watch = {}
    for ref in data.get("watch") or []:
        addr = _resolve(kb, ref)
        label = kb.label_at(NON_ROM, addr)
        watch[label.name if label else f"ram_{addr:04X}"] = addr
    progress = data.get("progress")
    if progress:
        if not isinstance(progress, dict) or "lo" not in progress:
            raise PlanError('progress must look like {"lo": <label/addr>, "hi": <label/addr or null>}')
        progress = (_resolve(kb, progress["lo"]), _resolve(kb, progress["hi"]) if progress.get("hi") else None)

    start = data.get("start") or {"setup": DEFAULT_SETUP}
    if "state" in start:
        start = {"state": int(start["state"])}
    else:
        parse_inputs(start.get("setup"))
    return Plan(
        goal=str(data.get("goal") or default_plan(kb)["goal"])[:500],
        start=start, actions=actions, watch=watch, progress=progress or None,
        poke={_resolve(kb, k): int(v) & 0xFF for k, v in (data.get("poke") or {}).items()},
        freeze={_resolve(kb, k): int(v) & 0xFF for k, v in (data.get("freeze") or {}).items()},
        restore_after=max(1, int(data.get("restore_after", 25))),
        coach_after=max(1, int(data.get("coach_after", 150))),
        builtins=builtins,
    )


def load_plan(path: Path, kb: KB) -> Plan:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlanError(f"{path}: {e}") from None
    return parse_plan(data, kb)


# ---------------------------------------------------------------------------------------------
# What the policy sees


@dataclass
class Outcome:
    action: str
    frames: int
    new_code: int      # code bytes first executed (this run) during the action
    new_cell: bool     # reached a RAM "situation" not seen before
    progress: int | None
    gain: int          # progress beyond the best so far (0 if none)

    @property
    def novel(self) -> bool:
        return self.new_code > 0 or self.new_cell or self.gain > 0


@dataclass
class Stats:
    """Per-situation history: what each action did in each cell."""
    tried: dict[tuple[str, str], list[float]] = field(default_factory=dict)  # (cell, action) -> rewards
    recent: list[Outcome] = field(default_factory=list)
    since_novel: int = 0
    total: int = 0
    just_restored: bool = False   # restoring twice in a row is pointless; hide the restores once

    @staticmethod
    def reward(o: Outcome) -> float:
        # Bounded: the first steps of a run "discover" the whole main loop, which says nothing
        # about which action is good. log1p keeps a 3000-byte burst within ~4x a 10-byte find.
        return math.log1p(o.new_code) / 2 + (1.0 if o.new_cell else 0.0) + min(o.gain, 256) / 64

    def add(self, cell: str, o: Outcome) -> None:
        self.tried.setdefault((cell, o.action), []).append(self.reward(o))
        self.recent = (self.recent + [o])[-RECENT:]
        self.since_novel = 0 if o.novel else self.since_novel + 1
        self.total += 1
        self.just_restored = False

    def here(self, cell: str) -> dict[str, list[float]]:
        return {a: r for (c, a), r in self.tried.items() if c == cell}


@dataclass
class View:
    cell: str
    cell_visits: int
    values: dict[str, int]
    progress: int | None
    best: int | None


@dataclass
class Decision:
    action: str
    policy: str
    probabilities: dict[str, float] | None = None
    stuck: float | None = None
    note: str = ""


def jev_state(plan: Plan, view: View, stats: Stats) -> dict:
    """The compact, named state Jev sees. Never raw RAM: only labeled values and outcomes."""
    state = {
        "goal": plan.goal,
        "game": view.values,
        "steps_since_anything_new": stats.since_novel,
        "situation": {"times_seen": view.cell_visits,
                      "actions_tried_here": {a: {"times": len(r), "found_new": sum(1 for x in r if x > 0)}
                                             for a, r in sorted(stats.here(view.cell).items())}},
        "recent_actions": [{"action": o.action, "new_code_bytes": o.new_code, "new_situation": o.new_cell,
                            "progress_gained": o.gain} for o in stats.recent],
    }
    if view.progress is not None:
        state["progress"] = {"now": view.progress, "best_so_far": view.best}
    return state


# ---------------------------------------------------------------------------------------------
# Policies


class NoveltyPolicy:
    name = "novelty"

    def __init__(self, rng: random.Random, explore_weight: float = 1.0):
        self.rng, self.c = rng, explore_weight

    def choose(self, plan: Plan, view: View, stats: Stats) -> Decision:
        here = stats.here(view.cell)
        n_here = sum(len(r) for r in here.values())
        everywhere: dict[str, list[float]] = {}
        for (_, a), r in stats.tried.items():
            everywhere.setdefault(a, []).extend(r)

        def score(a: str) -> float:
            # Only recent results count: what worked 50 steps ago has usually been used up.
            local, anywhere = here.get(a, [])[-WINDOW:], everywhere.get(a, [])[-WINDOW:]
            mean = (sum(local) + 0.5 * sum(anywhere)) / (len(local) + 0.5 * len(anywhere) + 1)
            return mean + self.c * math.sqrt(math.log(n_here + 2) / (len(local) + 1)) + self.rng.random() * 1e-3

        return Decision(max(plan.actions, key=score), self.name)


class RandomPolicy:
    name = "random"

    def __init__(self, rng: random.Random):
        self.rng = rng

    def choose(self, plan: Plan, view: View, stats: Stats) -> Decision:
        return Decision(self.rng.choice(list(plan.menu(not stats.just_restored))), self.name)


def api_key() -> str | None:
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    return None


def http_post(url: str, body: dict, key: str, timeout: float = 30) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


class JevPolicy:
    """Asks Jev; any failure or an exhausted budget hands the step to the fallback policy."""
    name = "jev"
    RETRY = {429, 529}

    def __init__(self, key: str, max_calls: int, fallback, post=http_post, sleep=time.sleep, log=print):
        self.key, self.max_calls, self.fallback = key, max_calls, fallback
        self.post, self.sleep, self.log = post, sleep, log
        self.calls = self.input_tokens = self.output_tokens = 0
        self.latency_ms: list[float] = []
        self.disabled: str | None = None

    @property
    def usage(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "usd": round(self.input_tokens * JEV_USD_PER_MTOK / 1e6, 5),
                "median_ms": round(sorted(self.latency_ms)[len(self.latency_ms) // 2]) if self.latency_ms else None,
                "disabled": self.disabled}

    def body(self, plan: Plan, view: View, stats: Stats) -> dict:
        return {
            "model": JEV_MODEL,
            "state": jev_state(plan, view, stats),
            "questions": {
                "next_action": {
                    "type": "choice",
                    "instructions": "Pick the action most likely to reach parts of the game not seen before "
                                    "(new code, new screens, further progress). Avoid actions that already "
                                    "found nothing in this situation.",
                    "criteria": plan.menu(not stats.just_restored),
                },
                "is_stuck": {
                    "type": "noul",
                    "instructions": "Is the explorer repeating itself without reaching anything new?",
                    "criteria": {"true": "The same actions keep finding nothing new",
                                 "false": "Recent actions still reach new things"},
                },
            },
        }

    def _fallback(self, plan: Plan, view: View, stats: Stats, note: str) -> Decision:
        d = self.fallback.choose(plan, view, stats)
        d.note = note
        return d

    def choose(self, plan: Plan, view: View, stats: Stats) -> Decision:
        if self.disabled:
            return self._fallback(plan, view, stats, "jev off")
        if self.calls >= self.max_calls:
            self.disabled = f"budget of {self.max_calls} calls used"
            self.log(f"  Jev: {self.disabled}; continuing with {self.fallback.name}")
            return self._fallback(plan, view, stats, "jev budget")
        body = self.body(plan, view, stats)
        for attempt in range(4):
            started = time.perf_counter()
            try:
                status, payload = self.post(JEV_URL, body, self.key)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, payload = 0, str(e)
            if status in self.RETRY and attempt < 3:
                self.sleep(2 ** attempt)
                continue
            break
        self.calls += 1
        self.latency_ms.append((time.perf_counter() - started) * 1000)
        if status != 200 or not isinstance(payload, dict):
            self.disabled = f"HTTP {status}: {str(payload)[:160]}"
            self.log(f"  Jev unavailable ({self.disabled}); continuing with {self.fallback.name}")
            return self._fallback(plan, view, stats, "jev error")
        usage = payload.get("usage") or {}
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        answers = payload.get("answers") or {}
        choice = (answers.get("next_action") or {}).get("choice")
        stuck = (answers.get("is_stuck") or {}).get("noul")
        if choice not in plan.menu(not stats.just_restored):
            return self._fallback(plan, view, stats, f"jev chose {str(choice)[:30]!r}")
        probs = (answers.get("next_action") or {}).get("probabilities")
        return Decision(choice, self.name, probs if isinstance(probs, dict) else None,
                        float(stuck) if isinstance(stuck, (int, float)) else None)


def make_policy(name: str, rng: random.Random, max_calls: int = 100, log=print):
    if name == "novelty":
        return NoveltyPolicy(rng)
    if name == "random":
        return RandomPolicy(rng)
    if name == "jev":
        key = api_key()
        if not key:
            raise PlanError("TYPESAFE_API_KEY is not set (environment or .env)")
        return JevPolicy(key, max_calls, NoveltyPolicy(rng), log=log)
    raise PlanError(f"unknown policy {name!r}")


# ---------------------------------------------------------------------------------------------
# Running


@dataclass
class AgentResult:
    run_id: int
    steps: int
    frames: int
    seconds: float
    run_code: int
    new_code: int
    code_bytes: int
    archive_size: int
    stop: str
    usage: dict | None


def _progress(plan: Plan, ram: bytes) -> int | None:
    if plan.progress is None:
        return None
    lo, hi = plan.progress
    return ram[lo] + (ram[hi] << 8 if hi is not None else 0)


def run_agent(kb_path: Path, plan: Plan, policy_name: str = "novelty", steps: int = 300,
              max_frames: int | None = None, minutes: float | None = None, max_calls: int = 100,
              seed: int = 1, log=print) -> AgentResult:
    rng = random.Random(seed)
    policy = make_policy(policy_name, rng, max_calls, log)
    with KB(kb_path) as kb:
        rom = kb.rom()
        baseline = kb.code_mask()
        start_state = None
        if "state" in plan.start:
            row = kb.db.execute("SELECT savestate, inputs FROM states WHERE id=?", (plan.start["state"],)).fetchone()
            if row is None:
                raise PlanError(f"no archived state {plan.start['state']}")
            start_state = (bytes(row[0]), json.loads(row[1]))
        run_id = kb.add_run(datetime.now(timezone.utc).isoformat(timespec="seconds"), seed)
        kb.commit()
    rom_file = kb_path.with_suffix(".nes")
    rom_file.write_bytes(rom.raw)

    cells, archive, stats = CellModel(), Archive(), Stats()
    decisions: list[tuple] = []
    ranges: dict[str, list[int]] = {}
    curve: list[tuple[int, int, int]] = []     # (step, run code, new beyond earlier runs)
    frames_total = step = restores = 0
    stop = "step limit"
    started = last_log = time.perf_counter()
    deadline = started + minutes * 60 if minutes else None

    with Mesen(rom_file) as m:
        m.step(1)
        if start_state:
            m.load_bytes(start_state[0])
            frame, inputs = 0, list(start_state[1])
        else:
            setup = parse_inputs(plan.start.get("setup"))
            for n, buttons in setup:
                if n:
                    m.step(n, buttons)
            frame, inputs = 1 + sum(n for n, _ in setup), [[1, "none"], *[[n, b] for n, b in setup]]
        for addr, value in plan.poke.items():
            m.write(addr, bytes([value]))
            inputs.append([0, f"poke ${addr:04X}={value:02X}"])
        for addr, value in plan.freeze.items():
            m.freeze(addr, value)
            inputs.append([0, f"freeze ${addr:04X}={value:02X}"])

        code, *_ = m.cdl_stats()
        ram = m.ram()
        best_progress = _progress(plan, ram)
        root = Entry("root", frame, code, _compact(inputs), ram, m.save_bytes(), True, 0)
        archive.add(root)
        best_entry = root
        seen_cells = {"root"}
        cell_visits: dict[str, int] = {}
        # Progress records, best last. A record can be doomed (mid-fall into a pit): when restoring
        # it keeps leading nowhere, it is skipped and the next best one is used.
        ladder: list[tuple[int, Entry]] = [(best_progress or 0, root)]
        fails: dict[int, int] = {}
        trial: list = []          # [entry, its progress, peak progress since, moves since]
        stuck_streak = 0

        def pick_best() -> Entry:
            for _, e in reversed(ladder):
                if fails.get(id(e), 0) < DOOMED_AFTER:
                    return e
            return root

        def judge_trial() -> None:
            if trial:
                e, p, peak, moves = trial
                if peak <= p and moves >= 2:
                    fails[id(e)] = fails.get(id(e), 0) + 1
                trial.clear()

        def restore(entry: Entry) -> None:
            nonlocal frame, inputs, ram
            m.load_bytes(entry.state)
            frame, inputs, ram = entry.frame, list(entry.inputs), entry.ram
            entry.visits += 1

        try:
            while step < steps and (max_frames is None or frames_total < max_frames) \
                    and (deadline is None or time.perf_counter() < deadline):
                cell = cells.cell(ram)
                cell_visits[cell] = cell_visits.get(cell, 0) + 1
                values = {name: ram[addr] for name, addr in plan.watch.items()}
                for name, v in values.items():
                    r = ranges.setdefault(name, [v, v])
                    r[0], r[1] = min(r[0], v), max(r[1], v)
                view = View(cell, cell_visits[cell], values, _progress(plan, ram), best_progress)

                if stats.since_novel >= plan.coach_after:
                    stop = f"plateau: nothing new for {stats.since_novel} steps (coach needed)"
                    break
                forced = stats.since_novel and stats.since_novel % plan.restore_after == 0 \
                    and {"restore_frontier", "restore_best"} & set(plan.builtins)
                if forced:
                    # With a progress measure, alternate: push on from the best state, then widen
                    # from a frontier state, so the run neither only digs nor only wanders.
                    options = [b for b in (("restore_best", "restore_frontier") if plan.progress
                                           else ("restore_frontier", "restore_best")) if b in plan.builtins]
                    target = options[restores % len(options)] if plan.progress else options[0]
                    decision = Decision(target, "loop-guard", note="nothing new; restore")
                else:
                    decision = policy.choose(plan, view, stats)
                    # The streak restarts after every restore, so a policy that keeps saying
                    # "stuck" gets STUCK_STREAK fresh moves from each restored state.
                    stuck_streak = stuck_streak + 1 if decision.stuck is not None and decision.stuck >= 0.85 else 0
                    if stuck_streak >= STUCK_STREAK and "restore_frontier" in plan.builtins:
                        decision = Decision("restore_frontier", "loop-guard", stuck=decision.stuck,
                                            note=f"policy says stuck ({decision.stuck:.2f}) {stuck_streak}x")

                before, n_frames, prev_ram = code, 0, ram
                if decision.action in BUILTINS:
                    judge_trial()
                    stuck_streak = 0
                if decision.action == "restore_frontier":
                    restore(archive.select(rng, step))
                    restores += 1
                elif decision.action == "restore_best":
                    target = pick_best()
                    restore(target)
                    restores += 1
                    if plan.progress:
                        trial[:] = [target, _progress(plan, target.ram) or 0, _progress(plan, target.ram) or 0, 0]
                else:
                    for n, buttons in plan.actions[decision.action][1]:
                        if n:
                            m.step(n, buttons)
                            n_frames += n
                            inputs.append([n, buttons])
                    frame += n_frames
                    frames_total += n_frames
                    ram = m.ram()
                    cells.observe(prev_ram, ram)

                code, *_ = m.cdl_stats()
                new_cell_key = cells.cell(ram)
                new_cell = new_cell_key not in seen_cells
                seen_cells.add(new_cell_key)
                progress = _progress(plan, ram)
                gain = 0
                if trial and progress is not None and decision.action not in BUILTINS:
                    trial[2], trial[3] = max(trial[2], progress), trial[3] + 1
                if progress is not None and best_progress is not None and progress > best_progress:
                    gain, best_progress = progress - best_progress, progress
                outcome = Outcome(decision.action, n_frames, code - before, new_cell, progress, gain)
                if decision.action not in BUILTINS:
                    stats.add(cell, outcome)
                else:
                    stats.since_novel += 1
                    stats.recent = (stats.recent + [outcome])[-RECENT:]
                    stats.just_restored = True

                discovered = outcome.new_code > 0 or gain > 0
                if archive.wants(new_cell_key, frame, discovered) and decision.action not in BUILTINS:
                    entry = Entry(new_cell_key, frame, code, _compact(inputs), ram, m.save_bytes(), discovered, step)
                    archive.add(entry)
                    if gain > 0:
                        ladder.append((progress, entry))
                decisions.append((step, decision.action, decision.policy, cell, n_frames, outcome.new_code,
                                  int(new_cell), progress, decision.stuck, decision.note))
                step += 1
                if step % REFIT_EVERY == 0 and cells.refit():
                    archive.rekey(cells)
                    seen_cells = {e.cell for e in archive.entries.values()} | {cells.cell(ram)}
                if step % CURVE_EVERY == 0:
                    curve.append((step, *run_metrics(m.cdl(), baseline)))
                now = time.perf_counter()
                if step % 10 == 0:
                    with KB(kb_path) as kb:
                        kb.set_progress(f"agent {policy_name}", step, steps, f"code {code}, restores {restores}")
                if now - last_log >= 30:
                    last_log = now
                    log(f"  {now - started:5.0f}s  step={step:>5}  frames={frames_total:>7}  code={code:>5}  "
                        f"archive={len(archive)}  restores={restores}  since-new={stats.since_novel}")
        finally:
            seconds = time.perf_counter() - started
            judge_trial()
            best_entry = pick_best()
            cdl = m.cdl()
            counts = {(mt, kind): m.counters(mt, kind)
                      for mt, kind in (("nesPrgRom", "execCount"), ("nesPrgRom", "readCount"),
                                       ("nesInternalRam", "readCount"), ("nesInternalRam", "writeCount"))}
            run_code, new_code = run_metrics(cdl, baseline)
            if not curve or curve[-1][0] != step:
                curve.append((step, run_code, new_code))
            usage = getattr(policy, "usage", None)
            notes = {"policy": policy_name, "stop": stop, "steps": step, "restores": restores,
                     "best_usable": _progress(plan, best_entry.ram),
                     "doomed": sorted(p for p, e in ladder if fails.get(id(e), 0) >= DOOMED_AFTER),
                     "jev": usage, "ranges": ranges, "curve": curve, "best_progress": best_progress,
                     "plan": {"goal": plan.goal, "start": plan.start, "actions": list(plan.actions)}}
            with KB(kb_path) as kb:
                merged = kb.merge_cdl("nesPrgRom", cdl)
                for (mt, kind), values in counts.items():
                    kb.merge_counters(mt, kind, values)
                kept = list(archive.entries.values())
                if all(e is not best_entry for e in kept):
                    kept.append(best_entry)
                kb.replace_states(run_id, [
                    (e.cell, e.frame, e.new_code, e.visits, json.dumps(e.inputs, separators=(",", ":")),
                     e.ram, e.state) for e in kept])
                notes["best_state"] = kb.db.execute(
                    "SELECT id FROM states WHERE run_id=? AND cell=? AND frame=? ORDER BY id LIMIT 1",
                    (run_id, best_entry.cell, best_entry.frame)).fetchone()[0]
                kb.add_decisions(run_id, decisions)
                kb.set_progress(f"agent {policy_name}", step, steps, f"stopped: {stop}; new code {new_code}", finished=True)
                total_code = sum(1 for f in merged if f & 0x01)
                kb.finish_run(run_id, frames=frames_total, actions=step, wall_seconds=round(seconds, 1),
                              code_bytes=total_code, data_bytes=sum(1 for f in merged if f & 0x02),
                              archive_size=len(archive), kind=f"agent:{policy_name}", run_code=run_code,
                              new_code=new_code, notes=json.dumps(notes))
    rom_file.unlink(missing_ok=True)
    return AgentResult(run_id, step, frames_total, seconds, run_code, new_code, total_code, len(archive), stop, usage)


# ---------------------------------------------------------------------------------------------
# Coach report


def report(kb_path: Path, run_id: int | None = None, unreached: int = 20) -> str:
    """Markdown evidence for the coach: what the run did, where it stalled, what is still unreached."""
    with KB(kb_path) as kb:
        rom = kb.rom()
        if run_id is None:
            row = kb.db.execute("SELECT max(id) FROM runs WHERE kind LIKE 'agent:%' AND notes IS NOT NULL").fetchone()
            run_id = row[0] if row else None
        run = kb.db.execute("SELECT id, started, kind, frames, wall_seconds, run_code, new_code, code_bytes, notes "
                            "FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        if run is None:
            return "No finished agent run recorded yet. Run `python -m renes agent <kb>` first."
        runs = kb.db.execute("SELECT id, kind, frames, run_code, new_code FROM runs WHERE frames IS NOT NULL "
                             "ORDER BY id").fetchall()
        decisions = kb.db.execute("SELECT step, action, policy, cell, new_code, new_cell, progress, stuck "
                                  "FROM decisions WHERE run_id=? ORDER BY step", (run_id,)).fetchall()
        mask = kb.code_mask()
        banks = rom.banks()
        multi = len(banks) > 1
        names = {(b.index, l.addr): l.name for b in banks for l in kb.labels(b.index)}
        notes_by_entry = {(b.index, e): s for b in banks for e, s, *_ in kb.routine_notes(b.index)}
        callers: dict[tuple[int, int], set[int]] = {}
        for b in banks:
            for caller, callee, _site, _kind in kb.calls(b.index):
                callers.setdefault((b.index, callee), set()).add(caller)
        routines = [(b, entry, last) for b in banks for entry, last, _ in kb.routines(b.index)]
    prg = len(rom.prg)
    _, started, kind, frames, secs, run_code, new_code, total, notes = run
    notes = json.loads(notes)
    out = [f"# Exploration report: run {run_id} ({kind}), {started}", "",
           f"- Stopped: {notes['stop']}",
           f"- {notes['steps']} steps, {frames} frames (~{frames / 60:.0f} s of play) in {secs:.0f} s; "
           f"{notes['restores']} restores",
           f"- Code executed this run: {run_code} bytes ({run_code / prg:.1%} of PRG); "
           f"**new beyond all earlier runs: {new_code} bytes**",
           f"- All runs together: {total} code bytes ({total / prg:.1%} of PRG)"]
    if jev := notes.get("jev"):
        out.append(f"- Jev: {jev['calls']} calls, {jev['input_tokens']} input tokens (~${jev['usd']}), "
                   f"median {jev['median_ms']} ms" + (f"; stopped: {jev['disabled']}" if jev.get("disabled") else ""))
    if notes.get("best_progress") is not None:
        out.append(f"- Best progress: {notes['best_progress']}; best usable state: {notes.get('best_usable')} "
                   f'(archived as state {notes.get("best_state")}; start there with '
                   f'`"start": {{"state": {notes.get("best_state")}}}`)')
        if notes.get("doomed"):
            out.append(f"- Progress records that kept leading nowhere (probably doomed, e.g. mid-fall): "
                       f"{notes['doomed']}")

    out += ["", "## Coverage during the run", "", "| step | code this run | new beyond earlier runs |", "|---|---|---|"]
    out += [f"| {s} | {c} | {n} |" for s, c, n in notes.get("curve", [])]

    out += ["", "## Runs so far (equal frames make a fair comparison)", "",
            "| run | kind | frames | code this run | new beyond earlier runs |", "|---|---|---|---|---|"]
    out += [f"| {i} | {k or 'fuzz'} | {f} | {c if c is not None else '-'} | {n if n is not None else '-'} |"
            for i, k, f, c, n in runs]

    by_action: dict[str, list] = {}
    for _, action, policy, _, nc, ncell, _, _ in decisions:
        by_action.setdefault(action, []).append((policy, nc, ncell))
    out += ["", "## Actions", "", "| action | chosen | by policy / loop guard | found new code | new situations | "
            "new code bytes |", "|---|---|---|---|---|---|"]
    for action, rows in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
        guard = sum(1 for p, _, _ in rows if p == "loop-guard")
        out.append(f"| {action} | {len(rows)} | {len(rows) - guard} / {guard} | {sum(1 for _, n, _ in rows if n)} | "
                   f"{sum(c for _, _, c in rows)} | {sum(n for _, n, _ in rows)} |")
    if stuck := [s for *_, s in decisions if s is not None]:
        out.append(f"\nPolicy's own is_stuck estimate: mean {sum(stuck) / len(stuck):.2f}, "
                   f"last {stuck[-1]:.2f}")

    by_cell: dict[str, list[int]] = {}
    for _, _, _, cell, nc, ncell, _, _ in decisions:
        by_cell.setdefault(cell, []).append(1 if nc or ncell else 0)
    stalls = sorted(((len(v) - sum(v), len(v), c) for c, v in by_cell.items()), reverse=True)[:8]
    out += ["", "## Where time went (situations = values of slowly changing RAM bytes)", "",
            "| situation | steps | steps that found nothing |", "|---|---|---|"]
    out += [f"| `{c}` | {n} | {dry} |" for dry, n, c in stalls]

    if notes.get("ranges"):
        out += ["", "## Watched variables (min - max seen)", ""]
        out += [f"- {name}: {lo} - {hi}" for name, (lo, hi) in notes["ranges"].items()]

    missing = []
    for b, entry, last in routines:
        i = b.offset + entry - b.base
        if 0 <= i < len(mask) and not mask[i]:
            missing.append((last - entry + 1, b.index, entry))
    missing.sort(reverse=True)
    out += ["", f"## Unreached code: {len(missing)} routines, {sum(s for s, *_ in missing)} bytes never executed",
            "", "Largest first. Callers show what has to happen first; annotated summaries say what it does.", ""]
    for size, bank, entry in missing[:unreached]:
        who = ", ".join(names.get((bank, c), f"${c:04X}") for c in sorted(callers.get((bank, entry), ()))[:3]) \
            or "no known caller"
        summary = notes_by_entry.get((bank, entry), "")
        where = f"B{bank}:${entry:04X}" if multi else f"${entry:04X}"
        out.append(f"- {where} {names.get((bank, entry), '')} ({size} bytes), called from {who}"
                   + (f": {summary}" if summary else ""))
    return "\n".join(out) + "\n"
