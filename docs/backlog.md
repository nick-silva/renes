# Backlog

Open work, roughly in priority order within each group. Done items move to the PRD's milestone
table or the commit history.

## Annotation quality (from the benchmark comparison against the reference)

Done on `quality-fixes`: tile sheets and value/jump-table probes with screenshots (1), unlabeled-RAM
guardrails (2), the competing-meaning rule for experiments (3), jump-table probing in the loop (4).

Done on `annotator-evidence`:
- **5, automatic rework** (`rework.py`): a placeholder name goes back to the queue once what it
  depends on is known: a raw address that is now labeled, the code a `...Continue` runs into, or
  a number whose probe picture now exists. 8 of the benchmark game's routines went back.
- **8, evidence and confidence** (`confidence.py`, `renes confidence`): an evidence score per routine
  and RAM label, next to the claim. Measured on the benchmark game, the item's premise (the claim doesn't predict
  accuracy) was wrong: the claim does rank names (r = 0.27), evidence alone doesn't (r = 0.11), and
  discounting incoherent names helps (r = 0.33). Review now ranks by that discounted claim and flags
  overconfident claims. Re-check with item 6: token similarity is a noisy yardstick.
- **10, slot picking** (`visual.find_slot`): indexed tables are probed in an archive state and
  object slot where they run (one table's entry is shown in the state and slot where an object of that type is alive); tables that never
  run are marked skipped (`visual skip`). Fixed a bridge bug: unfinished traces kept recording.

Done on `benchmark-grading`:
- **7, pairing fixes**: equates below $800 count as RAM only when the reference code addresses
  memory with them (93 constants no longer paired); names on a lone shared RTS aren't paired (38).
- **6, semantic grading** (`renes grade`, `renes-grade` skill): an isolated grader agent judges a
  stratified sample of 150 pairs. The benchmark game: same meaning 69%, right behaviour 99%, 2 wrong, against a
  15% token match; RAM 88% same. A second independent grader agrees on 97% (kappa 0.93). Claimed
  confidence predicts meaning (39% same below 0.5, 94% at 0.5-0.7); evidence alone still doesn't.
- **9, metatile pictures** (`visual metatiles`): block tables found from what the game draws,
  completed with the renderer's own pointer table. The benchmark game has four such tables;
  Game D has none (other units: its pictures are the tile sheets).

## Visualization (dashboard)

Done on `quality-fixes`: the Map tab with the ROM map (1), RAM map (2) and subsystem graph (3);
each loop round now stores a map snapshot (`map_snapshots`) for item 9.
Done on `benchmark-grading`: the Questions tab (7), an open-questions board.

4. **Gameplay timeline.** Scrub the TAS screenshots; show which routines first ran at each moment
   (the evidence step already records first-hit frames).
5. **Discovery feed.** Each loop round as a story: what became understood, with the experiment
   screenshots and probe images that prove it.
6. **Confidence vs evidence chart.** Every routine plotted by self-reported confidence against
   actual evidence; highlights speculative areas and overconfidence.
8. **Cross-project comparison.** Progress curves and ROM maps of several games side by side.
9. **Animated maps.** Replay the ROM/RAM maps from the per-round snapshots (needs the snapshots,
   which start being recorded with the maps).

## Several agents (done on `multi-agent`)

- **Project claims**: `round start --agent`, `renes claim`; agent recorded on rounds and activity;
  per-project scratch files.
- **Same code in other games** (`renes library`): cross-project hints in work units. Measured on
  four early NROM games: very little shared code (one game gets 3 hints from another). Worth more between games built on one studio's engine.
- **Reference-free audit** (`renes audit`, `renes-audit` skill) and quality numbers on project cards.
- **Dashboard at logon**: `scripts/install_dashboard_task.ps1` registers the RenesDashboard task.

## Finishing (done on `finishing`)

- Fixes from agents' runs: missing knowledge-base paths are refused; `renes movies` finds and
  downloads TASVideos movies for the exact ROM; two-player movies replay; per-project reports.
- Audit findings go back to the annotator (`audit record`; `audit prepare --repeat` re-checks).
- `renes finish` checklist, end-of-project loop stages (audit, done) and `renes export`.
- Review tab code links into the full listing (visualization 10).
- From agents' reports: `renes states` (archived states filtered by RAM values), `"shared"`
  interventions in experiments (both runs), TASVideos zips replay directly, fuller audit packets
  (`used_by`, `falls_in_from`, multi-byte labels), and `renes feedback` (agent notes to the developer).
- Third round (#27-#31): whole routines in audit packets and the plausible share in results; stale
  "never ran" claims say where they are; an empty line comment removes one; apply warns when it cuts
  text; logged entries nothing calls stay inside the routine that runs into them; `states --retire`;
  writer PCs at the storing instruction; renames of verified labels become proposals for review;
  `coverage --unclassified`; `explore --state/--setup`.
- Second round of agent feedback (#13-#22): sent-back names reopen the audit stage; moved evidence
  movies are found again (a missing one stops the stage); `states --writes`; `poke_at_pc`; `"reset"`
  input steps; `increments_by` `changed_only`; stale "never ran" summaries listed at round start;
  `feedback --file`.

## Agent feedback (#35-#38, on `feedback-35-38`)

- The role-word warning in `apply` skips a routine name's leading verb (`StepRamp...`) and counts
  addresses inside a sized label as labeled (`Timers+11` backs "Timer").
- Jump-table detection follows a helper that loads the index and falls into the engine: the
  table is indexed by the variable the helper loads, not by an earlier early-out test; re-probed.
  A re-probe replaces a table's earlier pictures.
- A treatment that also pokes or freezes other addresses than its target no longer verifies (setup
  goes in `"shared"`, or `"other_addresses"` says why); `experiments --invalidate N --reason` takes
  a mistaken experiment out of the evidence. Game D #24/#25 are invalid. 34 older passing
  experiments touch other addresses (Game B 16, Game C 15, Game D 3): mostly pairs
  (speed and fraction, score digits), left for their agents to check.

## Mapper 7 (AxROM, on `mapper7`)

- Banks are analysed, annotated and rendered one at a time (an AxROM bank is a whole program at
  $8000). Work units, `next`, `apply`, `progress`, the loop status, confidence, rework, audit,
  evidence and the dashboard handle every bank; routines are `B1:$9100` where banks share addresses.
  Review ids are `r<bank>:<entry>` outside bank 0; the ROM map lays the banks end to end.
- `banking.far_calls` follows each JSR/JMP through the bank switch symbolically (immediates before
  the call, RAM that every archived state agrees on) to the routine it lands on; `analyze` makes
  those targets routines and work units list them. Game E: 42 far-call sites.
- Evidence watches, traces and knocks out routines by PRG offset (`hits`/`trace` take a memtype).
- Still bank 0 only: the cross-game library table (no bank column), jump-table probe sites (CPU
  address), `experiment --writes` PC names, Game Genie codes, metatiles, benchmark and grading.

## Input bug (2026-10-21 09:23 to 2026-10-22 14:08)

The two-controller change addressed controller 2 as controller 1, so no button reached the game for
29 hours: setups stayed on the title screen, probes and TAS replays saw nothing. Fixed, with a
Mesen test that presses buttons on both controllers. Affected work, announced to the agents:
Game B 102 experiments (49 passed, verifying 41 labels), 17 knock-outs and an agent run;
Game C 4 experiments, 3 knock-outs and three TAS replays.

Cleaned up 2026-10-23. Every archived state experiments started from came from runs before the bug,
so only experiments that pressed buttons were affected. Game B: of the 65 passing experiments
in the window, 34 had already been re-run by the agents; the other 17 that pressed buttons were
re-run (#251-#267) and all passed again, so no label lost its status. Game C's passing experiments
pressed no buttons. Knock-outs and traces re-collected for 16 of the 17 Game B routines and
all 3 Game C ones; one sound routine never runs in the TAS movie, so its only
knock-out is still from scripted play during the bug. Game C's runs 7-10 are retired (67 states)
and the three TAS movies replayed in sync (runs 15-17, 39 states, no new code).

## Later ideas from agents

- **Goal-directed state search**: explore until a RAM condition holds (a rare event, a rare mode)
  and archive states there. Game B's bonus round turned out to be reachable: a TAS movie
  replays on the exact ROM now that input works (44 bonus-round states, coverage 60% -> 67%).

## Per project

- **Game B:** 155/155 routines, audit 87%; input-bug work redone and a TAS movie replayed (bonus
  round). One sound routine's knock-out needs a playback where its effect runs.
- **Game C:** 134/134 routines; re-check the names its audit sent back (`audit prepare --repeat`).
- **Game D:** research facts reviewed (29 accepted); an exact-ROM two-player TAS movie is available
  (`renes movies --download <id>`). Next: `/loop /renes-cycle` (blind second benchmark game, M8),
  graded with `renes grade`.
- **The benchmark game:** review queue (~600 low-confidence names; one swapped pair of RAM names; two
  overlapping code/data tables); more coverage only if wanted (fix the TAS desync, or Jev with
  credit). New names for it are no longer blind (the reference was read).

## Later

- **M9: bank-switching mappers** (MMC1, MMC3): bank-aware knowledge base, call graph and renderer.
- Shared-tail fragments are annotated before their callers (annotate tails top-down).
- Code another routine branches into (a bonus step entered from another routine's branch)
  stays inside its neighbour. Making every such target an entry was tried: +42 to +164 routines per
  game, mostly short branches between neighbours, so it needs a narrower rule.
- Done from agent feedback (#4-#11): `apply` updates sizes/comments of verified labels; code past
  data gaps and code the CDL saw only as data belongs to routines (Game C 533 orphaned instructions
  -> 0); `probe --changes`, `changes_at_most`, `from_frame`, `vram_*` checks, `writers`; audits stay
  current until 10 routines (or 5%) change; developer acks and patch notes for the agents.
- The analyzer turns a lone RTS table entry into a routine.
- Game Genie codes only work on NROM (PRG patching).
