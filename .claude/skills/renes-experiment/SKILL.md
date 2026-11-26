---
name: renes-experiment
description: Verify or refute candidate labels in a renes knowledge base with controlled A/B experiments in the Mesen emulator (freeze or poke RAM, compare against an untouched control run), and discover new candidates with the button probe. Use when asked to verify, test, check or prove what a RAM variable does, to run freeze/poke experiments, or to find which RAM reacts to input.
argument-hint: "[count=8 | probe | <label or $addr>] [kb=work/<game>.db]"
---

# Experiments (renes M5)

Names from annotation are hypotheses. An experiment turns one into evidence. From a fixed start
state, the same inputs are played twice: a **control** run, and a **treatment** run with an
intervention (freeze addresses, or poke values at a frame). The emulator is deterministic, so
every difference is caused by the intervention. If all checks pass, the target label becomes
`verified`. Failures are recorded too and never change a label.

## Commands

- `python -m renes probe <kb> [--setup '<json>' | --state <id>] [--frames 60]` reports counters
  while idle and, for each button, the RAM bytes that react (earliest first). Use it to find candidates.
- `python -m renes experiment <kb> work/<project>.experiment.json` (one file per project: other agents may share `work/`) runs a JSON list of experiments
  (overwrite this one file each time; the KB keeps every result).
- `python -m renes experiments <kb>` lists all results.
- Work units from `python -m renes next` show each RAM label's status and its experiment history.

## Choosing targets

1. If given a label or address, test that.
2. Otherwise list candidates worth testing, roughly in this order:
   - RAM labels used by many routines (the work units' "RAM it touches" tables show them).
   - Labels whose names other names depend on.
   - Labels annotated with confidence of 0.6 or more (cheap wins first), then the shaky ones.
3. Skip scratch/temporary bytes: their meaning changes from routine to routine.

## Designing an experiment

A good experiment makes a **prediction that would fail if the name were wrong**. "The screen
differs" is almost always true, so never rely on it alone.

| Kind of variable | Intervention | Prediction to check |
|---|---|---|
| counter / timer | none, or poke a value | `increments_by` +1 or -1 per frame (`run: control` for the natural behavior) |
| position | poke +N | `final_delta` of the same address >= N, and it stays shifted |
| speed / velocity | freeze to a value, no input | the position's `final_delta` (with `watch_hi` for the page byte) is roughly speed x frames |
| limit / maximum | freeze lower, hold the direction | the position falls behind the control (`final_delta` < -N) |
| flag (pause, enable) | poke or freeze the bit | the thing it gates stops or starts (`constant` vs control `changes`) |
| input byte | freeze a button bit, no input | the game behaves as if the button were held |
| mode / state | freeze a value | a behavior specific to that state appears. Think about what the game really does in that state first |

Always pair the treatment check with a control check that shows the effect is not just the
default (for example the control `changes` while the treatment is `constant`).

Default start state for most NES games: `"setup": [[60, "none"], [4, "start"], [240, "none"]]`
(press Start at the title, wait for play to begin). Use `"state": <id>` to start from an explorer
archive state instead, for example a later level or another game mode. Find one with
`python -m renes states <kb> Bonus_Mode=1 --show Menu_Option` (RAM labels or `$addr=value`,
several conditions allowed). Check that the state really is where you think: an input script
that "should" reach a mode may not, so watch the mode variable in a `constant` check.
States from a run that went wrong (a desynced replay, no input) can be retired:
`python -m renes states <kb> --retire 7,8 --why "desynced"` (or `--retire input-bug` for every run
started while button input was broken). They are no longer listed or picked, a spec naming one is
refused, and `--restore 7,8` brings them back.

To put **both runs** in a situation (hold a mode, a phase or a player number), add
`"shared": {"freeze": {"$003F": 2}, "poke": {"$0059": 3}}`. Shared freezes hold from the start of
the setup through both runs; shared pokes are written once at the start state. The intervention
then differs from the control only by what you are testing.

The treatment's own `intervention` should touch only the target (and the extent of its sized
label). If it also pokes or freezes other addresses, a pass does not verify: any of them could
cause the difference (two real experiments poked a jump into the treatment only). Move setup to
`"shared"`, or, when the extra address is part of the test (the other half of a pair, a mirror),
say why in `"other_addresses": "..."`. An experiment recorded by mistake can be marked
`python -m renes experiments <kb> --invalidate 24 25 --reason "..."`: it stops counting as
evidence, and a label it alone verified goes back to candidate.

## Spec format

```json
[
  {
    "name": "Forcing a speed moves the player without input",
    "target": "$0034",
    "setup": [[60, "none"], [4, "start"], [240, "none"]],
    "inputs": [[60, "none"]],
    "intervention": {"freeze": {"$0034": 24}},
    "checks": [
      {"check": "final_delta", "watch": "$0030", "watch_hi": "$0031", "op": ">", "value": 30},
      {"check": "constant", "watch": "$0030", "run": "control"}
    ]
  }
]
```

Checks (`run` defaults to `treatment`):
`increments_by` (watch, value, min_fraction 0.9) · `constant` / `changes` (watch) ·
`changes_at_most` (watch, value: the byte may change that many times, e.g. 1 when the game reuses
a slot once) · `differs` / `same` (watch, treatment vs control) · `final_delta` (watch, watch_hi,
op, value; signed) · `changed_bytes_ratio` (op, value; RAM bytes that changed, treatment over
control) · `screen_differs` / `screen_same` (final frames only) · `vram_differs` / `vram_same`
(nametables and palette on every frame: use these for bytes that only affect drawing, since a
brief redraw is gone by the final screenshot) · `apu_differs` / `apu_same` (every write to the sound
registers, frame by frame: for sound-engine bytes whose only effect is what reaches the APU; both
take `from_frame`/`to_frame`).
Per-run checks and `same`/`differs` take `"from_frame": N` and `"to_frame": M` (inclusive) to look at
a window only: settling at the start, or a timing effect later (the treatment launches the ball
earlier, so `same` must stop before that frame).
Set `"verifies": false` for diagnostic runs that must not promote a label.

Two aids for designing a check:
- `python -m renes probe <kb> --state <id> --changes` lists every byte that changes while idle
  from that state, so you can pick a watch that actually moves in the control run.
- `"writers": ["$0251"]` in a spec prints which code wrote those bytes in each run (PC, routine,
  count): see who else touches a byte you freeze, or why a `constant` check saw a stray value.
- `python -m renes states <kb> [LABEL=value ...] --writes $0125 [--frames 60]` plays the matching
  archived states and lists those where code writes the byte (and which code): start states for
  bytes that only change in some situations.

More interventions and inputs:
- A freeze overrides every CPU write to the byte, at any point of the frame. For bytes written and
  used within one frame (scratch, PPU queues), use `"poke_at_pc": {"$C5D2": {"$0050": 32}}` in the
  intervention instead: the value is written each time the instruction at $C5D2 is about to run.
- `[1, "reset"]` as an input step presses the console's Reset button (RAM survives), so a poke made
  before it is what the boot code finds: labels only read at boot become testable.
- `increments_by` with `"changed_only": true` judges only the frames where the byte changed (a
  countdown that waits or reloads); its detail always says how many frames held and how many took
  another step.

## The competing meaning (required to verify)

Before writing checks, name the most plausible **other** meaning of the variable and design a check
that comes out differently under it. Put it in the spec as `"alternative"`. Without it a passing
experiment is recorded but does **not** verify the label.

Example: "$0038 is the facing direction". Alternative: "it is the moving direction". Holding right
while forcing $0038 = left is satisfied by both. A check that separates them: stand still (no
movement), force $0038 = left, and check the player's sprite flips while X stays constant.

```json
{"name": "...", "target": "$0038", "alternative": "moving direction, not facing: then a standing player would not turn", ...}
```

For state or mode variables, prefer `python -m renes visual <kb> probe <$site>` (screenshots per
value) over guessing a single prediction.

## After running

- **PASS:** the label is verified. Report what was predicted and what happened, with numbers.
- **FAIL:** first decide whether the name or the prediction was wrong. Run a diagnostic
  (`"verifies": false`), for example to check whether the game writes the value back right away
  (watch it with `constant`), or try freezing instead of poking. If the evidence contradicts the
  name, propose a rename through `/renes-annotate` instead of forcing a pass.
- Never weaken a check just to make it pass. Report inconclusive results as inconclusive.
- Finish with `python -m renes experiments <kb>` and summarize: verified, refuted, inconclusive,
  plus open questions for the human.
