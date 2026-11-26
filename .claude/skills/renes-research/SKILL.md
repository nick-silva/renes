---
name: renes-research
description: Research the game being reverse engineered (manual, box/credits, walkthroughs, level maps, gameplay wikis, TAS notes) and record what matters as a cited game brief in the renes knowledge base, plus Game Genie codes when allowed. The brief feeds annotation work units and experiment ideas. Use when starting on a new game, when the loop says research, or when asked what is known about the game.
argument-hint: "[kb=work/<game>.db]"
---

# Research the game (renes M7)

The brief makes annotation and experiments better informed: the manual says which mechanics
exist and what things are called, walkthroughs say where things happen. Every fact is stored
with its source. Work units show the facts relevant to each routine.

## 1. Know the mode first

`python -m renes brief <kb> mode` prints `benchmark` or `open`.

- **benchmark**: the game is scored against a reference disassembly. Address-level sources
  would leak the answers.
- **open**: real use. Anything public may help.

| Source | benchmark | open |
|---|---|---|
| Instruction manual, box text, credits, publisher/developer/year | yes | yes |
| Walkthroughs, level maps, gameplay wikis (mechanics, items, enemies, secrets) | yes | yes |
| TAS publication notes (tricks used) | yes | yes |
| Disassemblies, source code leaks, RAM maps (e.g. Data Crystal), ROM maps | **never** | yes, cite them |
| Game Genie / Pro Action Replay / cheat lists | **only as a test set, added by the user** | yes, as research codes |
| Pages that mention RAM or ROM addresses for this game | **never** | yes |

In benchmark mode: don't open a page whose title or URL suggests addresses, RAM, ROM maps,
disassembly or cheats. If a gameplay page turns out to list addresses, stop reading it and
don't record anything from it. Also don't add facts from your own memory of this game's
internals. Recording what any player can observe is fine.

## 2. What to look for

Search the web (WebSearch, then WebFetch on the best pages). Aim for 15–40 short facts.

- **control**: what each button does in each situation (menus, swimming, climbing...).
- **mechanic**: rules with numbers, e.g. "100 coins give an extra life", "the timer starts at
  400", "a power-up turns small into big", scoring, lives, continues, difficulty loops.
- **vocabulary**: official names of characters, enemies, items, places, modes (from the manual
  first). These become better names: `Slime_Count` beats `Enemy_Type_6_Count`.
- **level**: the level/area structure, special areas (water, boss rooms, bonus rooms), how levels
  are entered (doors, ladders, shortcuts).
- **event**: notable things that happen: game over, ending, hidden content, pause behaviour,
  what happens after beating the game.
- Also note the publisher, developer, year and the cartridge mapper (the KB knows the mapper
  from the ROM header).

## 3. Record it

```
python -m renes brief <kb> add --kind mechanic --source "<url or 'Manual p.7'>" "100 coins give an extra life"
python -m renes brief <kb> list
```

One fact per command. Keep each to one sentence of player-observable behaviour.
In open mode, Game Genie codes go into a file of `CODE description` lines, then
`python -m renes codes <kb> add codes.txt --source <url>`. `python -m renes codes <kb> list`
then shows where each code lands in our annotation, which is a strong hint for the annotator
(verify with `python -m renes codes <kb> try <CODE>` before trusting it).

## 4. Turn it into questions

Finish with a short list of **testable hypotheses** taken from the brief, e.g. "manual: the
timer starts at 400 → which RAM holds it, does it tick once per N frames?", so the experiment
stage has targets. Report how many facts were recorded per kind and the hypotheses.

If nothing useful can be found (an obscure game), record that the search was done:
`python -m renes brief <kb> add --kind other --source "web search <date>" "No manual or walkthrough found"`.
