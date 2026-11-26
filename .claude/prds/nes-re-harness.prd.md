# PRD: NES Reverse-Engineering Harness ("renes")

**Status**: v0.2 (2026-10-18): M0–M7 done, M8 in progress, M9 pending
**Owner**: the project author
**Complexity**: Large

## 1. Problem

Producing a fully annotated, reassemblable disassembly of an NES game (the model is the
well-known community disassembly of a classic NROM platformer) takes a
human weeks or months. Most of that time goes into two tedious loops:

1. **Dynamic poke-and-check** (Tom Reich's workflow): search RAM for a value, play, narrow it
   down, set a write breakpoint, find the instruction that touched it, step through the branch
   logic.
2. **Static bookkeeping** (Henrique's workflow): split the iNES file, disassemble PRG-ROM,
   separate code from data, name every routine, variable and table, and comment the logic.

Both loops can be automated. An agent can drive the emulator to generate evidence, and an LLM can
turn that evidence into names and comments. That leaves the human to direct the work and verify
the results.

## 2. Goal

Given an NES ROM the user owns, the system produces an annotated `.asm` file in the style of
that disassembly that:

- **reassembles byte-for-byte into the original ROM** (this is non-negotiable and is the main
  correctness check), and
- names RAM variables, routines, data tables and constants, with comments explaining the logic,
  and
- backs every name with recorded evidence and a confidence level, so a human can audit it.

## 3. Non-goals (v1)

- Beating games, or playing them well. The agent plays to **find code it hasn't run yet**, not
  to win.
- Mappers other than NROM (mapper 0) in the MVP. Bank-switched games come in a later milestone.
- Decompiling to C. Pseudocode can appear in comments, but the deliverable is assembly.
- Distributing disassemblies. The output contains copyrighted code and stays private.

## 4. Users and roles

| Role | Who | Does |
|---|---|---|
| Director | Human | Sets focus ("audio engine today"), provides hints ("16x16 metatiles"), approves or rejects labels |
| Explorer | Jev (TypeSafe AI, model `jev`) plus a fuzzer | Drives the game toward unexplored code and states, quickly and cheaply |
| Coach | Claude, inside Claude Code (`/renes-explore`) | Spots loops or stalls in exploration and rewrites the explorer's goals and action menus |
| Annotator | Claude, inside Claude Code (`/renes-annotate`) | Reads evidence plus disassembly, proposes names, comments and pseudocode as **structured data** |
| Verifier | Deterministic code | Runs experiments (freeze or poke RAM) to confirm hypotheses; reassembles and diffs the ROM |

## 5. Key design decisions (corrections to the original concept)

1. **The knowledge base is the source of truth, not the `.asm` text.** The LLM never rewrites
   assembly. It writes JSON proposals (label, comment, type, evidence references) into a
   database. A deterministic renderer produces the `.asm` file. This makes it impossible for
   the LLM to silently corrupt an instruction, and the round-trip check proves it.
2. **Separating code from data is the hard problem, and dynamic play solves it.** `da65` alone
   cannot tell code from data, so pointer tables and level data get decoded as junk
   instructions. Instead, the emulator's **Code/Data Logger (CDL)** records which bytes were
   executed as code and which were read as data. The share of PRG-ROM that CDL has covered is
   the main measure of exploration progress.
3. **Jev has a bootstrap problem.** Jev needs a structured state snapshot and a menu of legal
   actions, but in an unknown game we don't know what the RAM means yet. Exploration therefore
   happens in stages:
   - Stage A: input fuzzing plus a savestate archive (Go-Explore style). The fuzzer keeps
     states that reach new CDL coverage and new RAM patterns, and restarts from them.
   - Stage B: once a handful of variables are labeled (player X/Y, game mode, level or area,
     lives), Jev receives that state and a menu of macro-actions ("walk right 30 frames", "jump
     and hold", "go back to savestate #12") and is rewarded for novelty.
   - Coach: when coverage plateaus or Jev loops (as happened in the Pokémon Red run), the LLM
     rewrites the goal and the action menu.
4. **Every claim is a hypothesis until an experiment confirms it.** Correlation ("$0048 dropped
   when we died") produces a *candidate* label. An experiment (freeze $0048, die, check that
   the game continues) promotes it to *verified*. Human approval makes it *approved*. Dynamic
   evidence greatly reduces hallucination but does not eliminate it, so the status ladder
   stays visible in the output.
5. **Annotate bottom-up along the call graph.** Leaf routines are annotated first. Callers then
   see their callees' names, so context builds up instead of every routine being guessed in
   isolation.
6. **Corrections propagate.** When a human renames `Enemy_Speed` to `Anim_Frame_Timer`, every
   summary and comment that mentions the old name is rewritten (whole words only), and a hint
   sends the routine back to the front of the annotation queue for regeneration. *As built:*
   propagation is by name, not by a recorded dependency graph.
7. **Free facts are preloaded.** PPU/APU/IO registers ($2000–$2007, $4000–$4017), the vectors
   at $FFFA–$FFFF (NMI, RESET, IRQ), and common idioms (the `JumpEngine` pattern of an indirect
   jump table, controller-read loops, OAM DMA via $4014) are recognized before any LLM call.

## 6. System components

| # | Component | Responsibility |
|---|---|---|
| A | ROM loader | Parse iNES/NES 2.0 header, detect mapper, split PRG/CHR, hash the ROM |
| B | Emulator bridge | Run headless Mesen 2 under script control: step frames, set inputs, save/load states, read/write RAM, memory-access callbacks, CDL, trace log |
| C | Recorder | Per-frame RAM snapshots or diffs, an access log (address → reading/writing PCs), JSR/RTS call traces, OAM/PPU snapshots, screenshots on demand |
| D | Explorer | Fuzzer plus savestate archive (Stage A), Jev agent (Stage B), LLM coach |
| E | Variable discovery | Correlate RAM with inputs and events; propose candidates; design and run freeze/poke experiments |
| F | Static analyzer | CDL-guided disassembly, routine boundaries, call graph, jump-table detection, data-table extents |
| G | Knowledge base | SQLite: addresses, routines, labels, comments, types, evidence, confidence, status, provenance, dependencies |
| H | Annotator | Packs work units (routine plus known labels plus evidence) → LLM → validated JSON → KB |
| I | Renderer + verifier | Render KB → `.asm` (ca65 syntax, the reference's style); assemble; byte-compare with the original PRG |
| J | Review interface | Queue of candidates and conflicts; approve, reject or rename; set focus; add hints |

## 7. Output format (target)

Modeled on the reference disassembly:

- A header block, then a defines section of hardware registers and RAM variables
  (`Player_Lives = $0048`), each with a one-line comment.
- Named routines (`PlayerLoseLife:`), local branch labels, and comments on lines with
  non-obvious logic.
- Data tables with named labels and `.db` / `.dw` directives, and pointer tables expressed
  symbolically.
- Optionally, a confidence marker on each label that isn't yet approved (for example `;?`), so
  readers know what is unverified.
- Assembles with `ca65`/`ld65` (or `asm6`) into a ROM identical to the input.

## 8. Success metrics

| Metric | MVP target |
|---|---|
| Round-trip byte-identical | 100% (hard gate on every render) |
| PRG bytes classified as code or data (CDL) | ≥ 90% for the benchmark game |
| RAM bytes with a verified label (of those ever written) | ≥ 60% |
| Routines named | ≥ 80%, ≥ 50% human-approved |
| Benchmark agreement vs. the reference disassembly | Routine boundaries ≥ 90%; label meanings judged equivalent ≥ 70% |
| Cost per game (LLM + Jev) | Tracked; budget TBD |

**Benchmark caveat:** frontier LLMs have very likely memorized the reference disassembly. The benchmark game is
useful for checking the pipeline, but it overstates the annotator's real accuracy. Validate
on at least one sparsely documented NROM game as well, and keep the reference disassembly out
of every prompt.

## 9. Tech stack (defaults, open to change)

- Python 3.12+ orchestrator (standard library only; tested on 3.14); SQLite knowledge base
- MesenCE (`nesdev-org/MesenCE`; the original Mesen2 repo was archived in June 2026) as the
  emulator. Run headless via `--testRunner`; a Lua script connects to the Python orchestrator
  over LuaSocket. Its Lua API covers memory callbacks with value override, CDL, access
  counters and savestates. It has **no trace-logger API**, so access logs come from callbacks.
  Fallback for raw fuzzing speed: `cynes` (no CDL or callbacks). See
  `docs/research/m0-spikes.md`
- Jev is called with `choice` questions (at most 255 options) over macro-actions, at about
  100 ms per call. Python owns the emulator clock, so latency only limits throughput
- Own 6502 disassembler and CDL-guided analysis (`renes/opcodes.py`, `renes/analyze.py`); the
  cc65 suite's `ca65`/`ld65` for reassembly. `da65` turned out not to be needed
- **Annotation runs inside Claude Code on the user's subscription. There's no Anthropic API
  key.** The `renes` CLI emits work units (`next`) and validates and applies proposals (`apply`);
  the `/renes-annotate` skill is the loop and `/renes-pipeline` runs the rest. See
  `docs/adr/0002-annotation-in-claude-code.md`
- TypeSafe Jev API (model `jev`) for Stage B exploration

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| MesenCE can't easily be driven from Python headless (sockets off by default and only enabled in `settings.json`; test-runner `--timeout`; savestates only allowed inside exec callbacks) | Medium | M0 spikes S1–S4; fall back to cynes for fuzzing |
| Logging every RAM access through Lua callbacks is too slow | Medium | Use access counters for broad sweeps; full callbacks only on targeted ranges |
| Exploration stalls behind skill gates (hard jumps, bosses) | High | Savestate archive, RAM poking to skip (warp, invincibility once labeled), coach |
| Data reached only through indirect pointers is misclassified | High | Jump-table and pointer-table heuristics, plus human hints, plus round-trip safety |
| LLM gives confident but wrong names | High | Status ladder, experiments, dependency-tracked regeneration, human review |
| Jev API maturity, access or pricing changes (launched Sept 2026) | Medium | Stage A works without Jev; keep the explorer interface model-agnostic |
| LLM cost on large games | Medium | Bottom-up work units, caching, cheap models for triage |
| Bank switching breaks the MVP's assumptions | Certain later | Keep the KB keyed by (bank, address) from day one |

## 11. Delivery Milestones

| # | Milestone | Outcome | Status | Plan |
|---|---|---|---|---|
| M0 | Spikes | MesenCE is driven headless from Python (step, input, RAM r/w, savestate, CDL, freeze); a Jev API hello-world; ADR 0001. Spikes S1–S7 in `docs/research/m0-spikes.md` | done (2026-10-17) | `docs/adr/0001-emulator.md` |
| M1 | Static baseline + round trip | Load an NROM ROM → naive KB → render → reassemble byte-identical; hardware labels and vectors preloaded | done (2026-10-17): both the benchmark game revisions byte-identical; static descent reaches 7.1% of PRG | `renes/`, `tests/` |
| M2 | Recorder + fuzzer | Random/fuzzed play with a savestate archive; CDL coverage, access log and call traces stored | done (2026-10-17): 10-min the benchmark game run → 35.2% code + 5.3% data, 184 subroutine entries; plateaus early in the first level | `renes/explore.py`, `renes/emu/` |
| M3 | CDL-guided static analysis | Code/data split, routine boundaries, call graph, jump tables in the KB; output still round-trips | done (2026-10-17): The benchmark game 69.3% code (11.5K proven by execution, 11.2K static), one jump engine + 18 tables, 457 routines, 834 call edges; byte-identical | `renes/analyze.py` |
| M4 | Annotator v1 | Bottom-up LLM annotation into the KB as JSON; renderer emits names and comments; CLI review | done (2026-10-18): tooling + skills; annotation continues through the loop (see M8 for current counts) | `renes/annotate.py`, `.claude/skills/` |
| M5 | Variable discovery + experiments | Correlation → candidate → freeze/poke experiment → verified | done (2026-10-18): A/B runner + button probe + `/renes-experiment`; the benchmark game 9 labels verified, one prediction refuted 3x (stays candidate) | `renes/experiment.py` |
| M6 | Jev explorer + coach | Stage B exploration with labeled state; coach breaks loops; coverage beats the fuzzer alone | done (2026-10-18): `renes agent` (novelty/random/Jev policies, loop guard, doomed-state detection) + `/renes-explore` coach. The benchmark game: coached agent found 352 code bytes 164K fuzzer frames never ran; Jev got more than twice as far into the level in 150 steps as novelty; equal-frame coverage inside the first level is a tie | `.claude/skills/renes-explore/SKILL.md` |
| M6b | TAS replay | Replay TASVideos movies (.fm2/.bk2) for coverage, savestates across the game and rare-change RAM candidates; sync checked by screenshots, `--until`/`--fix` | done (2026-10-18): a published TAS movie of the benchmark game stays in sync for 17.8K of its 67K frames on MesenCE; exec-proven code 11.5K -> 19.2K bytes; level/lives/power-up candidates found | `renes/tas.py` |
| M7 | Review UI + propagation | Review queue, focus and hints, dependency-tracked regeneration | done (2026-10-18): local dashboard (`renes dashboard`: Overview with pipeline stages, validation and history; Research review gate; Activity with live task progress; polling) and review page (`renes review`, 127.0.0.1 only: ROM-derived screenshots never leave the machine) with experiment screenshot strips, routine knock-outs (normal vs patched-to-RTS, labeled-RAM effect) and line-by-line register traces; approve/rename/hint/reject with name propagation and a rework queue; game brief (`/renes-research`, benchmark-safe source policy); Game Genie decoder, research codes and held-out test codes; loop stages research + evidence | `renes/review.py`, `renes/evidence.py`, `renes/codes.py` |
| M8 | Benchmark | The benchmark game vs. the reference disassembly, plus one obscure NROM game; report the metrics | in progress (2026-10-18): `renes benchmark` (aggregate-only, reference never shown to the annotator) + `renes status` / `/renes-cycle` loop. The benchmark game #1: split agreement 100% on 71% classified, 99.8% labels found, names 16% token match (verified 67%, routines 1%). Loop rounds (`renes round`) record duration and progress per round; the loop stops on review gates, when nothing is left, or when rounds stall (same stage 2x or any 3x without progress); shown on the dashboard. First full loop run (2026-10-18, 56 rounds, ~85 min): annotation 94 -> 455/456 routines, 33 RAM labels verified (47 experiments, 36 passed), evidence for 391 routines, every build byte-identical; stopped by the stall rule after 2 explore rounds found no new code. Benchmark: split 100% on 71% classified, names 12% match (RAM 32%, routines 8%, verified 36%); confidence above 0.5 no longer predicts the match rate. Obscure game pending | `renes/benchmark.py`, `renes/cycle.py` |
| M9 | Mapper support | MMC1 and MMC3: per-bank disassembly, bank-aware KB and call graph | pending | |

## 12. Open questions

Answered so far:

1. First target: a classic NROM platformer with a published disassembly, used as the benchmark.
2. Emulator: MesenCE (ADR 0001).
3. Budget: Claude runs on the subscription inside Claude Code (ADR 0002); Jev credit is small, so
   the loop never spends it unless the user says so in that session.
4. Review interface: a local web dashboard (`renes dashboard`), polling the knowledge base.
5. Naming style: the reference's conventions (`Capitalized_Snake_Case` RAM, `CamelCase` routines),
   enforced by the `/renes-annotate` skill, never copied from the reference.

Still open:

6. How much human review time per session to expect (the stall rule and review gates assume a
   human checks in between loop runs).
7. Which sparsely documented NROM game to use as the second benchmark.
