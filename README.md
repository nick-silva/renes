# renes

**Start here:** [Quick start](docs/quickstart.md) · [Running several agents](docs/multi-agent.md)

renes turns an NES ROM you own into an annotated 6502 disassembly: named routines, RAM variables
and data tables, with comments that explain the logic. The output reassembles byte-for-byte into
the original ROM, and every name records the evidence behind it and whether a human has checked it.

It is a harness around three things you install yourself: an emulator (MesenCE) that plays the
game and records what the program does, an assembler (cc65) that proves the output is still the
same program, and Claude Code, which does the naming and designs the experiments. renes itself is
plain Python with no dependencies.

No ROMs, game assets or disassemblies are included, and none of the examples in this repository
name a game. Wherever you see `example.nes`, use your own file.

## What it does

Reverse engineering a game by hand is mostly two slow loops: poking RAM in an emulator to see what
changes, and keeping track of thousands of addresses in a disassembly. renes automates both and
leaves the judgment calls to a person.

```
 ROM ──► explore ──► analyze ──► annotate ──► experiment ──► evidence ──► review
          │            │            │             │              │           │
   fuzzer, agent,   code/data   Claude names   freeze/poke   traces and   you approve,
   TAS movies run   split from  routines and   RAM to prove  knock-out    rename, hint
   the game and     what ran;   variables,     a name        screenshots  or reject
   record coverage  call graph  bottom-up      (verified)    per routine
                                     │
                     every change ──► reassemble ──► byte-identical, or rolled back
```

1. **Explore.** The game is played headless in the emulator by a fuzzer with a savestate archive,
   by a macro-action agent, or by replaying a tool-assisted speedrun movie. The emulator's
   Code/Data Logger records which ROM bytes ran as code and which were read as data.
2. **Analyze.** That coverage, plus static analysis from the vectors, gives the code/data split,
   routine boundaries, the call graph and jump tables.
3. **Annotate.** Python prepares one self-contained work unit per routine (its code, callers,
   callees, the RAM it touches). Claude, inside Claude Code, proposes names and comments as JSON.
   Python validates the proposal, applies it, reassembles, and rolls it back if the ROM differs.
4. **Experiment.** A name starts as a `candidate`. An A/B run in the emulator (for example
   "freeze this byte and the on-screen timer stops") makes it `verified`.
5. **Evidence and review.** For each routine renes records a register trace of one real call and
   screenshots with the routine running normally and knocked out. On a local dashboard you
   approve, rename, hint or reject. Only a person makes a name `approved`.

## How the pieces fit

| Piece | Where | Role |
|---|---|---|
| Knowledge base | `work/<stem>.db` (SQLite, one per ROM) | The source of truth: ROM bytes, labels, comments, coverage, experiments, screenshots. The `.asm` is rendered from it, so nothing ever edits assembly text |
| CLI | `renes/` (`python -m renes <command>`) | Every deterministic step: analysis, work units, applying proposals, experiments, rendering, verification |
| Emulator bridge | `renes/emu/` | Starts MesenCE headless and talks to a Lua script in it over a local socket: step, input, RAM, savestates, coverage, screenshots |
| Round-trip gate | `renes/render.py`, `renes/verify.py` | Renders ca65 source, assembles it with cc65 and compares with the ROM after every change |
| Skills | `.claude/skills/` | What Claude Code runs: `/renes-annotate`, `/renes-experiment`, `/renes-explore`, `/renes-research`, `/renes-cycle`, and `/renes-grade` and `/renes-audit` for a separate checking agent |
| Work loop | `/loop /renes-cycle` | Each round the CLI picks the next stage by fixed rules, Claude does it, and the round's duration and progress are recorded. The loop stops when it needs you or stops making progress |
| Dashboard | `python -m renes dashboard` | A local page (this machine only) for every project in `work/`: progress, research facts to accept, the review queue, the ROM and RAM maps, live code, activity |

Quality is checked by someone other than the annotator. Where a reference disassembly exists, an
isolated grader agent compares names with it and only aggregate scores come back. Where none
exists, an auditor agent judges whether each routine's code supports its name, and unsupported
names return to the annotation queue with the reason.

## Requirements

| | Version used in development | Notes |
|---|---|---|
| Python | 3.14 (3.12 or newer) | standard library only; `pytest` for the tests |
| [MesenCE](https://github.com/nesdev-org/MesenCE) | 2.2.1 | at `tools/mesen/Mesen.exe` or `RENES_MESEN`; needs the settings in the quick start |
| [cc65](https://cc65.github.io/) | current release | `ca65`/`ld65` at `tools/cc65/bin/` or `RENES_CC65_BIN` |
| [Claude Code](https://claude.com/claude-code) | current | runs the skills; a subscription is enough, no API key |
| A ROM you own | iNES file, mapper 0 (NROM) or 7 (AxROM) | goes in `roms/`, which is git-ignored |

Developed and tested on Windows 11. Nothing in the Python is Windows-only, but the emulator path
defaults and `scripts/install_dashboard_task.ps1` are.

Optional: a TypeSafe API key (`TYPESAFE_API_KEY` in `.env`) lets the exploration agent use the Jev
model as its policy. Everything else works without it.

## Getting started

**[docs/quickstart.md](docs/quickstart.md)** goes from an empty checkout to a byte-identical
build, the dashboard and a running loop. The short version:

```
python -m renes init roms/example.nes
python -u -m renes explore work/example.db --minutes 10
python -m renes analyze work/example.db
python -m renes build work/example.db        # must report byte-identical
python -m renes dashboard
```

Then, in Claude Code: `/renes-research`, review the facts on the dashboard, and `/loop /renes-cycle`.

Running several games at once, each with its own agent, is covered in
**[docs/multi-agent.md](docs/multi-agent.md)**.

## Commands and skills

| Command | Does |
|---|---|
| `init`, `analyze`, `build` | create a knowledge base, re-run the code/data analysis, render and verify the round trip |
| `explore`, `agent`, `tas`, `coverage` | fuzz, run the exploration agent, replay a TAS movie, summarize coverage |
| `next`, `apply`, `progress` | annotation work units, apply a proposal (validated, rolled back on failure), progress |
| `experiment`, `experiments`, `probe` | A/B experiments that verify labels, their history, the button probe |
| `evidence` | call traces and knock-out screenshots for the review page |
| `visual` | pictures for the annotator: tile sheets, jump tables, screenshots of each state or object type |
| `brief`, `codes` | the game research brief; cheat codes as research input or as a held-out test set |
| `benchmark`, `status`, `round` | aggregate benchmark, the loop's next stage, loop rounds |
| `movies`, `states` | find and download TAS movies for the ROM; list archived savestates by RAM values |
| `grade`, `audit`, `confidence`, `rework` | quality: reference grading, reference-free audit, evidence against claimed confidence, weak names back to the queue |
| `finish`, `export` | the finishing checklist; the hand-over package of a finished project |
| `claim`, `library`, `feedback` | several agents: project claims, code shared between games, agent notes and patch notes |
| `dashboard` (alias `review`) | the local dashboard |

`python -m renes <command> -h` lists the options.

| Skill (in Claude Code) | Does |
|---|---|
| `/renes-pipeline` | setup and the deterministic steps: new ROM, explore, TAS, rebuild, evidence |
| `/renes-annotate` | annotate routines bottom-up along the call graph |
| `/renes-experiment` | design and run experiments that verify or refute labels |
| `/renes-explore` | coach the exploration agent |
| `/renes-research` | build the game brief from manuals and walkthroughs |
| `/renes-cycle` | one round of the work loop; repeat it with `/loop /renes-cycle` |
| `/renes-grade`, `/renes-audit` | for a separate agent only: grade names against a reference, or audit them against the code |

## What to expect

Measured during development on four early 24-40 KB NROM games. One of them, the benchmark game,
has a published disassembly that the annotator never saw:

- On the benchmark game, an isolated grader judged 69% of 150 sampled names to mean the same as
  the reference's and 99% to describe the right behaviour, although only 15% match word for word.
  Two independent graders agreed with kappa 0.93.
- On two games without a reference, the audit found 87% and 79% of the sampled names supported by
  the code.
- The annotator's own confidence predicts accuracy. The evidence score alone doesn't.

Two cautions. The audit follows the mechanics well but can accept a name that describes the right
mechanics of the wrong game object. And a benchmark on a well-known game overstates accuracy,
because a model may have memorized parts of a published disassembly; results on obscure games are
the ones to trust.

## Status and limits

- Mappers 0 (NROM) and 7 (AxROM). In AxROM games the cross-game library, jump-table probes, cheat
  codes, metatiles and the benchmark still cover bank 0 only. Other mappers are not supported.
- NTSC movies only for TAS replay (`.fm2`, `.bk2`).
- Open work is in [docs/backlog.md](docs/backlog.md); milestones and design decisions are in
  [the PRD](.claude/prds/nes-re-harness.prd.md) and [docs/adr/](docs/adr/).

## Repository layout

```
renes/            the CLI and its modules; emu/ is the emulator bridge
.claude/skills/   the Claude Code skills
tests/            unit tests, plus emulator tests that run when you describe a ROM (see the quick start)
docs/             quickstart.md, multi-agent.md, backlog.md, adr/, research/
scripts/          install_dashboard_task.ps1: start the dashboard at logon (Windows)
spikes/           the first feasibility experiments, kept for reference
roms/ work/ out/ refs/ tools/   yours, git-ignored: ROMs and movies, knowledge bases, generated
                  output, reference disassemblies, the emulator and assembler
```

## License

renes is released under the MIT License: see [LICENSE](LICENSE). MesenCE, cc65 and Claude Code are separate
programs you install yourself, under their own terms.

## Copyright

No ROMs, game assets or disassemblies are included. Knowledge bases (`work/`), generated output
(`out/`), ROMs, TAS movies and reference material contain or are derived from copyrighted games.
They are git-ignored; don't distribute them. Use renes only with games you own.
