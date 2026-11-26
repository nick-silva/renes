---
name: renes-explore
description: Coach the renes Stage B explorer. Runs the macro-action agent in Mesen (novelty, random or Jev policy), reads its report, and rewrites the exploration plan (goal, action menu, watched variables, progress measure, start state, pokes/freezes) to break plateaus and reach unexplored code. Use when asked to explore further, get past a point in the game, run Jev, or raise code coverage beyond what the fuzzer reached.
argument-hint: "[rounds=3] [jev [max-calls]] [kb=work/<game>.db]"
---

# Explore with a coach (renes M6)

The agent plays in **macro-actions** from a menu. A policy picks each one from the labeled game
state. You are the **coach**: between runs you read what happened and rewrite the plan. The agent
stops by itself and asks for you when nothing new happens for `coach_after` steps.

## Commands

- `python -m renes agent <kb> [--policy novelty|random|jev] [--steps 300] [--max-calls 100] [--seed N]`
  runs the agent, merges coverage into the KB and writes `work/<project>.explore_report.md`.
- `python -m renes agent <kb> --init-plan` (re)writes the default plan at `work/<project>.explore_plan.json`.
- `python -m renes report <kb> [--run N]` prints the report for any agent run.
- `python -m renes explore <kb> --frames N` is the Stage A fuzzer, the baseline at equal frames.
  `--state <id>` fuzzes from an archived state (a TAS state, a mode the fuzzer never reaches) and
  `--setup '[[60,"none"],[4,"select"]]'` plays menu inputs first.
- `python -m renes coverage <kb> --unclassified` lists the ROM ranges nothing classified yet, between
  which labels and with the routines whose operands point there: targets for the next run.

## Policies and cost

- `novelty` is local and free. Use it for most rounds.
- `random` is an ablation. It shows whether the chooser matters or only the menu does.
- `jev` calls TypeSafe Jev about once per step (around 1,200 input tokens, 160 ms).
  **Jev credit is limited and has no auto-recharge.** Ask the user before any run with
  `--max-calls` above 150, and report the calls and tokens used. If Jev fails (credit, network),
  the run continues with novelty and the report says why.

## Loop (per round)

1. Run the agent (novelty unless asked otherwise). Run it in the background if over ~3 minutes.
2. Read `work/<project>.explore_report.md`:
   - **New beyond all earlier runs**: the number that matters. Code bytes no run had executed.
   - **Best progress / doomed records**: how far it got, and progress records that kept failing
     (for example a state mid-fall into a pit).
   - **Actions table**: which macros find things, which are never useful.
   - **Where time went**: situations where many steps found nothing.
   - **Unreached code**: the largest never-executed routines, with callers and annotated summaries.
     They hint at what must happen first (an annotated caller like `HandleDoorEntry` says "enter a door").
3. Change **one or two things** in `work/<project>.explore_plan.json`, then rerun:
   - **Stuck at an obstacle**: look at the best state (screenshot it with `Mesen.screenshot` from
     `"start": {"state": <id>}`) and add a macro that handles it (run-up jumps, back up then sprint,
     hold jump longer, duck into a doorway: `[[30, "down"]]`).
   - **No progress measure**: set `"progress": {"lo": <x label>, "hi": <page label>}` once position
     labels exist (verified ones are best).
   - **Same area over and over**: start later with `"start": {"state": <best usable state>}`.
   - **Skill gate (an unbeatable section)**: freeze or poke a *verified* variable (lives,
     timer, level number) with `"freeze"` / `"poke"`. Only use addresses this project has
     labeled and verified through `/renes-experiment`. **Never use RAM addresses recalled from
     published disassemblies or cheat lists for this game.** That would contaminate the benchmark.
   - **Actions that never find anything** can be removed. A smaller menu helps Jev.
   - **Goal text**: Jev reads it. Make it concrete ("get past the pit at the end of the screen").
4. Stop after the requested number of rounds, or when two rounds in a row find no new code.

## After exploring

- `python -m renes analyze <kb>` then `python -m renes build <kb>` so new coverage reaches the
  disassembly (the build must stay byte-identical).
- Report per round: policy, steps/frames, new code bytes, best progress, what you changed and why.
- Suggest follow-ups: new variables worth an experiment (for example the byte that changes when a
  new area loads), or routines that are now reached and ready for `/renes-annotate`.

## Plan format (`work/<project>.explore_plan.json`)

```json
{
  "goal": "Get as far right as possible and reach new areas.",
  "start": {"setup": [[60, "none"], [4, "start"], [240, "none"]]},
  "poke": {}, "freeze": {},
  "watch": ["Object_X_Position", "Object_X_Page", "Player_X_Speed"],
  "progress": {"lo": "Object_X_Position", "hi": "Object_X_Page"},
  "actions": {
    "sprint_jump_right": {"description": "Build speed, then a long jump right", "inputs": [[40, "right+b"], [35, "right+a+b"]]}
  },
  "builtins": ["restore_frontier", "restore_best"],
  "restore_after": 15,
  "coach_after": 150
}
```

Action ids are lowercase `snake_case`. Each action runs 1–600 frames, and buttons are joined with
`+` (`a b select start up down left right`). Watched variables must be RAM labels or `$addr`. The
policy only ever sees these named values, never raw RAM.
