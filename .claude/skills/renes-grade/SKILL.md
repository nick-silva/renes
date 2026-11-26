---
name: renes-grade
description: Grade a renes benchmark packet - judge whether each of our names means the same as the reference disassembly's name at that address. Only for an isolated grader agent, never for the annotator. Use when asked to grade out/<stem>/grading/packet.json.
argument-hint: "<path to packet.json>"
---

# Grade name pairs (renes benchmark, item 6)

You are the **grader**, not the annotator. The packet contains reference names; that is fine for
you, but nothing you read may flow back into annotation. Don't edit the knowledge base, don't run
`renes apply`, `next` or any annotation command, and don't mention specific reference names in
your final reply: report only counts.

## Input

`packet.json` has `pairs`. Each pair:
- `ours`: our label name; `our_description`: our routine summary or comment (may be empty).
- `reference`: the reference disassembly's name(s) at the same address; `reference_source`: the
  lines where they are defined, with nearby comments.
- `kind`: routine, ram, data (ROM tables) or code (local labels inside routines).
- Ignore every field starting with `_`.

## Verdicts

For each pair decide what **our name (with its description)** says compared to the reference:

| verdict | when |
|---|---|
| `same` | Same thing or same meaning, in any words. `Item_Kind` = `ItemType`; `ReadJoypads` = `PollControllers` if both read the controller. Abbreviation, word order and detail level don't matter. |
| `entity` | Right behaviour or mechanics, but misses or misnames the game thing the reference names, or is clearly more generic: `SpawnObjectType2F` vs a specific enemy name; `SetObjectTimer` vs `JellyfishSwimTimer`. |
| `wrong` | A different meaning: a different variable, purpose or thing. |
| `unclear` | The reference name and its context don't say enough to judge (e.g. `L8A3F`, `Unused`, a bare `Exit`). |

Judge meaning, not vocabulary. Use `our_description` when the name alone is ambiguous: a vague
name with a correct description is `entity`, not `wrong`. For `code` labels (branch targets) be
lenient about granularity: what matters is whether both describe the same branch purpose.

## Output

Write `verdicts.json` in the same folder as the packet: a JSON list with one object per pair,
`{"id": "g1", "verdict": "same", "why": "both are the power-up type"}` (`why` is under 15 words).
Grade every pair. Then reply only with the counts per verdict.
