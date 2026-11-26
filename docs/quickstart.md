# Quick start

This guide takes a new user from an empty checkout to an annotated, byte-identical disassembly
and a running work loop. It assumes Windows (the tested setup), but nothing below is
Windows-only except the paths to the emulator and assembler.

`example.nes` stands for your own ROM throughout. renes names each project after the ROM's file
name: `roms/example.nes` becomes the knowledge base `work/example.db` and the output folder
`out/example/`.

The ten-minute version:

1. Put MesenCE in `tools/mesen/`, cc65 in `tools/cc65/`, and your ROM at `roms/example.nes` (step 1).
2. `python -m renes init roms/example.nes`, then `explore`, `analyze`, `build` (step 2).
3. `python -m renes dashboard` (step 3).
4. In Claude Code: `/renes-research`, accept the facts on the dashboard, then `/loop /renes-cycle`
   (steps 4 and 5).

## 1. What you need

| Tool | Where it goes | Why |
|---|---|---|
| Python 3.12 or newer | on `PATH` | the `renes` CLI (standard library only, plus `pytest` for the tests) |
| [MesenCE](https://github.com/nesdev-org/MesenCE) 2.2+ | `tools/mesen/Mesen.exe` (or set `RENES_MESEN`) | headless emulator: coverage, savestates, RAM access, screenshots |
| [cc65](https://cc65.github.io/) | `tools/cc65/bin/` (or set `RENES_CC65_BIN`) | `ca65`/`ld65` reassemble the output to prove it's byte-identical |
| [Claude Code](https://claude.com/claude-code) | your terminal | does the annotation, experiment design, research and coaching through the skills in `.claude/skills/`. No API key |
| A ROM you own, mapper 0 (NROM) or 7 (AxROM) | `roms/example.nes` | the input. ROMs are never included or shared |

MesenCE must run in portable mode with Lua sockets allowed. Put this `settings.json` next to
`Mesen.exe` (see `docs/adr/0001-emulator.md` for why each setting is needed):

```json
{ "Debug": { "ScriptWindow": {
    "AllowIoOsAccess": true, "AllowNetworkAccess": true, "ScriptTimeout": 3600 } } }
```

Optional: a TypeSafe API key for the Jev exploration policy, in `.env` as
`TYPESAFE_API_KEY=...`. Everything else works without it.

Check the setup:

```
python -m pytest -q
```

Without a ROM this runs the unit tests (about 30 seconds) and skips the ones that drive a real
game in the emulator. To run those too, copy `tests/example_rom.sample.json` to
`tests/example_rom.json` (git-ignored) and fill in the handful of addresses it asks for about a
ROM you own; each field is explained in the sample. The full suite then takes about 5 minutes.

## 2. Your first game

All commands run from the project root. `<kb>` is the knowledge base that `init` creates and
prints: `work/example.db` for `roms/example.nes` (the file name in lower case, with spaces and
punctuation turned into underscores).

```
python -m renes init roms/example.nes       # parse the header, preload hardware labels, static analysis
python -u -m renes explore <kb> --minutes 10 # fuzz the game, record which bytes run as code
python -m renes analyze <kb>                 # code/data split, routines, call graph, jump tables
python -m renes build <kb>                   # render out/<stem>/<stem>.asm and reassemble it
```

`build` must print `OK: ... is byte-identical to ...`. That check runs after every change, so nothing the
tools or Claude do can silently corrupt the program.

If `init` says the mapper is not supported, stop: only mappers 0 and 7 are, so far.

### More coverage (optional, recommended)

Random play gets stuck early. Two ways further:

- **A TAS movie** plays most of the game in minutes. `python -m renes movies <kb>` lists the
  TASVideos movies for the game, marking those made for this exact ROM and those renes can replay
  (`.fm2`, `.bk2`; two-player movies too). `--download <id>` saves one to `roms/tas/<id>M/`; then
  `python -u -m renes tas <kb> roms/tas/<id>M/<id>M.zip`. Check the screenshots in
  `out/<stem>/tas/<id>M/` for desyncs and cap the replay with `--until <frame>` if needed. A movie
  for another revision of the ROM usually desyncs early.
- **The Stage B agent**: in Claude Code, run `/renes-explore`. Claude writes a plan of
  macro-actions, runs `python -m renes agent`, reads the coach report and adjusts.

Run `analyze` and `build` again afterwards.

## 3. Open the dashboard

```
python -m renes dashboard          # all projects in work/; or pass one <kb>
```

It opens http://127.0.0.1:8765 and refreshes itself every few seconds while the tools run in
your terminal. It only listens on this machine, because it shows ROM-derived screenshots and
can change the knowledge base. If a local reverse proxy points a name at it, the server
accepts the hosts `localhost`, `renes.localhost` and `renes` (see `ALLOWED_HOSTS` in
`renes/review.py`).

| Tab | What it's for |
|---|---|
| Projects | one card per ROM in `work/` with coverage, annotation and the loop's next step; pick the project the other tabs show (also in the header) |
| Overview | the next loop step, coverage and annotation numbers, pipeline stages, loop rounds and exploration runs with duration and progress, benchmark history |
| Research | the game brief Claude researched. Accept, edit or reject each fact, and add your own context. Only accepted facts reach the annotator |
| Review | each annotated routine and RAM label with screenshots, a line-by-line register trace and a knock-out comparison. Approve, rename, hint or reject |
| Code | the annotated disassembly as it stands now (rendered live from the knowledge base): routine, data and RAM outline, search, every name links to its definition with address and status, and routines link to their Review item |
| Activity | long-running tasks with progress bars, recent commands and events |

## 4. Research the game, then review it

In Claude Code:

```
/renes-research
```

Claude collects facts from the manual, walkthroughs and gameplay pages (mechanics, vocabulary,
level structure) with sources, and stores them with `python -m renes brief add`. If you
configured a reference disassembly for benchmarking, research runs in *benchmark mode*: RAM maps,
disassemblies and cheat lists are off-limits so the benchmark stays honest.

Open the **Research** tab and review the facts. The loop doesn't continue until you have.

## 5. Run the loop

```
/loop /renes-cycle
```

Each round, `python -m renes round start <kb>` picks one stage by fixed rules and records the
project metrics. Claude does that stage, rebuilds, and closes the round with
`python -m renes round end <kb> --note "..."`.

| Stage | Chosen when |
|---|---|
| research | there's no game brief yet |
| review (stop) | research facts await your review |
| benchmark | a reference is configured and 40+ routines were annotated since the last one |
| experiment | 25+ routines annotated since the last experiment and 5+ untested RAM candidates |
| evidence | 20+ annotated routines lack screenshots and traces |
| annotate | routines are ready (all their callees are annotated) |
| probe | tile sheets, metatiles or jump-table pictures are missing |
| explore | nothing is ready and the remaining routines never ran |
| audit, experiment, evidence, annotate | finishing: annotation is complete and a finishing check fails (see below) |
| done (stop) | every finishing check passes: the loop exports the project and releases it |
| review (stop) | nothing is left that the loop can do alone |

The loop also **stops when it stalls**: when the same stage made no progress in 2 rounds in a
row, or no round made progress in 3. Progress means any of these went up: routines
annotated or revised, code bytes executed, RAM labels verified, experiments run, routines with
evidence, benchmarks, research facts, approved labels. A review decision on the dashboard, or
`python -m renes round resume <kb>`, lifts the stop.

`python -m renes round list <kb>` shows each round's duration and progress; the Overview tab
shows the same.

### Several agents at once

[multi-agent.md](multi-agent.md) is the full guide. In short: run one loop per game. Each agent picks a name and starts rounds with
`python -m renes round start <kb> --agent <name>` (or sets `RENES_AGENT`): that claims the project,
and another agent's round start on the same game is refused until the claim has been idle for
2 hours. `python -m renes claim <kb>` shows who holds it (`release` gives it up). Scratch files are
per project (`work/<project>.proposal.json`), and the Projects tab shows each project's owner.

Routines whose code matches code another project already annotated get that annotation as a hint
in their work unit (`python -m renes library <kb>`; refreshed at every round start).

Loop agents report what got in their way (bugs, tool limits, ideas) with `python -m renes feedback add`.
Whoever develops renes reads them with `python -m renes feedback list` and answers with
`feedback close --id N "<what changed>"`; the reply is printed at that project's next round start.
While working on notes, `feedback ack --id 4,9 "<plan>"` tells the agents behind them a fix is
coming, and `feedback patch "<what changed>" --id 4,9` publishes a patch note to every project and
closes the notes it fixes; each round start prints the messages the project hasn't seen
(`feedback news` lists them again). Notes with quotes, dashes or `$addresses` go in a file:
`feedback add --file note.txt`.
Both sides can wait without spending tokens: `python -m renes feedback wait` (the developer) exits
when a new note is filed, `feedback wait --project <kb>` (an agent whose loop stopped) exits when
a reply arrives. Run as a background command, its exit wakes that Claude session to act.

### Finishing a game

Once every routine is annotated, the loop works through a checklist (`python -m renes finish <kb>`):
pictures done, evidence for every annotated routine that runs, an experiment round after the last
annotation, a recent audit with at least 70% of its sample supported (and the names it sent back
re-checked), and a byte-identical build. When all pass, the loop stops at `done` and runs
`python -m renes export <kb>`, which writes `out/<stem>/export/`: the annotated source (rebuilds the
ROM with ca65), a README with the numbers, the routines by subsystem, the RAM map and the tile and
metatile sheets. The export contains the game's code: keep it local.

### How good are the names?

- With a reference disassembly: `python -m renes grade <kb> prepare`, have a separate agent grade the
  packet with the `renes-grade` skill, then `grade <kb> record` (aggregates only).
- Without one: `python -m renes audit <kb> prepare`, a separate agent judges with `renes-audit` whether
  the code supports each name, then `audit <kb> record`. Project cards show the latest numbers.

## 6. Review and steer

Open the **Review** tab. Low-confidence names come first. For each item:

- **Approve** locks the name (status `approved`).
- **Rename** approves your name instead and rewrites every summary and comment that used the old one.
- **Hint** sends a routine back to the front of the annotation queue with your note.
- **Reject** removes a wrong label.

Experiments promote labels from `candidate` to `verified`; only you make them `approved`.
The `.asm` marks unreviewed labels and comments with `;?`.

## 7. Where things are

| Path | Contents | Share? |
|---|---|---|
| `work/<stem>.db` | the knowledge base: the source of truth | no (contains ROM bytes) |
| `out/<stem>/<stem>.asm` | the generated annotated disassembly; never edit it by hand | no |
| `roms/`, `refs/` | your ROMs, TAS movies and reference disassemblies | no |
| `work/<stem>.proposal.json`, `work/<stem>.experiment.json` | the latest batch Claude applied (one per project) | no |
| `work/feedback.sqlite3` | agent feedback notes, acks and patch notes (shared by all projects) | no |
| `out/<stem>/export/` | a finished project's export | no (contains the game's code) |
| `renes/`, `tests/`, `.claude/skills/`, `docs/` | the harness itself | yes |

`roms/`, `work/`, `out/`, `refs/`, `tools/` and `.env` are in `.gitignore`.

## Troubleshooting

- **`build` prints `MISMATCH at file offset ...`**: `apply` rolls back any batch that breaks
  the round trip, so this points at the analysis or renderer. Rerun `analyze`, then `build`; if it
  persists, the offset tells you where the rendered `.asm` assembles differently.
- **Mesen exits immediately**: check `settings.json` (sockets and IO access) and that
  `tools/mesen/Mesen.exe` exists, or set `RENES_MESEN`.
- **TAS replay is off**: a checksum mismatch means a different ROM revision. A desync shows in
  the screenshots as a death, a restart or the title screen. Use `--until` on the last good frame.
- **The loop keeps saying review**: read the reason. It's either your turn (research facts or
  review items) or a stall; fix the cause, then make a review decision or run `round resume`.
