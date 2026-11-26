"""M5: test candidate labels with controlled A/B experiments in the emulator.

An experiment reaches a start state (an input script replayed from power-on, or a state from
the explorer archive), saves it, then plays the same test inputs twice from it:

  control    nothing changed
  treatment  with an intervention: freeze RAM addresses, and/or poke values at a given frame

The emulator is deterministic, so any difference between the runs is caused by the
intervention. Checks compare per-frame RAM traces (and final screens). If every check passes
and the experiment targets a labeled address, that label is promoted to `verified`.

Spec (JSON object, or a list of them):
{
  "name": "Freezing the speed moves the player",
  "target": "$0034",                       # label address the experiment is about (optional)
  "setup": [[60, "none"], [4, "start"], [240, "none"]],   # from power-on; or "state": <archive id>
  "inputs": [[90, "none"]],                # test inputs, same for both runs
  "intervention": {"freeze": {"$0034": 24}, "poke": {"$0036": 1}, "poke_at": 0},
  "shared": {"freeze": {"$003F": 2}, "poke": {"$0059": 3}},   # optional, both runs (see below)
  "checks": [{"check": "final_delta", "watch": "$0030", "watch_hi": "$0031", "op": ">", "value": 30}],
  "alternative": "it is the moving direction, not the facing direction: then ..."
}

"shared" puts both runs in the same situation: its freezes hold from the start of the setup through
both runs, and its pokes are written once when the start state is reached (before it is saved).
The treatment's own freeze wins where both name an address.

A passing experiment only verifies its target if it names the competing meaning it rules out
("alternative") and its checks would come out differently under that meaning. Without one it is
recorded as a diagnostic: `$0038` passed a check that both "facing" and "moving direction" satisfy.

Checks (run = "control" | "treatment", default "treatment"):
  increments_by  watch, value, [run], [min_fraction=0.9]   per-frame step (mod 256)
  constant / changes  watch, [run]       (these, changes_at_most and increments_by skip the samples
                                          before a poke, and before frame `from_frame` if given)
  changes_at_most     watch, value, [run]                    the value changes at most `value` times
  differs / same      watch                                  treatment series vs control series
  final_delta         watch, [watch_hi], op, value           treatment final - control final (signed)
  changed_bytes_ratio op, value                              RAM bytes that ever changed, treatment/control
  screen_differs / screen_same                               final frames
  vram_differs / vram_same                                   nametables + palette, compared on every frame
                                                             (catches a redraw the final screenshot misses)
  apu_differs / apu_same                                     writes to the sound registers ($4000-$4013,
                                                             $4015, $4017), compared frame by frame

A treatment that also freezes or pokes other addresses than the target (and the target label's
extent) doesn't verify: any of them could cause the difference. Setup belongs in "shared"; when
the extra address is part of the test (a mirror, the other half of a pair), say why in
"other_addresses": "<why they can't explain the result>".
`renes experiments <kb> --invalidate N --reason "..."` marks a recorded experiment as proving
nothing: it stops counting as evidence and a label it alone verified goes back to candidate.

"writers": ["$0251", ...] reports, for each run, which code wrote those addresses (PC, routine, count).
An input step [n, "reset"] presses Reset, then waits n frames: RAM survives a soft reset, so a
shared or treatment poke made before it is what the boot code finds (`[[1, "reset"], [120, "none"]]`
as the treatment inputs, with the poke at frame 0).
"intervention": {"poke_at_pc": {"$C5D2": {"$0050": 32}}} writes $0050 = 32 every time the
instruction at $C5D2 is about to run: for bytes written and used within one frame (scratch, PPU
queues), where a freeze is the wrong tool.
(Freezes override every CPU write to the address, at any point in the frame, not once per frame.)
"""
import json
import operator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .annotate import parse_addr
from .emu.mesen import Mesen
from .kb import KB, NON_ROM
from .opcodes import OPCODES

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq,
       "!=": operator.ne}
PROMOTABLE = {"auto", "candidate"}
BOOT_SLOT = "boot"
START_SLOT = "start"
RESET = "reset"          # an input step that presses the console's Reset button, then holds nothing


class SpecError(ValueError):
    pass


@dataclass
class Trace:
    ram: list[bytes]   # RAM after each frame; index 0 = start state
    screen: bytes      # PNG of the final frame
    after: int = 0     # first sample after the poke; per-run checks ignore the samples before it
    strip: list = field(default_factory=list)   # [(frame, png)] evenly spaced screenshots for review
    vram: list = field(default_factory=list)    # nametables + palette after each frame (vram checks only)
    writers: list = field(default_factory=list)  # [(addr, pc, count)] for the spec's "writers"
    apu: list = field(default_factory=list)      # per frame: ((register, value), ...) written to $4000-$4017


@dataclass
class CheckResult:
    check: dict
    passed: bool
    detail: str


@dataclass
class Result:
    name: str
    target: int | None
    passed: bool
    checks: list[CheckResult]
    frames: int
    promoted: str | None = None
    note: str = ""
    experiment_id: int | None = None
    strips: dict = field(default_factory=dict)   # "control"/"treatment" -> [(frame, png)]
    writers: dict = field(default_factory=dict)  # "control"/"treatment" -> [(addr, pc, count)]


def parse_inputs(seq) -> list[tuple[int, str]]:
    out = []
    for item in seq or []:
        n, buttons = int(item[0]), str(item[1])
        if n < 0:
            raise SpecError(f"negative frame count in {item}")
        out.append((n, buttons))
    return out


def _addr_map(d: dict | None) -> dict[int, int]:
    return {parse_addr(k): int(v) & 0xFF for k, v in (d or {}).items()}


# ---------------------------------------------------------------------------------------------
# Running


class Lab:
    """One Mesen process; every run starts from a saved state, so runs never interfere."""

    def __init__(self, kb: KB, rom_file: Path):
        self.kb = kb
        self.m = Mesen(rom_file)
        self.m.step(1)
        self.m.save(BOOT_SLOT)
        self.shared: dict[int, int] = {}   # freezes held in every run of the current experiment
        self.writer_start = writer_starts(kb)

    def close(self) -> None:
        self.m.close()

    def reach(self, spec: dict) -> None:
        """Bring the emulator to the experiment's start state and save it."""
        for addr in self.shared:
            self.m.unfreeze(addr)
        shared = spec.get("shared") or {}
        self.shared = _addr_map(shared.get("freeze"))
        for addr, value in self.shared.items():
            self.m.freeze(addr, value)
        if "state" in spec:
            row = self.kb.db.execute("SELECT savestate, (SELECT retired FROM runs WHERE id=run_id) FROM states "
                                     "WHERE id=?", (int(spec["state"]),)).fetchone()
            if row is None:
                raise SpecError(f"no archived state {spec['state']}")
            if row[1]:
                raise SpecError(f"state {spec['state']} is from a retired run ({row[1]}); pick another with `renes states`")
            self.m.load_bytes(bytes(row[0]))
        else:
            self.m.load(BOOT_SLOT)
            for n, buttons in parse_inputs(spec.get("setup")):
                if buttons == RESET:        # soft reset: RAM survives, so earlier pokes reach the boot code
                    self.m.reset()
                    buttons = "none"
                if n:
                    self.m.step(n, buttons)
        for addr, value in _addr_map(shared.get("poke")).items():
            self.m.write(addr, bytes([value]))
        self.m.save(START_SLOT)

    def vram(self) -> bytes:
        return self.m.read(0, 0x800, "nesNametableRam") + self.m.read(0, 0x20, "nesPaletteRam")

    def run(self, inputs: list[tuple[int, str]], freeze: dict[int, int] | None = None,
            poke: dict[int, int] | None = None, poke_at: int = 0, shots: int = 4, *,
            vram: bool = False, writers: list[int] | None = None,
            pc_pokes: dict[int, dict[int, int]] | None = None, apu: bool = False) -> Trace:
        self.m.load(START_SLOT)
        total = sum(n for n, _ in inputs)
        shot_at = {max(1, total * k // shots) for k in range(1, shots + 1)} if total else set()
        strip, video = [], []
        for addr, value in {**self.shared, **(freeze or {})}.items():
            self.m.freeze(addr, value)
        for pc, writes in (pc_pokes or {}).items():   # bytes set and used within one frame
            for addr, value in writes.items():
                self.m.poke_at_pc(pc, addr, value)
        if writers:
            self.m.log_writes(min(writers), max(writers))      # restarts the log
        frames = [self.m.ram()]
        if vram:
            video.append(self.vram())
        sound = [()] if apu else []
        if apu:
            self.m.log_apu()
        frame = 0
        try:
            for n, buttons in inputs:
                if buttons == RESET:
                    self.m.reset()
                    buttons = "none"
                for _ in range(n):
                    if poke and frame == poke_at:
                        for addr, value in poke.items():
                            self.m.write(addr, bytes([value]))
                    self.m.step(1, buttons)
                    frame += 1
                    frames.append(self.m.ram())
                    if vram:
                        video.append(self.vram())
                    if apu:                     # this frame's APU register writes, then a fresh log
                        sound.append(tuple((a, v) for _, a, v in self.m.apu_writes()))
                        self.m.log_apu()
                    if frame in shot_at:
                        strip.append((frame, self.m.screenshot_bytes()))
            screen = self.m.screenshot_bytes()
            wrote = _merge_writers([w for w in self.m.writers() if w[0] in writers], self.writer_start) if writers else []
        finally:
            if pc_pokes:
                self.m.clear_pc_pokes()
            for addr in freeze or {}:
                if addr in self.shared:
                    self.m.freeze(addr, self.shared[addr])
                else:
                    self.m.unfreeze(addr)
        return Trace(frames, screen, poke_at + 1 if poke else 0, strip, video, wrote, sound)


# ---------------------------------------------------------------------------------------------
# Checks


def _signed(value: int, bits: int) -> int:
    half = 1 << (bits - 1)
    return value - (1 << bits) if value >= half else value


def _series(trace: Trace, addr: int, from_poke: bool = False, from_frame: int = 0,
            to_frame: int | None = None) -> list[int]:
    end = None if to_frame is None else to_frame + 1
    return [frame[addr] for frame in trace.ram[max(trace.after if from_poke else 0, from_frame):end]]


def _changed(trace: Trace) -> int:
    first = trace.ram[0]
    return sum(1 for i in range(len(first)) if any(frame[i] != first[i] for frame in trace.ram))


def evaluate(check: dict, control: Trace, treatment: Trace) -> CheckResult:
    kind = check.get("check")
    run = treatment if check.get("run", "treatment") == "treatment" else control
    addr = parse_addr(check["watch"]) if "watch" in check else None

    def compare(value) -> tuple[bool, str]:
        op = OPS.get(check.get("op", ""))
        if op is None:
            raise SpecError(f"check {kind} needs op in {sorted(OPS)}")
        return op(value, check["value"]), f"{value} {check['op']} {check['value']}"

    skip = int(check.get("from_frame", 0))
    until = int(check["to_frame"]) if check.get("to_frame") is not None else None   # inclusive
    if kind == "increments_by":
        s = _series(run, addr, from_poke=True, from_frame=skip, to_frame=until)
        steps = [(b - a) & 0xFF for a, b in zip(s, s[1:])]
        want = int(check["value"]) & 0xFF
        held = sum(1 for d in steps if d == 0)
        if check.get("changed_only"):            # a counter that waits or reloads: judge its moves only
            steps = [d for d in steps if d]
        hits = sum(1 for d in steps if d == want)
        frac = hits / max(len(steps), 1)
        need = float(check.get("min_fraction", 0.9))
        other = len(steps) - hits - (0 if check.get("changed_only") else held)
        return CheckResult(check, frac >= need,
                           f"{frac:.0%} of {'changes' if check.get('changed_only') else 'frames'} stepped by "
                           f"{_signed(want, 8)} (need {need:.0%}); {held} frames unchanged, {other} other steps")
    if kind in ("constant", "changes"):
        s = _series(run, addr, from_poke=True, from_frame=skip, to_frame=until)
        distinct = len(set(s))
        ok = distinct == 1 if kind == "constant" else distinct > 1
        return CheckResult(check, ok, f"{distinct} distinct values: {', '.join(f'{v:02X}' for v in sorted(set(s))[:8])}")
    if kind == "changes_at_most":
        s = _series(run, addr, from_poke=True, from_frame=skip, to_frame=until)
        moves = [i + 1 for i, (a, b) in enumerate(zip(s, s[1:])) if a != b]
        ok = len(moves) <= int(check["value"])
        return CheckResult(check, ok, f"changed {len(moves)} times (at most {check['value']})"
                           + (f", first after sample {moves[0]}" if moves else ""))
    if kind in ("vram_differs", "vram_same"):
        if not control.vram or not treatment.vram:
            raise SpecError(f"{kind}: the runs recorded no VRAM")
        first = next((i for i, (x, y) in enumerate(zip(control.vram, treatment.vram)) if x != y), None)
        ok = (first is not None) == (kind == "vram_differs")
        if first is None:
            return CheckResult(check, ok, "nametables and palette identical on every frame")
        x, y = control.vram[first], treatment.vram[first]
        at = next(i for i in range(len(x)) if x[i] != y[i])
        where = f"PPU ${0x2000 + at:04X}" if at < 0x800 else f"palette ${0x3F00 + at - 0x800:04X}"
        n = sum(1 for a, b in zip(control.vram, treatment.vram) if a != b)
        return CheckResult(check, ok, f"first differ at frame {first} ({where}: {x[at]:02X} vs {y[at]:02X}); "
                                      f"{n} of {len(control.vram)} frames differ")
    if kind in ("apu_differs", "apu_same"):
        if not control.apu or not treatment.apu:
            raise SpecError(f"{kind}: the runs recorded no APU writes")
        a, b = control.apu[skip:None if until is None else until + 1], treatment.apu[skip:None if until is None else until + 1]
        first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
        ok = (first is not None) == (kind == "apu_differs")
        writes = sum(len(x) for x in a)
        if first is None:
            return CheckResult(check, ok, f"identical APU writes on every frame ({writes} writes in the control)")
        x, y = a[first], b[first]
        at = next((i for i in range(max(len(x), len(y))) if (x[i:i + 1] or None) != (y[i:i + 1] or None)), 0)
        show = lambda w: f"${w[at][0]:04X}={w[at][1]:02X}" if at < len(w) else "nothing"
        return CheckResult(check, ok, f"APU writes first differ at frame {skip + first}: control {show(x)}, "
                                      f"treatment {show(y)}; {sum(1 for p, q in zip(a, b) if p != q)} frames differ")
    if kind in ("differs", "same"):
        a = _series(control, addr, from_frame=skip, to_frame=until)
        b = _series(treatment, addr, from_frame=skip, to_frame=until)
        first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
        ok = (first is not None) if kind == "differs" else first is None
        window = f" in frames {skip}-{until if until is not None else 'end'}" if skip or until is not None else ""
        return CheckResult(check, ok, f"identical{window}" if first is None else
                           f"diverge at frame {skip + first}: control {a[first]:02X}, treatment {b[first]:02X}")
    if kind == "final_delta":
        def value(trace: Trace) -> int:
            lo = trace.ram[-1][addr]
            if "watch_hi" in check:
                return lo | (trace.ram[-1][parse_addr(check["watch_hi"])] << 8)
            return lo
        bits = 16 if "watch_hi" in check else 8
        delta = _signed((value(treatment) - value(control)) & ((1 << bits) - 1), bits)
        ok, text = compare(delta)
        return CheckResult(check, ok, f"treatment - control = {text}")
    if kind == "changed_bytes_ratio":
        c, t = _changed(control), _changed(treatment)
        ratio = round(t / max(c, 1), 3)
        ok, text = compare(ratio)
        return CheckResult(check, ok, f"{t}/{c} bytes changed: {text}")
    if kind in ("screen_differs", "screen_same"):
        same = control.screen == treatment.screen
        return CheckResult(check, same == (kind == "screen_same"), "screens identical" if same else "screens differ")
    raise SpecError(f"unknown check {kind!r}")


def run_experiment(lab: Lab, spec: dict) -> Result:
    if not spec.get("checks"):
        raise SpecError("an experiment needs at least one check")
    inputs = parse_inputs(spec.get("inputs"))
    iv = spec.get("intervention") or {}
    lab.reach(spec)
    extra = {"vram": any(str(c.get("check", "")).startswith("vram_") for c in spec["checks"]),
             "apu": any(str(c.get("check", "")).startswith("apu_") for c in spec["checks"]),
             "writers": [parse_addr(a) for a in spec.get("writers") or []]}
    control = lab.run(inputs, **extra)
    treatment = lab.run(inputs, _addr_map(iv.get("freeze")), _addr_map(iv.get("poke")), int(iv.get("poke_at", 0)),
                        pc_pokes={parse_addr(pc): _addr_map(w) for pc, w in (iv.get("poke_at_pc") or {}).items()},
                        **extra)
    checks = [evaluate(c, control, treatment) for c in spec["checks"]]
    target = parse_addr(spec["target"]) if spec.get("target") is not None else None
    return Result(spec.get("name", "unnamed"), target, all(c.passed for c in checks), checks,
                  len(control.ram) - 1, strips={"control": control.strip, "treatment": treatment.strip},
                  writers={"control": control.writers, "treatment": treatment.writers} if extra["writers"] else {})


def other_addresses(kb: KB, spec: dict, target: int | None) -> list[int]:
    """Addresses the treatment alone changes besides its target (and the target label's extent).
    A difference between the runs could come from any of them (feedback #36)."""
    iv = spec.get("intervention") or {}
    touched = set(_addr_map(iv.get("freeze"))) | set(_addr_map(iv.get("poke")))
    for writes in (iv.get("poke_at_pc") or {}).values():
        touched |= set(_addr_map(writes))
    if target is None:
        return sorted(touched)
    label = kb.label_at(NON_ROM if target < 0x8000 else 0, target)
    return sorted(a for a in touched if not target <= a < target + ((label.size if label else None) or 1))


def still_verified(kb: KB, bank: int, addr: int) -> bool:
    """Whether a valid passing experiment still verifies the label at (bank, addr)."""
    for eid, _, _, _, _, passed in kb.experiments(bank, addr):
        spec = json.loads(kb.db.execute("SELECT spec FROM experiments WHERE id=?", (eid,)).fetchone()[0])
        if passed and spec.get("verifies", True) and str(spec.get("alternative") or "").strip():
            return True
    return False


def record(kb: KB, spec: dict, result: Result) -> None:
    """Store the result; promote the target label if every check passed."""
    bank = None if result.target is None else (NON_ROM if result.target < 0x8000 else 0)
    payload = json.dumps([{"check": c.check, "passed": c.passed, "detail": c.detail} for c in result.checks])
    result.experiment_id = kb.add_experiment(datetime.now(timezone.utc).isoformat(timespec="seconds"), bank,
                                             result.target, result.name, json.dumps(spec), result.passed, payload)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for role, strip in result.strips.items():
        for frame, png in strip:
            kb.add_media("experiment", bank, result.target, result.experiment_id, role, frame, png, created)
    others = other_addresses(kb, spec, result.target)
    if result.passed and result.target is not None and spec.get("verifies", True) \
            and not str(spec.get("alternative") or "").strip():
        result.note = ("not verified: add an \"alternative\" (the competing meaning these checks rule out) "
                       "to promote the label")
    elif result.passed and result.target is not None and spec.get("verifies", True) and others \
            and not str(spec.get("other_addresses") or "").strip():
        result.note = (f"not verified: the treatment also changes {', '.join(f'${a:04X}' for a in others)}, "
                       f"which could explain the difference. Put setup pokes in \"shared\" (both runs), or say in "
                       f"\"other_addresses\" why they can't")
    elif result.passed and result.target is not None and spec.get("verifies", True):
        label = kb.label_at(bank, result.target)
        if label and label.status in PROMOTABLE:
            kb.set_label_status(bank, result.target, "verified")
            result.promoted = label.name


def run_all(kb_path: Path, specs: list[dict]) -> list[Result]:
    results = []
    with KB(kb_path) as kb:
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        try:
            for spec in specs:
                result = run_experiment(lab, spec)
                record(kb, spec, result)
                kb.commit()
                results.append(result)
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)
    return results


# ---------------------------------------------------------------------------------------------
# Probe: which RAM reacts to what


PROBE_BUTTONS = ("right", "left", "up", "down", "a", "b", "start", "select")


def writer_starts(kb: KB):
    """Mesen reports a write with the PC already past the storing instruction ($C864 for the sta $59
    at $C862): map such a PC back to the instruction that ends there."""
    from .analyze import PRG_BANK

    rom = kb.rom()
    prg, base = rom.prg, rom.prg_base
    ops = {a for a, k in kb.classes(PRG_BANK).items() if k == "op"}

    def start(pc: int) -> int:
        for s in (pc - 3, pc - 2, pc - 1):
            op = OPCODES.get(prg[s - base]) if base <= s < base + len(prg) else None
            if op and s + op.size == pc and (s in ops or not ops):
                return s
        return pc
    return start


def _merge_writers(rows, start) -> list[tuple[int, int, int]]:
    counts: dict[tuple[int, int], int] = {}
    for a, pc, n in rows:
        counts[a, start(pc)] = counts.get((a, start(pc)), 0) + n
    return sorted((a, pc, n) for (a, pc), n in counts.items())


def find_writes(kb_path: Path, addr: int, state_ids: list[int], frames: int = 60, log=print
                ) -> list[tuple[int, list[tuple[int, int]]]]:
    """Play each archived state idle for `frames` and record which code writes `addr`.
    Returns [(state id, [(pc, count), ...])] for the states where something wrote it."""
    out = []
    with KB(kb_path) as kb:
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        m = Mesen(rom_file)
        start = writer_starts(kb)
        try:
            for n, sid in enumerate(state_ids, 1):
                row = kb.db.execute("SELECT savestate FROM states WHERE id=?", (sid,)).fetchone()
                m.load_bytes(bytes(row[0]))
                m.log_writes(addr, addr)
                m.step(frames)
                wrote = sorted(((pc, c) for a, pc, c in _merge_writers(m.writers(), start) if a == addr),
                               key=lambda x: -x[1])
                if wrote:
                    out.append((sid, wrote))
                if n % 50 == 0:
                    log(f"  {n}/{len(state_ids)} states played, {len(out)} with writes")
        finally:
            m.close()
            rom_file.unlink(missing_ok=True)
    return out


def changing_bytes(trace: Trace, name, limit: int = 120) -> list[str]:
    """Every RAM byte that changes during a run (to pick a `watch` that moves in the control)."""
    rows = []
    for addr in range(len(trace.ram[0])):
        s = _series(trace, addr)
        moves = sum(1 for a, b in zip(s, s[1:]) if a != b)
        if moves:
            values = sorted(set(s))
            rows.append(f"${addr:04X}  {moves:>3} changes  {s[0]:02X} -> {s[-1]:02X}  "
                        f"values {' '.join(f'{v:02X}' for v in values[:6])}{' ...' if len(values) > 6 else ''}"
                        f"  {name(addr)}")
    return rows[:limit] + ([f"... {len(rows) - limit} more"] if len(rows) > limit else [])


def probe(kb_path: Path, spec: dict, frames: int = 60, top: int = 12, changes: bool = False) -> str:
    with KB(kb_path) as kb:
        names = {l.addr: l.name for l in kb.labels(NON_ROM)}
        statuses = {l.addr: l.status for l in kb.labels(NON_ROM)}
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        try:
            lab.reach(spec)
            idle = lab.run([(frames, "none")])
            pressed = {b: lab.run([(frames, b)]) for b in PROBE_BUTTONS}
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)

    def name(addr: int) -> str:
        return f"{names[addr]} [{statuses[addr]}]" if addr in names else "-"

    size = len(idle.ram[0])
    out = [f"Probe: {frames} frames from the start state, idle vs holding each button.", ""]
    counters = []
    for addr in range(size):
        s = _series(idle, addr)
        steps = [(b - a) & 0xFF for a, b in zip(s, s[1:])]
        if steps and sum(1 for d in steps if d == 1) / len(steps) >= 0.95:
            counters.append(f"${addr:04X} +1/frame  {name(addr)}")
        elif steps and sum(1 for d in steps if d == 0xFF) / len(steps) >= 0.95:
            counters.append(f"${addr:04X} -1/frame  {name(addr)}")
        else:
            downs = [d for d in steps if d]
            if len(downs) >= 2 and all(d == 0xFF for d in downs) and len(downs) < len(steps) * 0.5:
                counters.append(f"${addr:04X} counts down every ~{len(steps) // len(downs)} frames  {name(addr)}")
    out += ["### Counters while idle", *(counters or ["(none)"]), ""]
    if changes:
        out += ["### Every byte that changes while idle (the control run of an experiment from here)",
                *(changing_bytes(idle, name) or ["(none)"]), ""]
    for b, trace in pressed.items():
        reacting = []
        for addr in range(size):
            a, t = _series(idle, addr), _series(trace, addr)
            first = next((i for i, (x, y) in enumerate(zip(a, t)) if x != y), None)
            if first is not None:
                reacting.append((first, addr))
        reacting.sort()
        out.append(f"### Holding {b}: {len(reacting)} bytes differ from idle"
                   + (" (screen differs)" if trace.screen != idle.screen else ""))
        for first, addr in reacting[:top]:
            out.append(f"${addr:04X}  from frame {first:>3}  idle {idle.ram[-1][addr]:02X} -> "
                       f"{trace.ram[-1][addr]:02X}  {name(addr)}")
        out.append("")
    return "\n".join(out)


def load_specs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def capture_media(kb_path: Path, log=print) -> int:
    """Re-run recorded experiments that have no screenshots, only to capture them.

    Results are not re-recorded and labels are not touched: the screenshots attach to the
    original experiment id.
    """
    with KB(kb_path) as kb:
        todo = [(eid, json.loads(spec)) for eid, spec in kb.db.execute(
            "SELECT e.id, e.spec FROM experiments e WHERE NOT EXISTS "
            "(SELECT 1 FROM media m WHERE m.kind='experiment' AND m.ref=e.id) ORDER BY e.id")]
        if not todo:
            return 0
        rom_file = kb_path.with_suffix(".nes")
        rom_file.write_bytes(kb.rom().raw)
        lab = Lab(kb, rom_file)
        done = 0
        try:
            for eid, spec in todo:
                try:
                    inputs = parse_inputs(spec.get("inputs"))
                    iv = spec.get("intervention") or {}
                    lab.reach(spec)
                    strips = {"control": lab.run(inputs).strip,
                              "treatment": lab.run(inputs, _addr_map(iv.get("freeze")), _addr_map(iv.get("poke")),
                                                   int(iv.get("poke_at", 0))).strip}
                except SpecError as e:
                    log(f"  #{eid}: skipped ({e})")
                    continue
                target = parse_addr(spec["target"]) if spec.get("target") is not None else None
                bank = None if target is None else (NON_ROM if target < 0x8000 else 0)
                created = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for role, strip in strips.items():
                    for frame, png in strip:
                        kb.add_media("experiment", bank, target, eid, role, frame, png, created)
                kb.commit()
                done += 1
        finally:
            lab.close()
            rom_file.unlink(missing_ok=True)
    return done
