---
name: renes-cycle
description: Run one round of the renes reverse-engineering work loop. Asks `python -m renes status` which stage is due (annotate, experiment, benchmark, explore or review), does that stage, and reports. Designed to be repeated with `/loop /renes-cycle`. Use when asked to keep the annotation going, run the loop, or continue the reverse engineering.
argument-hint: "[kb=work/<game>.db] [annotate-count=15]"
---

# One round of the renes loop

1. `python -m renes round start <kb> --agent <name>`. Pick one short agent name for this session
   (e.g. `game-a-loop`) and use it every round. Other agents may be running loops on other games: if
   the project is claimed by another agent, round start refuses (exit 3). Then stop and tell the
   user; never pass `--force` unless the user says that agent has stopped. It prints the status and
   records the round with the project metrics. The last status line says `NEXT: <stage> (<reason>)`. Do exactly that
   stage. Don't pick a different one because it seems more interesting: the rules keep the loop
   balanced over many rounds.
2. Run the stage:

| Stage | What to do |
|---|---|
| `research` | Follow `/renes-research` once for this game (respect the benchmark/open source rules). |
| `annotate` | Follow `/renes-annotate` for **15 routines** (3 batches of 5). |
| `experiment` | Follow `/renes-experiment` for **3–5 targets**, taken from the "untested RAM candidates" list in the status (most accessed first). Skip scratch bytes and anything you can't make a falsifiable prediction for. |
| `benchmark` | `python -m renes benchmark <kb>` (the reference is remembered). Compare with the previous run and write down which aggregate moved. |
| `probe` | Once per project: `python -m renes visual <kb> tiles` and `python -m renes visual <kb> metatiles` (16x16 block pictures; some games have none), then for each jump table listed in the status `python -m renes visual <kb> probe <$site>`. For tables indexed by an object slot (`,x`) it picks a slot that runs the table in the start state; if none does, it marks the table skipped. Open a few of the images it writes. If every value says "no visible change", the table doesn't run in the default start state: retry with `--state <id>` from the explorer archive (a state in the game mode where it runs); if none helps, `python -m renes visual <kb> skip <$site> --reason "..."` so the loop moves on. |
| `evidence` | `python -u -m renes evidence <kb>` in the background (about 10 s per routine): traces and knock-out screenshots for the review page. Annotate while it runs. |
| `explore` | Follow `/renes-explore` for **one round** with the free `novelty` policy, then `analyze` and `build`. Use Jev only if the user has said so in this session. |
| `audit` | Finishing: `python -m renes audit <kb> prepare` (add `--repeat` if an audit was recorded before, to re-check the same items). Then **spawn a separate agent** (Agent tool) to judge the packet with the `renes-audit` skill; never judge it yourself (you wrote the names). Then `python -m renes audit <kb> record`: unsupported names come back to the annotate stage with the auditor's reasons. |
| `done` | Every finishing check passes (`python -m renes finish <kb>`). `python -m renes export <kb>`, release the claim (`python -m renes claim <kb> release --agent <name>`), stop the loop and tell the user the project is finished, with the export path and the audit numbers. |
| `review` | Stop the loop. Release the claim (`python -m renes claim <kb> release --agent <name>`), then tell the user to open the dashboard (`python -m renes dashboard`, then pick the project): the Research tab when research awaits review, otherwise the Review tab (screenshots, line-by-line traces, approve/rename/hint). List the open questions. |

3. Finish every round with `python -m renes build <kb>` (must say byte-identical), then
   `python -m renes round end <kb> --agent <name> --note "<one line: what this round did>"`. It prints the round's
   duration and progress (what went up), which the dashboard shows too. Then give a short
   report: the stage, what changed (routines/labels/experiments, or the benchmark deltas), and
   anything surprising. Keep it to a few lines: in `/loop` the user reads many of these.
4. **Tell the renes developer what got in your way.** Whenever renes itself held you back this
   round (a bug, a command that rejected valid input, a missing option, a workaround you had to
   use, a check you couldn't express) or you saw how the tools could work better, record one note
   per issue, then carry on with what you can do:
   `python -m renes feedback add --kind bug|limit|idea|insight --project <kb> --agent <name> "<what happened, what you needed, what you did instead>"`.
   Be concrete (the command, the input, the error). Game findings go in the knowledge base, not
   here. Put the note in single quotes: in double quotes the shell eats `$0125` and backticks. For
   anything longer or with quotes and dashes, write it to `work/<project>.feedback.txt` and pass
   `--file work/<project>.feedback.txt` instead of the text (PowerShell mangles such arguments).

   `round start` also lists summaries that say code "never ran" (or is unused) although coverage
   now shows it ran: fix those in the next annotate round, before an audit counts them against you.

   **Read what the developer sends back.** `round start` prints, before the status:
   - `renes developer is working on your notes #N: ...` (an ack): the fix is coming. Don't build a
     workaround for it; do other work and leave items that need it for later.
   - `renes patch note N (fixes #...): ...`: what changed in renes, for every project. Use new
     commands and checks from then on, and re-try anything you had left because of a fixed note.
   - `reply to your feedback #N`: the answer to one of your notes.
   `python -m renes feedback news` lists recent patch notes again; `feedback list --project <kb>`
   shows your open notes and their status (`open`, `working`).
5. **Waiting for the developer.** If the loop stops (or you can't make progress) while notes of
   yours are open, don't just end: start `python -m renes feedback wait --project <kb>` as a
   **background** command (Bash `run_in_background`). It uses no tokens and exits when the
   developer replies (or after 12 hours). When it exits with replies, tell the user in one line,
   then start the loop again (`/loop /renes-cycle <kb>`, or the next round if you are still in
   one): `round start` prints the replies, and a review stop lifts once you `python -m renes round
   resume <kb>` for a reply that unblocks the stalled stage. Only one wait per project.

## Rules that keep the loop honest

- **Never open the reference disassembly or the benchmark details.** Never read `refs/` or
  `out/<stem>/benchmark_details.md` or `out/<stem>/codes_details.md` (or test codes' descriptions), never grep them, and never ask for names from them.
  The benchmark command shows only aggregates, and that's all you may use. If a score drops,
  improve how you work (read more callers, lower your confidence, run experiments), not
  individual names.
- **Use the benchmark as calibration, not as a target.** If high-confidence names score no
  better than low-confidence ones, be more conservative with confidence. If verified names
  score much better than candidates, experiments are worth more rounds.
- **Stop the loop** when `round start` says `review`. Then don't schedule another round: report and
  wait for the user. Besides "nothing left to do" and "research awaits review", `review` is how the
  stall rule stops the loop: the same stage made no progress in 2 rounds in a row, or no stage made
  progress in 3. A review decision on the dashboard or `python -m renes round resume <kb>` lifts it.
- Also stop if the same stage *fails* twice in a row (a rollback that keeps coming back). Close the round
  with `round end` either way, so the failure shows up as a round without progress.
- Routines the reviewer sent back (`rework`) come first in `next`: follow their hints exactly.
- Jev credit is limited: the loop never spends it unless the user said so in this session.
