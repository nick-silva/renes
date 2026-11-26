# Running several agents

renes is built so that several Claude Code sessions can work at once on one machine: one loop
agent per game, short-lived checking agents that judge the loop agents' names, and one person (or
one more session) who develops renes itself and answers the agents' tool problems. This guide
explains the roles, how to start them, and the practices that keep them from getting in each
other's way.

Each section says whether it describes something renes enforces or advice from running it.

## The roles

| Role | How many | What it runs | May write to the knowledge base |
|---|---|---|---|
| **Loop agent** | one per game | `/loop /renes-cycle work/<game>.db` in its own Claude Code session | yes, through `apply`, `experiment`, `evidence` |
| **Auditor** | spawned by a loop agent when the loop reaches the `audit` stage | the `renes-audit` skill on one packet | no: it writes `verdicts.json` next to the packet |
| **Grader** | spawned on request, only for a game with a reference disassembly | the `renes-grade` skill on one packet | no: it writes `verdicts.json` next to the packet |
| **Developer** | one | changes renes, reads `renes feedback list`, publishes patch notes | no game work |
| **Reviewer** | you | the dashboard: Research and Review tabs | yes: approve, rename, hint, reject |

The split exists because a name should never be checked by whoever wrote it, and a grader who can
see the reference must never be able to pass anything back to the annotator except counts.

## Starting a set of agents

1. Create the projects first, one per ROM, from one terminal:

   ```
   python -m renes init roms/example_a.nes
   python -m renes init roms/example_b.nes
   ```

   Run `explore`, `analyze` and `build` for each as in the [quick start](quickstart.md), or leave
   that to the loop.

2. Start the dashboard once. It shows every project in `work/` and who holds each one:

   ```
   python -m renes dashboard
   ```

3. Open one Claude Code session per game in the project root and start its loop:

   ```
   /loop /renes-cycle work/example_a.db
   ```

   Give each session a short agent name and tell it to keep that name, or set `RENES_AGENT` in the
   session's environment before starting Claude Code. The skill passes it to every
   `round start` and `round end`.

4. Review research facts on the dashboard when a loop stops for them. A loop does not annotate
   until its game brief has been reviewed.

## What renes enforces

**One loop per project.** `round start --agent <name>` claims the project. Another agent's
`round start` on the same project is refused (exit code 3) while the claim is fresh, which means
seen within the last 2 hours. `python -m renes claim <kb>` shows the holder, `claim <kb> release
--agent <name>` gives it up, and the loop releases its own claim when it stops for review or
finishes. `--force` takes a claim over; use it only when you know the other session has ended.
Reading a project (status, `next`, the dashboard) never needs a claim.

**The round-trip gate.** Every `apply` reassembles the ROM and rolls the whole batch back if a
single byte differs. An agent cannot damage a project by writing a bad proposal; the worst case is
a rejected batch.

**Protected names.** `verified` labels (proved by an experiment) and `approved` labels (decided by
you) are not overwritten by later proposals. A proposed rename of a verified label becomes a
suggestion for the reviewer.

**Per-project scratch files.** Proposals, experiment specs and feedback drafts are written to
`work/<project>.proposal.json`, `work/<project>.experiment.json` and
`work/<project>.feedback.txt`, so two agents never share a scratch file.

**A separate emulator per run.** Each emulator process gets a free local port from the operating
system, so loops on different games can explore, run experiments and collect evidence at the same
time.

**Aggregates only from checks.** `grade record` and `audit record` store counts and, for the
audit, the reasons for unsupported names. `benchmark` prints aggregate scores. No reference name is
ever printed to an annotating agent.

**Stall detection.** A loop stops when the same stage made no progress twice in a row, or no stage
made progress in three rounds. A review decision on the dashboard or
`python -m renes round resume <kb>` restarts it. This keeps an unattended agent from spending
hours on rounds that do nothing.

## Practices that keep the results honest

These are rules for the agents and for you. The skills state them, but nothing in the code can
stop a session from reading a file it shouldn't.

- **A loop agent never reads `refs/`, `out/<stem>/benchmark_details.md` or
  `out/<stem>/codes_details.md`.** If a benchmarked game's loop agent has seen the reference, its
  later names are no longer a blind result. Say so in your notes if it happens.
- **Spawn the auditor and the grader as fresh agents.** The loop agent wrote the names, so it
  must not judge them. In Claude Code the loop agent starts a subagent and tells it only: use the
  `renes-audit` (or `renes-grade`) skill on this packet path. Don't pass it your opinion of the
  names.
- **Keep the grader away from annotation.** A grader has seen reference names. It replies with
  counts only, and that session should not go on to annotate the same game.
- **Research in benchmark mode for benchmarked games.** `/renes-research` then avoids RAM maps,
  disassemblies and cheat lists, and uses only manuals, walkthroughs and gameplay pages.
- **Use the benchmark and the audit as calibration, not as targets.** If a score drops, the agent
  changes how it works (reads more callers, lowers its confidence, runs experiments). It does not
  chase individual names.

## Talking to the agents: feedback and patch notes

Loop agents run into bugs and limits in renes that only they see. The feedback channel lets them
report these and lets the developer answer without you relaying messages. It is implemented in
`renes/feedback.py` and stored in `work/feedback.sqlite3`, shared by all projects.

| Who | Command | Effect |
|---|---|---|
| agent | `feedback add --kind bug\|limit\|idea\|insight --project <kb> --agent <name> "<text>"` | files a note; `--file` reads the text from a file |
| developer | `feedback list` | open notes from all projects |
| developer | `feedback ack --id 4,9 "<plan>"` | tells those agents a fix is coming, so they don't build a workaround |
| developer | `feedback patch "<what changed>" --id 4,9` | publishes a patch note to every project and closes the notes it fixes |
| developer | `feedback close --id N "<reply>"` | answers one note |
| agent | (automatic) | each `round start` prints acks, patch notes and replies the project hasn't seen |
| either | `feedback wait` / `feedback wait --project <kb>` | blocks without using tokens until a new note (developer) or a reply (agent) arrives |

Practices:

- Ask agents for one note per problem, with the command, the input and the error. Game findings
  belong in the knowledge base, not in feedback.
- Acknowledge before you fix. An agent that knows a fix is coming does other work instead of
  inventing a workaround you then have to undo.
- Publish a patch note for every change that alters how a command behaves. Loops read patch notes
  at their next round start; without one, an agent keeps using the old way.
- When a loop stops with notes still open, have it run `feedback wait --project <kb>` as a
  background command. It exits when you reply, which wakes that session.

## Sharing work between games

`python -m renes library <kb>` matches each routine's code against routines that other projects in
`work/` have already annotated, and adds the other project's name and summary to the work unit as a
hint. It is refreshed at every round start. In practice games share little code unless they come
from the same studio's engine, so treat a hint as a lead to verify, not an answer.

## Changing renes while loops are running

Advice, not enforced:

- Work on a branch and run `python -m pytest -q` before the loops pick the change up. Loops import
  the code from the working tree, so a broken change breaks every running loop at its next command.
- If a bug affected stored results (for example an experiment check that was wrong for a day),
  say in a patch note exactly which rounds or time window were affected, and have each agent re-run
  what it relied on. `experiments --invalidate N --reason "..."` removes a mistaken experiment from
  the evidence, and `states --retire` hides states from broken runs.
- Don't change the knowledge base schema and leave old projects behind: `kb.py` migrates old
  databases when it opens them, and new columns should follow that pattern.

## Cost and load

Advice, not enforced:

- Start with one loop and watch several rounds on the dashboard's Overview tab before adding more.
  Each round records its duration and what moved, so a loop that is burning time shows up there.
- The expensive stages for the machine are `explore`, `tas` and `evidence`, which run the
  emulator flat out. Annotation uses almost no CPU. If the machine is busy, stagger the loops.
- The Jev exploration policy costs API credit. The loop never uses it unless you say so in that
  session; the free `novelty` policy is the default.
- A loop that is waiting for you costs nothing. A loop that keeps starting rounds while stalled
  does, which is why the stall rule stops it. If you lift a stall, fix its cause first.

## Checklist

1. One project per ROM, one loop agent per project, one agent name per session.
2. Dashboard running; research reviewed before annotation starts.
3. Auditor and grader are fresh agents that reply with counts.
4. Nobody who annotates a benchmarked game opens `refs/` or the benchmark details.
5. Feedback acknowledged, fixes announced as patch notes.
6. renes changes tested on a branch before running loops see them.
7. `work/`, `out/`, `roms/` and `refs/` stay on your machine.
