# ADR 0001: Emulator and control protocol

**Status**: Accepted (2026-10-17)
**Context**: M0 spikes, `docs/research/m0-spikes.md`

## Decision

1. **Emulator: MesenCE 2.2.1** (`nesdev-org/MesenCE`, Windows build), installed portably in
   `tools/mesen/` (a `settings.json` next to `Mesen.exe` enables portable mode).
2. **Control: a line-based TCP protocol.** Python is the server (`renes/emu/mesen.py`).
   Lua running inside `Mesen --testRunner` is the client (`renes/emu/bridge.lua`). The emulator
   advances only on `step`, so Python owns the clock and runs are deterministic.
3. **cynes is not used.** Mesen's speed is sufficient, and only Mesen provides CDL, memory
   callbacks with value override, and access counters.

## Required Mesen settings (`tools/mesen/settings.json`)

```json
{ "Debug": { "ScriptWindow": {
    "AllowIoOsAccess": true, "AllowNetworkAccess": true, "ScriptTimeout": 3600 } } }
```

- Without the first two, `socket.core` doesn't load.
- `ScriptTimeout` (default **1 s**) is a watchdog on each Lua callback, *including time spent
  blocked waiting for Python*. Any slow orchestrator step (LLM calls) would otherwise kill
  the script.
- Pass `--timeout=<seconds>` to the test runner (default 100 s) for long runs.

## Measured (a 32 KB NROM game, the development machine)

| Mode | Speed |
|---|---|
| Bulk `step 600` | 307–380 fps (5–6× real time) |
| 1 command per frame | 270–300 fps |
| 1 command per frame plus a 2 KB RAM read | ~247 fps |
| Write log with writer PC, 1 address | ~260 fps |
| Write log with writer PC, $0700–$07FF | ~115 fps |
| Write log with writer PC, all 2 KB RAM | unusable (< 20 fps) |
| Savestate | 5,368 bytes, exact RAM restore, deterministic replay |
| Jev call | 256 ms (first call), 612 input tokens |

## Consequences and known quirks

- **Writer PCs are expensive.** Lua has no cheap PC accessor; `emu.getState()` serializes the
  whole console. Use access counters for broad sweeps, and write logging only on targeted
  candidate addresses (that's what the pipeline needs anyway). Possible later optimization:
  find the writer as the PRG byte whose `lastExecClock` falls just before the address's
  `lastWriteClock`, using `getAccessCounters`.
- `emu.log` output isn't visible in test-runner mode, so all diagnostics go over the socket.
- Mesen arrays (`getCdlData`, `getAccessCounters`) are 0-indexed.
- Savestate commands outside an exec callback are deferred by one instruction, using a
  temporary exec callback over the whole address range.
- `quit` occasionally doesn't terminate the process; the Python side kills it after 30 s.
- A game's timer can stop during the player's death animation, so experiments must start from known
  savestates, not from wherever exploration left off.
