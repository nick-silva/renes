---
name: renes-annotate
description: Annotate routines of an NES game in a renes knowledge base. Reads work units from `python -m renes next`, writes evidence-based names, summaries and comments as JSON proposals, and applies them with `python -m renes apply` (rolled back unless the ROM still reassembles byte-identically). Use when asked to annotate, name, comment, document or reverse engineer routines, RAM variables or tables in the renes project.
argument-hint: "[routines=10] [kb=work/<game>.db]"
---

# Annotate routines (renes M4)

You are the annotator in the renes pipeline. Python prepares the evidence and guards the output;
you supply the understanding. Work only through the CLI: never edit `.asm` files or the
SQLite database directly.

## Arguments
- First argument: how many routines to annotate this session (default 10).
- Second argument: the knowledge base (default: the only `.db` file in `work/`; ask if there are several).

## Loop

1. `python -m renes progress <kb>`. Note the counts so you can report the change at the end.
2. `python -m renes next <kb> --count 5` prints the next 5 work units, bottom-up along the call graph.
   Callees and fall-through targets are annotated before their callers, so their summaries are
   included in the work unit.
3. For each unit, work out what the routine does from the code, its callers and callees, the RAM it
   touches (with game-wide read/write counts) and the data it references.
4. Write all the proposals for the batch as a JSON list to `work/<project>.proposal.json` (e.g.
   `work/example_game.proposal.json`: one file per project, since other agents may be annotating
   other games at the same time), overwriting the
   previous batch. Use the format below. One fixed file avoids per-file write prompts; the
   knowledge base keeps everything that was applied.
5. `python -m renes apply <kb> work/<project>.proposal.json`.
   - `+` lines were accepted. `!` lines were rejected, with a reason (for example the name is
     already used, or the address is not an instruction start). Fix them in the file and apply
     again: applying is idempotent.
   - `ROLLED BACK` means nothing was kept. Read the reason, fix it and retry. If the same
     rollback happens twice, stop and report it.
6. Repeat from step 2 until you've done the requested number of routines.
7. Finish with `python -m renes build <kb>` and `python -m renes progress <kb>`. Report what
   changed and highlight the most interesting findings or open questions for the human.

## Proposal format

```json
[
  {
    "routine": "$C100",
    "name": "InitializeNameTables",
    "summary": "Clears both nametables and attribute tables to the blank tile.",
    "notes": ["optional block comment shown above the routine"],
    "lines": {"$C103": "wait for vblank before touching the PPU"},
    "labels": {"$C109": "ClearNTLoop"},
    "ram": {"$0010": {"name": "Mirror_PPU_CTRL", "comment": "copy of the last value written to PPUCTRL"}},
    "data": {"$C141": "NameTableInitData"},
    "confidence": 0.7
  }
]
```

- `routine` and `summary` are required. Everything else is optional; include only what the evidence supports.
- To fix only RAM labels (a name, size or comment), send an entry with just `"ram"` (and optionally
  `confidence`), no `routine`: `{"ram": {"$0125": {"name": "Cursor_Blink_Frame", "comment": "..."}}}`.
  No routine counts as re-annotated, so its evidence and the audit stay current.
- Addresses are hex strings (`"$C103"`). `lines` and `labels` must be instruction starts inside the routine.
  A `lines` entry with an empty comment (`"$C103": ""`) removes the comment already there.
- **Banked games (AxROM):** every bank sits at $8000, so the same address exists in each bank. Give
  `routine` as the work unit shows it (`"B1:$9100"`, or the routine's current name); `lines`,
  `labels` and `data` stay plain (`"$9104"`): they are in the routine's bank. "Calls in other banks"
  in a work unit are calls through the bank-switch stub, resolved to the routine they reach.
- Length limits: `summary` 400 characters; each note, line comment and RAM comment 140. Longer
  text is cut, and apply warns with the part it dropped.
- `ram` covers $0000-$07FF and $6000-$7FFF. `data` covers ROM data the routine references.
- For arrays and multi-byte variables, give the RAM entry a `"size"` in bytes (1-256), e.g.
  `{"name": "Sprite_Buffer", "size": 256}`. References inside the region then render as
  `Sprite_Buffer+12,y` instead of a raw address. Only set a size when indexing or offsets
  in the code show the extent.

## Using the work unit
- **Alternate entries that fall into this routine**: fragments that run into the code shown.
  Name them in the same batch, by what they set before continuing.
- **Code at jumps/branches leaving this routine**: what runs after a tail jump or an exit
  branch. Use it instead of guessing what `jmp LF38D` leads to.

- **Reviewer hints / Sent back by the reviewer**: the human's word. Follow it, keep what was
  right, and re-propose the whole routine.
- **Why this name was sent back (automatic)**: the name was a placeholder (`Store0InByte39`,
  `...ThenContinue`) and what it depends on now has a name, a summary or a probe picture. Rename
  using it (`ClearPowerupKind`), keep the rest, re-propose the whole routine. `apply` lists such
  send-backs as `~` lines; they come first in `next`.
- **Same code in other games**: our own earlier annotation of identical or similar code in another
  project (never a reference). Check it fits this game's callers and data before reusing the name;
  where RAM is paired (`$0031 ~ Joypad_Buttons`), propose the matching label for this game's address.
- **RAM it touches** shows each label's `evidence` (0-0.9): how much the tools have checked it.
  A label at 0.2-0.4 is untested: don't build a routine's meaning on it alone.
- **Knock-out**: what changed when this routine was patched to return immediately (screen and
  labeled RAM). Strong evidence of what it is responsible for; "screen unchanged" suggests
  sound, timing, or an effect outside the window.
- **Game brief**: facts from the manual/walkthroughs. Prefer the game's official vocabulary in
  names when the code clearly matches the fact.

## Rules

- **Never open `refs/`, `out/<stem>/benchmark_details.md`, `out/<stem>/codes_details.md` (or test codes' descriptions)
  or anything in `out/<stem>/grading/`** (the reference disassembly, the benchmark's per-name pairs and the
  grader's packet). Only `python -m renes benchmark` and `python -m renes grade <kb> show` aggregates may be used.
- **Look at the pictures.** When a work unit lists image files (jump-table states, values compared
  here, sprite tiles), open them with the Read tool before naming. A probe screenshot of "the screen
  with Player_State held at $0B" tells you the state is dying; a tile image tells you a routine draws
  a coin or a hammer. Name the game thing you see (use the brief's vocabulary), not just the mechanics.
- **Don't interpret unlabeled RAM.** The work unit lists unlabeled RAM the routine writes. Don't give it a
  role in names or summaries ("timer", "speed", "state"): say what is stored ("sets $049A,x to 9"),
  or propose a `ram` label with confidence 0.3-0.4 so the experiment stage tests it. `apply` prints a
  `?` warning when a name claims a role only unlabeled RAM could back: fix it before the next batch.
- **Evidence first.** Base names on what the code demonstrably does. **Do not recall names from
  published disassemblies of this game**: the output is benchmarked against them, and copying
  from memory makes the benchmark meaningless. When unsure, pick a plain, descriptive name and
  lower the confidence.
- **Confidence:** 0.3 is a guess, 0.6 plausible, 0.85 strongly supported by code and evidence.
  Never above 0.9: only experiments (M5) and humans verify. On the benchmark game your confidence did rank names
  (low ones were rarely right), so keep it honest. Review discounts it for names with placeholder
  parts or unbacked roles, and `python -m renes confidence <kb>` lists claims well above their evidence.
- **Naming conventions** (ca65-safe, letters/digits/underscore, at most 40 chars):
  - Routines: `CamelCase` verb phrases, e.g. `UpdatePlayerPosition`, `ReadJoypads`.
  - RAM variables: `Capitalized_Snake_Case` nouns, e.g. `Player_X_Position`, `Game_Timer`.
    Use a suffix for arrays indexed by object slot (`Enemy_State`, indexed by X).
  - Data tables: `CamelCase` nouns ending in `Data`, `Table` or `Ptrs`.
  - Local labels: short `CamelCase` describing the branch point (`SkipScroll`, `NextSprite`), or omit them.
- **Comments** explain intent, not mnemonics. Write "skip if timer expired", not "branch if zero".
  Comment the lines a reader would stop at, not every line. Keep them under 100 characters.
- **Summaries** are one or two sentences. They're what callers see, so state the effect and its
  inputs and outputs (registers, RAM).
- **Tiny entries** (1-3 instructions that fall through into another routine, or a `.byte $2C` skip)
  are alternate entry points. Name them by what they set before continuing, e.g. `ClearYThenSkip`.
- **Lines marked `?`** were never executed. Treat them as plausible code, but say so if the
  decoding looks like data (nonsense sequences, undocumented-looking patterns).
- **Don't rename** labels marked `[verified]` or `[approved]` lightly: apply keeps the old name and
  records yours as a rename proposal for the human (dashboard Questions tab). Give the reason in
  `"why"` (next to `name`, in the routine proposal or the RAM entry; up to 400 characters), or the
  summary or RAM comment is used. Leave out `comment` unless you mean to change it: it still applies.
  Propose one only when the evidence contradicts the verified name.

## NES background to apply

- PPU: `PPUCTRL` (NMI enable, increment, pattern tables), `PPUMASK`, `PPUSTATUS` (read: vblank
  flag; clears the address latch), `PPUSCROLL`/`PPUADDR` (two writes each), `PPUDATA`, `OAMDMA`
  (copies a $xx00 RAM page to sprite memory; usually page $02).
- Input: write 1 then 0 to `JOY1` to strobe, then 8 reads, one bit per button
  (A, B, Select, Start, Up, Down, Left, Right).
- Common idioms: vblank wait loops on `PPUSTATUS`; RAM clearing loops at reset; a jump engine
  (pulls its return address, dispatches through a table placed right after the `JSR`); BCD digit
  arithmetic for scores and timers; LFSR random-number generators; object loops indexed by X;
  `BIT abs` (`.byte $2C`) to skip a 2-byte instruction for alternate entry points.
