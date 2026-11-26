# M0 Research: Emulator Control and Jev API

**Date**: 2026-10-17
**Feeds**: `.claude/prds/nes-re-harness.prd.md`, milestone M0

> **Later findings (2026-10-18)** that correct this pre-spike research:
> - Read callbacks with a value override don't see the CPU's operand fetches, so Game Genie
>   codes can't be applied that way; `renes codes try` patches PRG-ROM instead (NROM only).
> - The bridge now has a call trace (`trace`/`tracelog`) and routine-entry hit recording built
>   from exec callbacks, as suggested under "Gap" below.

## 1. Emulator: Mesen (MesenCE)

### Project status
- **SourMesen/Mesen2 was archived on 2026-06-04** and is now read-only. It is frozen at about
  v2.1.1 (mid-2025).
- Development continues as **MesenCE** (`nesdev-org/MesenCE`), GPL-3.0, and is active (17 open
  PRs). **Target MesenCE.** Everything below was read from the Mesen2 source, so the spike must
  confirm MesenCE behaves the same.

### Headless mode
- `Mesen --testRunner <script.lua> <rom.nes>` runs the script headless at maximum speed.
  `emu.stop(code)` ends the run and becomes the process exit code.
- Useful switches: `--noVideo`, `--noAudio`, `--noInput`, `--enableStdout`,
  `--doNotSaveSettings`.
- **`--timeout=<int>` (default 100).** The units are probably seconds; confirm. Long
  exploration runs need a large value, or a run is restarted from savestates in chunks.

### Lua API (from `Core/Debugger/LuaApi.cpp`)
| Need | API | Notes |
|---|---|---|
| Read/write RAM | `emu.read/write` (also 16/32-bit) | Adding `0x100` to the memory type skips side effects (safe peeks at PPU registers) |
| Breakpoints / access log | `emu.addMemoryCallback(fn, type, start, end)` | Read, write and exec. **A callback can return a value that overrides the read or write**, so "freeze address" experiments are built in |
| Per-address access stats | `emu.getAccessCounters` / `resetAccessCounters` | Read/write/exec counts per address, which is cheap input for variable discovery |
| Code/data split | `emu.getCdlData(memType)` | Works on ROM memory types; this is the source of code/data coverage |
| Frame loop | `emu.addEventCallback(fn, eventType)` | e.g. start of frame |
| Controller | `emu.setInput(table, port)` | |
| Savestates | `emu.createSavestate()` / `emu.loadSavestate(s)` | **Only allowed inside an exec memory callback for the main CPU.** Work around it by registering an exec callback on the NMI or RESET vector target, or on any hot address |
| CPU state | `emu.getState()` / `setState` | Includes PC, so a write callback can log which PC wrote |
| Screen | `getScreenBuffer`, `takeScreenshot` | For the human and the LLM, and for detecting the game screen |
| Patches | `emu.addCheat` | |
| Labels | `emu.getLabelAddress` | Could load our labels back into Mesen for debugging |

**Gap:** Lua has no trace-logger API. Instead, record the address → PC access log from
read/write callbacks via `getState().cpu.pc`, and build call traces from exec callbacks on
known routine entry points. Exec callbacks across the whole PRG range will be slow, so
**measuring throughput is part of the spike.**

### Talking to Python
- LuaSocket (`socket.core`) is compiled in but **off by default**. It loads only when both
  `ScriptAllowIoOsAccess` and `ScriptAllowNetworkAccess` are enabled
  (Script → Settings → Script Window → Restrictions). The command-line config prefixes don't
  include the debug settings, so they **must be set in Mesen's `settings.json`** before
  launching. Confirm the test runner honors them.
- Set `settimeout()` on every socket before using it, or Mesen can hang (from the docs).
- Proposed design: Python runs a TCP server, and Mesen's Lua script connects as a client. At
  the start of each frame (or when paused), Lua blocks on a command:
  `step n input`, `read range`, `write`, `save`, `load`, `cdl`, `counters`, `shot`, `quit`.
  Lua has no built-in JSON library, so either bundle a pure-Lua JSON file or use a simple
  line protocol. The emulator only advances when Python says so, which makes every run
  **deterministic and fully under Python's control.**

### Fallback: cynes (Python package)
- C/C++ core with Python bindings, cycle-accurate, with an `NESHeadless` class. RAM reads and
  writes work like indexing an array (CPU RAM and mapper RAM only). `save()`/`load()` exist but
  are slow. GPL-3.0.
- **It has no CDL, memory callbacks or trace support.** We would lose the code/data split, the
  most important signal. It might still suit Stage A fuzzing throughput, but it can't replace
  Mesen.
- tetanes-py (Rust TetaNES bindings) is headless and has a Gym environment. Savestates and RAM
  access are unconfirmed.

## 2. Jev API (TypeSafe AI)

### Interface (from docs.typesafe.ai/api)
- `POST https://api.typesafe.ai/v1/systemone`, with `Authorization: Bearer <key>`
- Body: `{ "state": <string|object|array>, "model": "jev-latest", "questions": { "<id>": Question } }`
- Question types:
  - `noul`: yes/no, returns a probability from 0 to 1. Optional `criteria.true/false`.
  - `choice`: `criteria` maps an option id to a description, **at most 255 options**.
    Returns `choice`, `probabilities` and `confidence`.
  - `score`: 2–10 ordered levels; returns a probability-weighted score.
- Response: `model` (e.g. `jev-1.13.0`), `answers`, and `usage` (input/output tokens).
- Limits: 32 MiB body, nesting depth 64, finite numbers only, gzip/zstd accepted. Errors 429
  and 529 mean retry with backoff. Third-party guides add: **context about 64k tokens total,
  about 32k for state plus one question** (a long-context header goes to 262k but is slower
  and less accurate).
- Third-party guides also say: **about 100 ms typical latency (70–500 ms)**, **$0.042 per
  million input tokens, output free**, Python SDK `typesafe-sdk` (Python 3.10+, async
  client). Verify the SDK name on PyPI.

### What this means for the design
- **Jev ranks a menu; it doesn't steer frame by frame.** At about 100 ms per call against
  16.7 ms per frame, each decision must be a macro-action covering many frames. Because Python
  controls the emulator clock, latency limits **throughput** (about 10 decisions per second),
  not correctness. The game never runs ahead while Jev thinks.
- Keep the state small and named, as the guides advise (accuracy drops with irrelevant
  content). Send labeled variables only, never raw 2 KB RAM dumps.
- Ask several independent questions in one call: `next_action` (choice), plus `is_stuck`
  (noul) and `is_new_screen_type` (noul) together, at no extra latency.
- **Second use:** Jev's `choice` and `noul` with probabilities are a cheap triage step for
  variable discovery. For example: "given this value trace and these events, which candidate
  meaning fits?" Only uncertain cases then go to Opus. This is worth a quick test in M5.
- Cost is negligible. Even a million decisions at about 500 tokens each costs about $21.

### Lessons from the Pokémon Red harness (`christianmat/jev-pokemon`)
- TypeScript on Node with a Game Boy core (serverboy). GPL-2.0+.
- **It read RAM using symbols from the existing pret/pokered disassembly.** In other words, it
  had the exact knowledge our system is trying to produce. This confirms the bootstrap problem
  in the PRD (§5.3): Jev can only be useful once some variables are labeled.
- The harness did the mechanics (A* pathfinding, menus). Jev only chose among high-level
  options with precomputed facts (type matchups, distance to the objective).
- Loop protection worth copying: tag options that were tried and had no effect, sample
  alternatives when a choice keeps failing, and reload a checkpoint as a last resort.
- **The "Claude Opus coach" from the earlier conversation does not appear in the repository.**
  Treat it as unverified. The coach in our PRD is our own design, not a proven technique.

## 3. M0 spike tasks (pass/fail)

| # | Spike | Pass when |
|---|---|---|
| S1 | Install MesenCE on Windows; a `--testRunner` hello script runs 600 frames and exits with code 42 | The exit code is 42; the timeout's units and maximum are known |
| S2 | Enable IO and network in `settings.json`; Lua connects to a Python TCP server | A `ping` round trip works from the test runner |
| S3 | Command set: `step`, `read`, `write`, `save`, `load` (via the exec-callback workaround), `cdl`, `counters`, `shot` | Python plays the example game for 1 minute of game time from scripted input, saves state, reloads it, and RAM matches |
| S4 | Throughput | Frames per second at 1 command per frame and at 1 per 30 frames; the cost of the write-callback access log on $0000–$07FF |
| S5 | Freeze experiment | A write-callback override pins one RAM address; the effect shows on screen |
| S6 | Jev hello world via `typesafe-sdk` | A `choice` call with a mock game state and 6 macro-actions returns; latency and tokens logged |
| S7 | Decision record | `docs/adr/0001-emulator.md`: MesenCE vs. cynes, plus the IPC protocol choice |

**Prerequisites from you:** a ROM dump you own (NROM, mapper 0) and a TypeSafe API key
(sign up through the TypeSafe developer console).

## Sources
- https://github.com/SourMesen/Mesen2 (archived notice; `Core/Debugger/LuaApi.cpp`,
  `Core/Debugger/ScriptingContext.cpp`, `UI/Utilities/CommandLineHelper.cs`)
- https://github.com/nesdev-org/MesenCE
- https://www.mesen.ca/docs/apireference.html and https://www.mesen.ca/docs/apireference/callbacks.html,
  https://www.mesen.ca/docs/apireference/misc.html
- https://pypi.org/project/cynes, https://pypi.org/project/tetanes-py/
- https://docs.typesafe.ai/api
- https://flaviocopes.com/jev/ (latency, pricing, SDK, context limits: third-party)
- https://github.com/christianmat/jev-pokemon
