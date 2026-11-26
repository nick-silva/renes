---
name: renes-audit
description: Audit a renes annotation sample - judge whether each routine's or RAM label's code supports its name and summary. For an isolated auditor agent, never the annotator who wrote the names. Use when asked to audit out/<stem>/audit/packet.json.
argument-hint: "<path to packet.json>"
---

# Audit annotations (renes, reference-free)

You are the **auditor**. Someone else wrote these names; your job is to check them against the
code, skeptically. Don't edit the knowledge base or run annotation commands (`apply`, `next`).
Don't look anything up about the game's internals (disassemblies, RAM maps): judge only from the
packet. Reply with counts only.

## Input

`packet.json` has `items`; ignore fields starting with `_`.
- `type: "routine"`: `name`, `summary`, `code` (6502 listing with our labels and comments; `?` marks
  lines never executed), `callers`, `falls_in_from` (code that runs straight into this routine),
  `leads_to` (the code it falls through into and the code its branches and jumps outside it go to,
  up to 10 instructions each, with the routine that code belongs to: judge claims about what
  happens next from it), `callees` (their names and summaries), `ram` (addresses it touches with
  their labels and status),
  `evidence` (share of bytes that ran; what changed when the routine was knocked out). A list ending
  in "... N more" was shortened; `code` is always the whole routine.
- `type: "ram"`: `name`, `size`, `status`, `comment`, `used_by` (every routine that addresses any of
  its bytes) and `uses`: code snippets from the first of those routines, with their summaries. Each
  snippet's `listing` is a file next to the packet with that routine's whole code: open it when the
  snippet alone can't decide between supported and plausible.

## Verdicts

| verdict | when |
|---|---|
| `supported` | The code clearly does what the name and summary say: you could defend it from the listing (and callees/evidence) alone. |
| `plausible` | Consistent with the code but not shown by it: the name rests on meanings established elsewhere (RAM labels, callees), or is vague enough that it can't be wrong. |
| `unsupported` | The code doesn't do what is claimed, the summary describes something else, the name claims a role the code doesn't show (a "timer" that is never decremented), or comments contradict the code. |
| `unclear` | Not enough context to judge (e.g. a fragment whose effect depends entirely on its caller). |

Check concretely: compares, branches and stores against what the summary says; a "clear" routine
should store zeros; a "timer" should count; "draws" should write OAM/PPU or a buffer. Judge the
whole claim: a right name with a wrong summary is `unsupported`. For RAM, the uses must agree
with the name (a "Lives" byte decremented on death, compared to 0 for game over).

## Output

Write `verdicts.json` next to the packet: a JSON list with one object per item,
`{"id": "a1", "verdict": "supported", "why": "<under 15 words>"}`. Judge every item, check the file
parses with one entry per item, then reply only with the count per verdict.
