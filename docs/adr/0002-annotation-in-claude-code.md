# ADR 0002: Annotation runs in Claude Code, not through an API

**Status**: Accepted (2026-10-17)

## Context

The original PRD called the Claude API with an API key for annotation (M4) and coaching. The
user prefers to run the LLM work from Claude Code on their subscription, packaged as skills.

## Decision

- Python does the deterministic work; Claude Code does the reasoning:
  - `python -m renes next <kb> --count N` emits self-contained work units, bottom-up along the
    call graph. Each includes the routine's code with current names, callers, callee summaries
    (including fall-through targets), the RAM it touches with game-wide read/write counts, and
    the ROM data it references with preview bytes.
  - Claude writes a JSON proposal to `work/proposal.json`.
  - `python -m renes apply <kb> work/proposal.json` validates names (ca65-safe, unique, not
    reserved, never overwriting verified or approved labels) and addresses, writes everything as
    `candidate`, then re-renders, reassembles and byte-compares. On any failure the whole batch
    is rolled back.
- Skills in `.claude/skills/`: `renes-annotate` (annotation and its conventions), `renes-pipeline`
  (init, explore, TAS, analyze, build, evidence, review), `renes-experiment` (A/B experiments),
  `renes-explore` (the Stage B coach), `renes-research` (the game brief) and `renes-cycle` (one
  round of the work loop, repeated with `/loop /renes-cycle`).
- The annotation prompt tells Claude **not to recall names from published disassemblies**, to
  keep the benchmark meaningful.
- Jev (TypeSafe) still uses its own API key for M6 exploration; it isn't a Claude model.

## Consequences

- No API key or per-token billing. Throughput depends on subscription usage limits; a work
  unit is small (tens of lines), so batches of 5 per `apply` are cheap.
- The human sees the work happen in their own Claude Code session and can steer it directly.
- The coach (M6) runs as a skill too (`/renes-explore`), so exploration is turn-based: explore,
  read the coach report, adjust the plan, explore.
- Long unattended runs use `/loop /renes-cycle`. The `round` command records each round's
  duration and progress, and stops the loop when rounds stop making progress, so a loop can't
  burn usage on a stage that keeps failing.
