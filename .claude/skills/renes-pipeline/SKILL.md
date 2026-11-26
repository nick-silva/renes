---
name: renes-pipeline
description: Run the renes NES reverse-engineering pipeline. Creates a knowledge base from a ROM, explores the game headless in Mesen to record code/data coverage, re-runs the CDL-guided analysis, rebuilds and byte-verifies the annotated .asm, and reports coverage and annotation progress. Use when the user wants to start on a new ROM, run or extend exploration, refresh analysis, rebuild the disassembly, or check status.
argument-hint: "[status|new <rom>|explore [minutes]|rebuild]"
---

# renes pipeline

All commands run from the project root (the checkout). No API keys are needed for these steps.

## Status (default)

```
python -m renes coverage work/<game>.db
python -m renes progress work/<game>.db
python -m renes build work/<game>.db
```

Report the code/data coverage, annotation progress, and whether the ROM is still byte-identical.

## New ROM: `new <rom>`

1. `python -m renes init "roms/<file>.nes"` creates `work/<stem>.db`. Only mapper 0 (NROM) is
   supported so far. If init reports another mapper, stop and tell the user.
2. Run the explore step below, starting with 10 minutes.
3. `python -m renes analyze work/<stem>.db`, then `python -m renes build work/<stem>.db`.
4. Report the results and suggest `/renes-annotate` as the next step.

## Explore: `explore [minutes]`

- `python -u -m renes explore work/<game>.db --minutes <N> --seed <S>`. Run it in the background
  for anything over 2 minutes, and wait for the completion notification instead of polling.
- Use a different `--seed` for each run. Coverage merges across runs.
- Afterwards run `analyze`, then `build`, so the new coverage reaches the disassembly.
- Expect coverage to plateau: random play gets stuck early in a game. More minutes give
  diminishing returns. To go further, use `/renes-explore` (Stage B: coached macro-action agent,
  optionally Jev-guided).

## TAS replay: `tas <movie>`

A tool-assisted speedrun replays the whole game in minutes: coverage for every level it visits,
and a savestate every 600 frames for experiments and the agent (`"start": {"state": <id>}`).

1. `python -m renes movies work/<game>.db` lists the game's tasvideos.org publications, marking
   which were made for **this exact ROM** (SHA-1) and which formats replay (`.fm2`, `.bk2`; two-player
   movies too). `python -m renes movies work/<game>.db --download <id>` saves one to
   `roms/tas/<id>M/` (git-ignored; don't put movies anywhere else). Prefer exact-ROM, full-game
   categories that visit every level (e.g. "warpless"). Note the publication's tags: runs with heavy
   glitch abuse reach code normal play never runs; say so when reporting. Credit the authors.
   A movie for another ROM revision will usually desync: ask the user before trying one.
2. `python -u -m renes tas work/<game>.db roms/tas/<id>/movie.fm2` (background; ~4 min per 67K frames).
   A checksum mismatch means the movie was made for another ROM revision.
3. **Check sync**: view the screenshots in `out/<stem>/tas/<id>/` in order. A desync shows as
   the player dying, a level restarting, or the title screen. Find the last good frame.
4. Fix or cap: `--until <frame>` keeps only the synced part. To get further, find a resync edit
   (insert or delete one frame near the break, usually at a level load where lag frames differ
   between emulators) and pass it with `--fix roms/tas/<id>/fix.json`
   (`[{"frame": 17950, "insert": 1}]`). Replays merge coverage, so rerunning is safe.
5. `work/<project>.tas_report.md` lists RAM bytes that change only a few times over the run, with their
   value timelines: level/world/lives/mode candidates. Hand them to `/renes-experiment`.
6. Then `analyze` and `build` as usual.

## Review: `review`

1. `python -u -m renes evidence work/<game>.db` records, for annotated routines, one real call
   traced line by line and a knock-out (normal vs routine patched to return) with screenshots.
   It plays the configured TAS movie (`--movie ... --until N`, remembered) or a scripted run.
2. `python -m renes dashboard` (alias `review`; serves every project in `work/`, or pass one KB) opens the local dashboard on
   127.0.0.1:8765 (on this machine also http://renes.localhost through Caddy, when the dashboard
   is running). The Review tab is where the user approves, renames, hints or rejects; decisions go
   straight into the KB and renames propagate to summaries and comments.

## Rebuild: `rebuild`

`python -m renes analyze work/<game>.db && python -m renes build work/<game>.db`.
Re-analysis keeps all candidate, verified and approved annotations. It replaces only
machine-derived structure.

## Outputs

- `out/<stem>/<stem>.asm` is the annotated disassembly. It's generated, so never edit it by hand.
- `work/<stem>.db` is the knowledge base, the source of truth.
- `work/` and `out/` contain copyrighted ROM bytes and are git-ignored. Don't share them.

## Tests

`python -m pytest -q` takes about 3 minutes and needs Mesen, cc65 and an example ROM described in `tests/example_rom.json` for the integration tests.
