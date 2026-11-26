"""renes command line.

  python -m renes init roms/game.nes      create work/<stem>.db from a ROM
  python -m renes build work/<stem>.db    render, assemble, byte-compare (exit 1 on mismatch)
  python -m renes explore work/<stem>.db  fuzz in Mesen, record CDL coverage + access counters
  python -m renes coverage work/<stem>.db summarize recorded coverage
  python -m renes analyze work/<stem>.db  re-run code/data analysis with the recorded coverage
  python -m renes next work/<stem>.db     print the next annotation work unit(s)
  python -m renes apply work/<stem>.db p.json   apply a proposal; rolled back unless ROM verifies
  python -m renes progress work/<stem>.db annotation progress
  python -m renes status work/<stem>.db   what the work loop should do next (/renes-cycle)
  python -m renes evidence work/<stem>.db traces + knock-out screenshots for annotated routines
  python -m renes dashboard work/<stem>.db  local dashboard: overview, research, review, activity
  python -m renes brief work/<stem>.db ... game research facts (manual, walkthroughs) with sources
  python -m renes codes work/<stem>.db ... Game Genie codes: research input or held-out test set
  python -m renes agent work/<stem>.db    Stage B: macro-actions chosen by novelty, random or Jev
  python -m renes report work/<stem>.db   coach report for the latest agent run
  python -m renes tas work/<stem>.db movie.fm2   replay a TAS movie: coverage + savestates across the game
  python -m renes benchmark work/<stem>.db --reference ref.asm   aggregate scores vs a reference
"""
import argparse
import json
import sys
from pathlib import Path

from . import analyze, hardware, rom as romlib
from .analyze import PRG_BANK
from .review import BRIEF_KINDS
from .kb import INPUT_BUG, KB, LIVE_STATES, NON_ROM, Label
from .render import render
from .verify import BuildError, build_and_verify

ROOT = Path(__file__).resolve().parent.parent


def stem_of(name: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in Path(name).stem.lower())
    return "_".join(filter(None, keep.split("_")))


def cmd_init(args: argparse.Namespace) -> int:
    rom = romlib.load(Path(args.rom))
    if rom.mapper not in romlib.MAPPERS:
        supported = ", ".join(f"{m} ({n})" for m, n in romlib.MAPPERS.items())
        print(f"mapper {rom.mapper} is not supported yet (supported: {supported})", file=sys.stderr)
        return 2
    kb_path = Path(args.kb) if args.kb else ROOT / "work" / f"{stem_of(rom.name)}.db"
    if kb_path.exists():
        if not args.force:
            print(f"{kb_path} exists; pass --force to rebuild it", file=sys.stderr)
            return 2
        kb_path.unlink()
    kb_path.parent.mkdir(parents=True, exist_ok=True)
    with KB(kb_path) as kb:
        kb.store_rom(rom)
        for addr, name, comment in hardware.REGISTERS:
            kb.add_label(Label(NON_ROM, addr, name, "hardware", "builtin", "hardware"))
            kb.add_comment(NON_ROM, addr, "line", comment, "builtin", "hardware")
        results = analyze.analyze_and_store(kb, rom)
    code = sum(r.code_bytes() for r in results)
    print(f"{rom.name}: mapper {rom.mapper} ({romlib.MAPPERS[rom.mapper]}), PRG {len(rom.prg) // 1024}K"
          + (f" in {len(results)} banks" if len(results) > 1 else "") + f", CHR {len(rom.chr) // 1024}K"
          + ("" if rom.chr else " (CHR RAM)"))
    print(f"static analysis: {code} code bytes ({code / len(rom.prg):.1%} of PRG), "
          f"{sum(len(r.labels) for r in results)} labels")
    print(f"knowledge base: {kb_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    kb_path = Path(args.kb)
    with KB(kb_path) as kb:
        rom = kb.rom()
        rendered = render(kb)
    stem = kb_path.stem
    out_dir = Path(args.out) if args.out else ROOT / "out" / stem
    try:
        result = build_and_verify(rendered, rom.raw, out_dir, stem)
    except BuildError as e:
        print(e, file=sys.stderr)
        record_build(kb_path, False, f"build failed: {str(e)[:200]}")
        return 1
    if result.identical:
        print(f"OK: {result.rom_path} is byte-identical to {rom.name} ({result.size} bytes, SHA-1 {rom.sha1})")
        record_build(kb_path, True, f"byte-identical to {rom.name}")
        return 0
    print(f"MISMATCH at file offset ${result.first_diff:X} ({result.rom_path})", file=sys.stderr)
    record_build(kb_path, False, f"MISMATCH at file offset ${result.first_diff:X}")
    return 1


def cmd_analyze(args: argparse.Namespace) -> int:
    with KB(Path(args.kb)) as kb:
        rom = kb.rom()
        cdl = kb.coverage("nesPrgRom", "cdl")
        if cdl is None:
            print("no dynamic coverage recorded; analyzing statically (run 'explore' for better results)")
        results = analyze.analyze_and_store(kb, rom, cdl)
    prg = len(rom.prg)
    by_source: dict[str, int] = {}
    code = words = cdl_data = 0
    for result, bank in zip(results, rom.banks()):
        for addr, kind in result.kinds.items():
            if kind in analyze.CODE_KINDS:
                src = result.sources.get(addr, "static")
                by_source[src] = by_source.get(src, 0) + 1
        code += result.code_bytes()
        words += sum(1 for k in result.kinds.values() if k in ("word", "word_hi"))
        cdl_data += sum(1 for i, f in enumerate((cdl or b"")[bank.offset:bank.offset + len(bank.data)])
                        if f & analyze.CDL_DATA and result.kinds.get(bank.base + i) not in analyze.CODE_KINDS)
    engines = [(r.bank, e) for r in results for e in sorted(r.jump_engines)]
    print(f"code: {code} bytes ({code / prg:.1%} of PRG) = "
          + ", ".join(f"{n} {src}" for src, n in sorted(by_source.items())))
    print(f"pointer words: {words} bytes in {sum(len(r.tables) for r in results)} jump tables (+ vectors); "
          f"jump engines at {', '.join((f'{b}:' if len(results) > 1 else '') + f'${e:04X}' for b, e in engines) or 'none'}")
    print(f"data seen by the emulator: {cdl_data} bytes ({cdl_data / prg:.1%})")
    unknown = prg - code - words - cdl_data
    print(f"unclassified: {unknown} bytes ({unknown / prg:.1%})")
    print(f"routines: {sum(len(r.routines) for r in results)}, call edges: {sum(len(r.calls) for r in results)}, "
          f"labels: {sum(len(r.labels) for r in results)}")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    from . import annotate

    with KB(Path(args.kb)) as kb:
        ctxs = annotate.contexts(kb)
        if args.entry:
            found = [annotate.resolve_routine(kb, ctxs, e) for e in args.entry]
            missing = [f for f in found if isinstance(f, str)]
            if missing:
                print("\n".join(missing), file=sys.stderr)
                return 2
            by_bank = {c.bank: c for c in ctxs}
            units = [(by_bank[b], e) for b, e in found]
        else:
            units = annotate.queue_all(ctxs)[:args.count]
        if not units:
            print("nothing left to annotate")
            return 0
        print("\n\n".join(annotate.work_unit(c, e) for c, e in units))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from . import annotate

    kb_path = Path(args.kb)
    proposals = annotate.load_proposals(Path(args.proposal))
    out_dir = Path(args.out) if args.out else ROOT / "out" / kb_path.stem
    report, ok = annotate.apply(kb_path, proposals, out_dir)
    for line in report.accepted:
        print(f"  + {line}")
    for line in report.rejected:
        print(f"  ! {line}")
    for line in report.warnings:
        print(f"  ? {line}")
    for line in report.requeued:
        print(f"  ~ sent back for a better name: {line}")
    print(("APPLIED" if ok else "ROLLED BACK") + f": {len(report.accepted)} accepted, {len(report.rejected)} rejected"
          + ("; ROM still byte-identical" if ok else ""))
    if ok:
        record_build(kb_path, True, f"byte-identical after applying {len(report.accepted)} changes")
    with KB(kb_path) as kb:
        kb.db.execute("UPDATE activity SET summary=? WHERE id=(SELECT max(id) FROM activity WHERE command='apply')",
                      (f"{'applied' if ok else 'rolled back'}: {len(report.accepted)} accepted, {len(report.rejected)} rejected",))
        kb.commit()
    return 0 if ok else 1


def cmd_grade(args: argparse.Namespace) -> int:
    """Semantic grading of a sample of names against the reference, by an isolated grader agent."""
    from . import grade

    kb_path = Path(args.kb)
    out = grade.grading_dir(kb_path, ROOT)
    if args.action == "prepare":
        with KB(kb_path) as kb:
            if not kb.setting("reference"):
                print("grade: run `renes benchmark <kb> --reference <file>` first", file=sys.stderr)
                return 2
        s = grade.prepare(kb_path, out, args.sample, args.seed)
        print(f"{s['pairs']} pairs ({', '.join(f'{n} {k}' for k, n in s['by_kind'].items())}) written to "
              f"{out / 'packet.json'}.\nGrade them in a separate agent with the renes-grade skill (never the "
              f"annotator: the packet holds reference names), then `python -m renes grade {args.kb} record`.")
        return 0
    if args.action == "record":
        print(grade.format_results(grade.record(kb_path, out)), end="")
        return 0
    with KB(kb_path) as kb:
        results = grade.latest(kb)
    print(f"graded {results['created']}\n" + grade.format_results(results) if results else "not graded yet")
    return 0


def cmd_confidence(args: argparse.Namespace) -> int:
    """Recompute evidence scores; list the claims the evidence supports least."""
    from . import confidence

    with KB(Path(args.kb)) as kb:
        s = confidence.refresh(kb)
        kb.commit()
        banks = [b.index for b in kb.rom().banks()]
        names = {(l.bank, l.addr): l.name for b in [*banks, NON_ROM] for l in kb.labels(b)}
        rows = [(c - e, bank, addr, e, c, f) for bank in [*banks, NON_ROM]
                for addr, (e, c, _, f) in confidence.scores(kb, bank).items() if c is not None]
    print(f"{s['scored']} scored, mean evidence {s['mean']}; {s['low']} below {confidence.LOW}, "
          f"{s['overconfident']} overconfident (claim >= evidence + {confidence.OVERCONFIDENT}), "
          f"{s['incoherent']} routine names with placeholder parts or unbacked roles")
    print("Least supported claims:")
    for gap, bank, addr, e, c, f in sorted(rows, reverse=True)[:args.top]:
        name = names.get((bank, addr), f"${addr:04X}")
        print(f"  {name:<32} claimed {c:.2f}, evidence {e:.2f}: {confidence.describe(f)}")
    return 0


def cmd_rework(args: argparse.Namespace) -> int:
    """Send routines with weak names back to the queue when what they depend on is now named."""
    from . import rework

    with KB(Path(args.kb)) as kb:
        sent = rework.scan(kb)
        kb.commit()
        names = {l.addr: l.name for b in kb.rom().banks() for l in kb.labels(b.index)}
    for entry, why in sent:
        print(f"  ~ {names.get(entry, f'${entry:04X}')}: {why}")
    print(f"{len(sent)} routines sent back to the front of the annotation queue")
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    from . import annotate

    with KB(Path(args.kb)) as kb:
        p = annotate.progress(kb)
    print(f"routines annotated: {p['annotated']}/{p['routines']} ({p['annotated'] / max(p['routines'], 1):.0%}), "
          f"ready now (all callees done): {p['ready_now']}")
    print(f"named routines: {p['named_routines']}, RAM labels: {p['ram_labels']}, "
          f"line comments: {p['line_comments']}")
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    from . import experiment

    specs = experiment.load_specs(Path(args.spec))
    try:
        results = experiment.run_all(Path(args.kb), specs)
    except experiment.SpecError as e:
        print(f"bad experiment spec: {e}", file=sys.stderr)
        return 2
    with KB(Path(args.kb)) as kb:
        names = {l.addr: l.name for l in kb.labels(PRG_BANK)}
        spans = [(e, last) for e, last, _ in kb.routines(PRG_BANK)]

    def where(pc: int) -> str:
        entry = next((e for e, last in spans if e <= pc <= last), None)
        return f"${pc:04X} in {names.get(entry, f'${entry:04X}')}" if entry is not None else f"${pc:04X}"

    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  #{r.experiment_id} {r.name}  ({r.frames} frames)")
        for c in r.checks:
            what = c.check.get("check") + (f" {c.check['watch']}" if "watch" in c.check else "")
            print(f"    {'ok ' if c.passed else 'XX '} {what}: {c.detail}")
        for run, wrote in r.writers.items():
            print(f"    writers ({run}): " + ("; ".join(f"${a:04X} <- {where(pc)} x{n}" for a, pc, n in wrote)
                                            or "none"))
        if r.promoted:
            print(f"    -> {r.promoted} is now verified")
        elif r.note:
            print(f"    -> {r.note}")
    return 0 if all(r.passed for r in results) else 1


def cmd_experiments(args: argparse.Namespace) -> int:
    from . import experiment

    with KB(Path(args.kb)) as kb:
        for exp_id in args.invalidate or []:
            if not args.reason:
                print("--invalidate needs a --reason", file=sys.stderr)
                return 2
            target = kb.invalidate_experiment(exp_id, args.reason)
            if target is None:
                print(f"#{exp_id}: no such experiment", file=sys.stderr)
                return 2
            print(f"#{exp_id} marked invalid")
            bank, addr = target
            label = kb.label_at(bank, addr) if addr is not None else None
            if label and label.status == "verified" and not experiment.still_verified(kb, bank, addr):
                kb.set_label_status(bank, addr, "candidate")
                print(f"    -> {label.name} is a candidate again: no other experiment verifies it")
        kb.commit()
        rows = kb.experiments(include_invalid=True)
        invalid = kb.invalid_experiments()
        names = {(l.bank, l.addr): f"{l.name} [{l.status}]" for l in kb.labels()}
    for exp_id, created, bank, addr, name, passed in rows:
        target = f"${addr:04X} {names.get((bank, addr), '')}" if addr is not None else "-"
        state = "INVALID" if exp_id in invalid else "PASS" if passed else "FAIL"
        print(f"#{exp_id:<4} {state:<7}  {created}  {target:<40} {name}"
              + (f"  ({invalid[exp_id]})" if exp_id in invalid else ""))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from . import experiment

    spec = {"state": args.state} if args.state is not None else {"setup": json.loads(args.setup)}
    print(experiment.probe(Path(args.kb), spec, args.frames, changes=args.changes))
    return 0


def cmd_states(args: argparse.Namespace) -> int:
    """Archived savestates (explorer, agent and TAS runs), filtered by RAM values they hold."""
    from .annotate import parse_addr

    with KB(Path(args.kb)) as kb:
        ram_names = {l.name.lower(): l.addr for l in kb.labels(NON_ROM)}
        names = {l.addr: l.name for l in kb.labels(NON_ROM)}

        def addr_of(text: str) -> int:
            text = text.strip()
            if text.lower() in ram_names:
                return ram_names[text.lower()]
            try:
                return parse_addr(text)
            except ValueError:
                raise SystemExit(f"states: {text!r} is neither a RAM label nor an address") from None

        wants = []
        for cond in args.where:
            left, sep, right = cond.partition("=")
            if not sep:
                raise SystemExit(f"states: expected LABEL=value or $ADDR=value, got {cond!r}")
            wants.append((addr_of(left), parse_addr(right) if right.strip().startswith("$") else int(right, 0)))
        if args.retire or args.restore:
            return _retire_runs(kb, args.retire, args.restore, args.why)
        show = [a for a, _ in wants] + [addr_of(s) for s in args.show]
        kinds = dict(kb.db.execute("SELECT id, kind FROM runs"))
        rows = [(sid, run, frame, bytes(ram)) for sid, run, frame, ram in kb.db.execute(
            f"SELECT id, run_id, frame, ram FROM states WHERE {LIVE_STATES} ORDER BY id")]
        retired = kb.db.execute(f"SELECT count(*) FROM states WHERE NOT ({LIVE_STATES})").fetchone()[0]
    hits = [r for r in rows if all(a < len(r[3]) and r[3][a] == v for a, v in wants)]
    print(f"{len(hits)} of {len(rows)} archived states" + (f" where {', '.join(args.where)}" if wants else "")
          + (f" ({retired} from retired runs left out)" if retired else ""))
    if not hits:
        return 1
    if args.writes:
        from . import experiment

        target = addr_of(args.writes)
        print(f"playing each for {args.frames} frames, watching writes to {names.get(target, f'${target:04X}')}")
        found = experiment.find_writes(Path(args.kb), target, [r[0] for r in hits], args.frames)
        with KB(Path(args.kb)) as kb:
            code = {l.addr: l.name for l in kb.labels(PRG_BANK)}
            spans = [(e, last) for e, last, _ in kb.routines(PRG_BANK)]
        where = lambda pc: next((code.get(e, f"${e:04X}") for e, last in spans if e <= pc <= last), "?")
        by_id = {r[0]: r for r in hits}
        print(f"{len(found)} of {len(hits)} states write it within {args.frames} frames")
        for sid, wrote in found[:args.limit]:
            print(f"{sid:>6} run {by_id[sid][1]:>3} frame {by_id[sid][2]:>6}  "
                  + ", ".join(f"${pc:04X} in {where(pc)} x{n}" for pc, n in wrote[:4]))
        return 0 if found else 1
    head = "".join(f" {names.get(a, f'${a:04X}')[:14]:>14}" for a in show)
    print(f"{'state':>6} {'run':>4} {'kind':<14} {'frame':>7}{head}")
    for sid, run, frame, ram in hits[:args.limit]:
        cells = "".join(f" {ram[a] if a < len(ram) else '-':>14}" for a in show)
        print(f"{sid:>6} {run:>4} {(kinds.get(run) or '')[:14]:<14} {frame:>7}{cells}")
    if len(hits) > args.limit:
        print(f"... {len(hits) - args.limit} more (--limit)")
    print('start an experiment or probe there: "state": <id> / --state <id>')
    return 0


def _retire_runs(kb: KB, retire: str | None, restore: str | None, why: str | None) -> int:
    """Retire runs (their states are no longer listed or picked) or restore them. `retire` is run ids
    ("7,8,9") or "input-bug" for every run started while button input was broken."""
    if retire == "input-bug":
        ids = [i for (i,) in kb.db.execute("SELECT id FROM runs WHERE started >= ? AND started < ?", INPUT_BUG)]
        why = why or "played while button input was broken"
    else:
        ids = [int(x) for x in (retire or restore).split(",") if x.strip()]
    known = {i for (i,) in kb.db.execute("SELECT id FROM runs")}
    if not ids or set(ids) - known:
        raise SystemExit(f"states: no run {', '.join(map(str, sorted(set(ids) - known))) or 'matches'}")
    for i in ids:
        kb.db.execute("UPDATE runs SET retired=? WHERE id=?", (None if restore else why or "retired", i))
    kb.commit()
    n = kb.db.execute(f"SELECT count(*) FROM states WHERE run_id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
    print(f"{'restored' if restore else 'retired'} runs {', '.join(map(str, ids))} ({n} archived states)")
    return 0


def cmd_explore(args: argparse.Namespace) -> int:
    from .explore import explore  # imports the emulator bridge only when needed

    kb_path = Path(args.kb)
    setup = json.loads(args.setup) if args.setup else None
    print(f"exploring {kb_path.name} for {args.minutes} min (seed {args.seed})"
          + (f" from state {args.state}" if args.state is not None else "") + (" after the setup" if setup else ""))
    r = explore(kb_path, args.minutes, args.seed, args.frames, state=args.state, setup=setup)
    with KB(kb_path) as kb:
        prg_size = len(kb.rom().prg)
    print(f"run {r.run_id}: {r.frames} frames, {r.actions} actions in {r.seconds:.0f}s "
          f"({r.frames / max(r.seconds, 1e-9):.0f} fps), archive {r.archive_size} states")
    print(f"coverage (all runs): code {r.code_bytes} ({r.code_bytes / prg_size:.1%}), "
          f"data {r.data_bytes} ({r.data_bytes / prg_size:.1%}), subroutine entries {r.sub_entries}")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from . import agent  # imports the emulator bridge only when needed

    kb_path = Path(args.kb)
    plan_path = Path(args.plan) if args.plan else kb_path.with_name(kb_path.stem + ".explore_plan.json")
    with KB(kb_path) as kb:
        if args.init_plan or not plan_path.exists():
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(agent.plan_json(agent.default_plan(kb)), encoding="utf-8")
            print(f"wrote default plan to {plan_path}")
            if args.init_plan:
                return 0
        try:
            plan = agent.load_plan(plan_path, kb)
        except agent.PlanError as e:
            print(f"bad plan: {e}", file=sys.stderr)
            return 2
    budget = f", at most {args.max_calls} Jev calls" if args.policy == "jev" else ""
    print(f"agent on {kb_path.name}: policy {args.policy}, {args.steps} steps{budget}, "
          f"{len(plan.actions)} actions, watching {len(plan.watch)} variables")
    try:
        r = agent.run_agent(kb_path, plan, args.policy, args.steps, args.frames, args.minutes, args.max_calls,
                            args.seed)
    except agent.PlanError as e:
        print(f"bad plan: {e}", file=sys.stderr)
        return 2
    with KB(kb_path) as kb:
        prg_size = len(kb.rom().prg)
    print(f"run {r.run_id}: {r.steps} steps, {r.frames} frames in {r.seconds:.0f}s; stopped: {r.stop}")
    print(f"code this run {r.run_code} ({r.run_code / prg_size:.1%}), new beyond earlier runs {r.new_code}; "
          f"all runs {r.code_bytes} ({r.code_bytes / prg_size:.1%})")
    if r.usage:
        print(f"Jev: {r.usage['calls']} calls, {r.usage['input_tokens']} input tokens (~${r.usage['usd']})"
              + (f"; {r.usage['disabled']}" if r.usage.get("disabled") else ""))
    report = Path(args.report) if args.report else kb_path.with_name(kb_path.stem + ".explore_report.md")
    report.write_text(agent.report(kb_path, r.run_id), encoding="utf-8")
    print(f"coach report: {report}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from . import agent

    print(agent.report(Path(args.kb), args.run, args.unreached))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Is the project finished? The checks the loop works through after annotation is complete."""
    from . import finish

    with KB(Path(args.kb)) as kb:
        checks = finish.checklist(kb)
    print(finish.format_checklist(checks), end="")
    print("finished: `python -m renes export " + args.kb + "` writes the hand-over package" if finish.finished(checks)
          else "not finished yet: the loop's next stages work through the open checks")
    return 0 if finish.finished(checks) else 1


def cmd_export(args: argparse.Namespace) -> int:
    from . import finish

    target = finish.export(Path(args.kb), ROOT / "out")
    print(f"exported to {target} (annotated source that rebuilds the ROM, README, routines.md, ram.md, images). "
          "It contains the game's code and data: keep it local.")
    return 0


def cmd_movies(args: argparse.Namespace) -> int:
    """TAS movies for this ROM on TASVideos; --download one into roms/tas/<id>M/."""
    from . import movies

    kb_path = Path(args.kb)
    tas_dir = ROOT / "roms" / "tas"
    if args.download:
        path = movies.download(args.download, tas_dir)
        print(f"downloaded {path}\nreplay it: python -m renes tas {args.kb} {path}")
        return 0
    with KB(kb_path) as kb:
        rom = kb.rom()
    found = movies.find(rom.name, rom.sha1, ROOT / "out" / "tasvideos_nes.json")
    if not found:
        print(f"no TASVideos publication for {rom.name}")
        return 0
    for m in found:
        fmt = "replayable" if m["file"].lower().endswith((".fm2", ".bk2")) else "format not supported"
        print(f"{m['id']:>5}M  {m['rom']:<40} {m['frames']:>6} frames  {m['file']} ({fmt})\n"
              f"        {m['title']}")
    print(f"download one: python -m renes movies {args.kb} --download <id>  (exact ROM + replayable first)")
    return 0


def cmd_tas(args: argparse.Namespace) -> int:
    from . import tas

    kb_path, movie_path = Path(args.kb), Path(args.movie)
    name = args.name or (movie_path.parent.name if movie_path.parent.name not in ("tas", "roms") else movie_path.stem)
    try:
        movie = tas.load_movie(movie_path)
        fixes = json.loads(Path(args.fix).read_text(encoding="utf-8")) if args.fix else []
        if fixes:
            movie = tas.apply_fixes(movie, fixes)
        shots_dir = ROOT / "out" / stem_of(kb_path.name) / "tas" / name if args.shots else None
        print(f"replaying {movie_path.name} ({len(movie.frames)} frames, {len(fixes)} resync fixes) as '{name}'")
        r, samples = tas.replay(kb_path, movie, name, args.until, args.every, args.shots or 0, shots_dir,
                                args.force, {"fixes": fixes})
    except (tas.MovieError, OSError, ValueError) as e:
        print(f"tas: {e}", file=sys.stderr)
        return 2
    with KB(kb_path) as kb:
        prg_size = len(kb.rom().prg)
        first = kb.db.execute("SELECT min(id), max(id) FROM states WHERE run_id=?", (r.run_id,)).fetchone()
        rare = tas.rare_changes(samples, kb)
    print(f"run {r.run_id}: {r.frames} frames in {r.seconds:.0f}s; {r.states} states archived (ids {first[0]}-{first[1]}, "
          f"one per {args.every} frames)")
    print(f"code this run {r.run_code} ({r.run_code / prg_size:.1%}), new beyond earlier runs {r.new_code}; "
          f"all runs {r.code_bytes} ({r.code_bytes / prg_size:.1%})")
    if r.shots:
        print(f"sync check: {len(r.shots)} screenshots in {shots_dir}")
    report = Path(args.report) if args.report else kb_path.with_name(kb_path.stem + ".tas_report.md")
    report.write_text(rare, encoding="utf-8")
    print(f"rare-change RAM report: {report}")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from . import benchmark

    kb_path = Path(args.kb)
    with KB(kb_path) as kb:
        history = kb.benchmarks()
        seen = kb.setting("reference_seen")
    if seen:
        print(f"warning: the reference was read on {seen}; names written after that are not blind", file=sys.stderr)
    if args.history:
        for bid, created, ref, results in history:
            n = json.loads(results)["naming"]["all"]
            c = json.loads(results)["classification"]
            print(f"#{bid} {created} {ref}: split agreement {c['agreement_on_classified']:.2%}, "
                  f"names {n['names']} (match {n['match_rate'] if n['match_rate'] is not None else '-'})")
        return 0
    reference = args.reference
    with KB(kb_path) as kb:
        if reference:
            kb.set_setting("reference", str(Path(reference).resolve()))
            kb.commit()
        else:
            reference = kb.setting("reference")
    if not reference:
        print("benchmark: --reference <disassembly.asm> is required the first time", file=sys.stderr)
        return 2
    details = ROOT / "out" / stem_of(kb_path.name) / "benchmark_details.md"
    try:
        results = benchmark.run(kb_path, Path(reference), details)
    except (benchmark.ReferenceError, OSError) as e:
        print(f"benchmark: {e}", file=sys.stderr)
        return 2
    previous = json.loads(history[-1][3]) if history else None
    print(benchmark.format_results(results, previous))
    print(f"(per-name details for the human reviewer: {details})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import cycle

    with KB(Path(args.kb)) as kb:
        reference = kb.setting("reference")
        s = cycle.status(kb, has_reference=bool(reference and Path(reference).exists()))
    print(cycle.format_status(s), end="")
    return 0


def cmd_round(args: argparse.Namespace) -> int:
    """One pass of the work loop: `start` picks the stage (like status) and records the metrics,
    `end` records them again; `list` shows each round's duration and progress."""
    from . import cycle

    from . import claims

    agent = claims.agent_name(args.agent)
    with KB(Path(args.kb)) as kb:
        if args.action == "start":
            ok, message = claims.claim(kb, agent, args.force)
            print(message)
            if not ok:
                return 3
            kb.commit()
            from . import library
            from .review import find_projects
            found = library.refresh(Path(args.kb), find_projects(Path(args.kb).parent))   # cheap: about a second
            if found["routines"]:
                print(f"same code in other games: {found['routines']} routines ({found['unannotated']} not annotated yet)")
            from . import feedback
            print(feedback.format_messages(feedback.inbox(ROOT / "work", Path(args.kb).stem)), end="")
            from .rework import stale_claims
            stale = stale_claims(kb)
            if stale:
                print("summaries that say code never ran, though it has run since (fix them before an audit): "
                      + ", ".join(f"{name} ({share:.0%} ran; {where})" for _, name, share, where in stale[:8])
                      + (f" and {len(stale) - 8} more" if len(stale) > 8 else ""))
            for r in feedback.undelivered(ROOT / "work", Path(args.kb).stem):
                print(f"reply to your feedback #{r['id']} ({r['status']}): {r['reply']}")
            reference = kb.setting("reference")
            s = cycle.status(kb, has_reference=bool(reference and Path(reference).exists()))
            rid = cycle.start_round(kb, s, agent)
            print(cycle.format_status(s), end="")
            print(f"round #{rid} started; finish it with `python -m renes round end {args.kb}"
                  + (f" --agent {agent}" if agent else "") + " --note ...`")
        elif args.action == "end":
            current = claims.owner(kb)
            if agent and current and current["agent"] == agent:
                claims.claim(kb, agent)                      # refresh: still working on it
            r = cycle.end_round(kb, args.note)
            if r is None:
                print("no open round")
                return 1
            print(cycle.format_round(r))
        elif args.action == "resume":
            kb.set_setting("loop_resumed", cycle._now())
            kb.commit()
            print("earlier rounds without progress no longer stop the loop")
        else:
            for r in cycle.rounds(kb, args.limit):
                print(cycle.format_round(r))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Reference-free quality audit: an isolated auditor agent judges whether the code supports the names."""
    from . import audit, grade

    kb_path = Path(args.kb)
    out = audit.audit_dir(kb_path, ROOT)
    grading = grade.grading_dir(kb_path, ROOT)
    if args.action == "prepare":
        like = grading / "packet.json" if args.like_grading else out / "packet.json" if args.repeat else None
        if like is not None and not like.exists():
            print(f"audit: no {'grading' if args.like_grading else 'earlier audit'} packet for this project", file=sys.stderr)
            return 2
        s = audit.prepare(kb_path, out, args.routines, args.ram, args.seed, like)
        print(f"{s['items']} items ({s['routines']} routines, {s['ram']} RAM labels) written to {out / 'packet.json'}."
              f"\nAudit them in a separate agent with the renes-audit skill (not the annotator), then "
              f"`python -m renes audit {args.kb} record`.")
        return 0
    if args.action == "record":
        results = audit.record(kb_path, out, grading)
        print(audit.format_results(results), end="")
        if results["sent_back"]:
            print(f"{results['sent_back']} unsupported names sent back to the annotator with the auditor's reason; "
                  f"after they're reworked, `audit {args.kb} prepare --repeat` measures the change")
        return 0
    with KB(kb_path) as kb:
        results = audit.latest(kb)
    print(f"audited {results['created']}\n" + audit.format_results(results) if results else "not audited yet")
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    """Match this project's routines against the other projects' annotated code."""
    from . import library
    from .review import find_projects

    kb_path = Path(args.kb)
    s = library.refresh(kb_path, find_projects(kb_path.parent))
    print(f"compared with {', '.join(s['projects']) or 'no other projects'}: {s['matches']} matches in "
          f"{s['routines']} routines ({s['unannotated']} not annotated yet); work units show them")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    from . import claims, feedback

    work = ROOT / "work"
    project = Path(args.project).stem if args.project else None
    ids = feedback._ids(args.id or "")
    if args.file:      # notes with quotes, dashes or $addresses survive no shell this way
        args.text = (sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")).strip()
    if args.action == "add":
        if not args.text:
            print("feedback add: give the note as text", file=sys.stderr)
            return 2
        fid = feedback.add(work, args.kind, args.text, claims.agent_name(args.agent), project)
        print(f"feedback #{fid} recorded; the reply will show at a later round start")
    elif args.action == "close":
        if not ids or not args.text:
            print("feedback close: --id N and a reply text", file=sys.stderr)
            return 2
        for fid in ids:
            if not feedback.close(work, fid, args.text, "wontfix" if args.wontfix else "done"):
                print(f"no feedback #{fid}", file=sys.stderr)
                return 1
            print(f"feedback #{fid} closed")
    elif args.action == "ack":
        if not ids or not args.text:
            print('feedback ack: --id 4,9 and a message ("working on it: ...")', file=sys.stderr)
            return 2
        try:
            feedback.ack(work, ids, args.text)
        except ValueError as e:
            print(f"feedback ack: {e}", file=sys.stderr)
            return 1
        print(f"told the agents behind {', '.join(f'#{i}' for i in ids)} that they are being worked on")
    elif args.action == "patch":
        if not args.text:
            print("feedback patch: the patch note text (what changed in renes)", file=sys.stderr)
            return 2
        mid = feedback.patch(work, args.text, ids)
        print(f"patch note {mid} published to every project" + (f"; closed {', '.join(f'#{i}' for i in ids)}" if ids else ""))
    elif args.action == "news":
        rows = feedback.messages(work, args.limit)
        print(feedback.format_messages(rows) or "no patch notes yet\n", end="")
    elif args.action == "wait":
        # run in the background: this process exiting is what wakes the waiting session
        rows = feedback.wait(work, project, args.after, timeout=args.minutes * 60)
        if not rows:
            print(f"no feedback {'replies' if project else 'notes'} within {args.minutes:g} minutes")
            return 1
        print(("replies for " + project if project else "new feedback") + ":")
        print(feedback.format_items(rows), end="")
        if project:
            print(f"resume the loop: python -m renes round start {args.project} ...")
    else:
        print(feedback.format_items(feedback.items(work, args.all, project)), end="")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    from . import claims

    agent = claims.agent_name(args.agent)
    with KB(Path(args.kb)) as kb:
        if args.action == "show":
            print(claims.describe(claims.owner(kb)))
            return 0
        ok, message = (claims.claim if args.action == "take" else claims.release)(kb, agent, args.force)
        kb.commit()
    print(message)
    return 0 if ok else 3


def cmd_visual(args: argparse.Namespace) -> int:
    """Pictures for the annotator: tile sheets, dispatch tables, and probes of what values look like."""
    from . import visual
    from .annotate import contexts, parse_addr

    kb_path = Path(args.kb)
    out = ROOT / "out" / kb_path.stem
    with KB(kb_path) as kb:
        kb.set_setting("export_dir", str(out))
        kb.commit()
        ctxs = contexts(kb)
        ctx = ctxs[0]
        names = {l.addr: l.name for c in ctxs for l in kb.labels(c.bank)}
        ram = {l.addr: l.name for l in kb.labels(NON_ROM)}
        sites = [d for c in ctxs for d in visual.dispatches(c)]
        if args.action == "metatiles":
            from . import metatiles
            found = metatiles.export(kb_path, out)
            for t in found:
                print(f"${t.addr:04X}: {t.count} blocks ({t.layout}, {t.order} order; {t.hits} seen on screen)")
            print(f"{len(found)} metatile tables; pictures in {out / 'metatiles'}" if found else
                  "no 16x16 block tables found: this game builds its screens from other units (use the tile sheets)")
            return 0
        if args.action == "tiles":
            n = visual.export_tiles(kb, out)
            if n:
                kb.set_setting("tiles_exported", str(out / "tiles"))
                kb.commit()
            print(f"{n} tiles written to {out / 'tiles'} (p0/XX.png, p1/XX.png and a sheet per pattern table)"
                  if n else "no CHR ROM in this cartridge (CHR RAM): tiles exist only at run time")
            return 0
        if args.action == "dispatch":
            for d in sites:
                index = (f"{ram.get(d.index_addr, f'${d.index_addr:04X}')}" + (f",{d.indexed}" if d.indexed else "")
                         if d.index_addr is not None else "-")
                shown = "probed" if d.site in visual.probed_sites(kb) else "skipped" if d.site in visual.skipped_sites(kb) else ""
                print(f"${d.site:04X} in {names.get(d.owner, f'${d.owner:04X}')}: {len(d.handlers)} entries, "
                      f"index {index} ({d.source}) {shown}")
            return 0
    if args.action == "skip":
        with KB(kb_path) as kb:
            visual.skip_site(kb, parse_addr(args.target), args.reason)
        print(f"{args.target} marked skipped: the loop stops asking for it")
        return 0
    if args.action == "probe":
        site = parse_addr(args.target)
        d = next((d for d in sites if d.site == site), None)
        if d is None or d.index_addr is None:
            print(f"no dispatch with a known index variable at {args.target}; see `renes visual {args.kb} dispatch`", file=sys.stderr)
            return 2
        slot, state = args.slot or 0, args.state
        if d.indexed and args.slot is None:
            # Pick a start state and object slot where this table actually runs (backlog item 10).
            state, slots = visual.find_slot(kb_path, d, state=args.state)
            if not slots:
                with KB(kb_path) as kb:
                    visual.skip_site(kb, d.site, "the table never ran in any start state tried")
                print(f"${d.site:04X} never ran in the start states tried; marked skipped (use --slot/--state to force)")
                return 1
            slot = slots[0][0]
            print(f"start {'state ' + str(state) if state is not None else 'after pressing Start'}, slot {slot}: "
                  f"{d.indexed.upper()} values seen at the JSR: " + ", ".join(f"{s} ({n} calls)" for s, n in slots[:6]))
        addr = d.index_addr + (slot if d.indexed else 0)
        values = [d.value(i) for i in range(len(d.handlers))]
        print(f"probing ${d.site:04X}: {ram.get(d.index_addr, f'${d.index_addr:04X}')} = each of {len(values)} values, {args.frames} frames each")
        visual.probe(kb_path, addr, values, out, site=d.site, state=state, frames=args.frames)
        return 0
    if args.action == "values":
        if not args.values:
            print("--values is required, e.g. --values 0-1F", file=sys.stderr)
            return 2
        lo, _, hi = args.values.partition("-")
        values = list(range(int(lo, 16), int(hi or lo, 16) + 1))
        with KB(kb_path) as kb:
            label = kb.label_named(args.target)
        addr = (label.addr if label else parse_addr(args.target)) + (args.slot or 0)
        print(f"probing ${addr:04X} = {len(values)} values, {args.frames} frames each")
        visual.probe(kb_path, addr, values, out, site=0, state=args.state, frames=args.frames)
        return 0
    return 2


def _build(kb_path: Path) -> dict:
    with KB(kb_path) as kb:
        rom = kb.rom()
        rendered = render(kb)
    try:
        result = build_and_verify(rendered, rom.raw, ROOT / "out" / kb_path.stem, kb_path.stem)
    except BuildError as e:
        return {"ok": False, "message": f"build failed: {e}"}
    out = ({"ok": True, "message": f"rebuilt: byte-identical to {rom.name}"} if result.identical
           else {"ok": False, "message": f"MISMATCH at file offset ${result.first_diff:X}"})
    record_build(kb_path, out["ok"], out["message"])
    return out


def cmd_evidence(args: argparse.Namespace) -> int:
    from . import evidence
    from .annotate import contexts, resolve_routine

    kb_path = Path(args.kb)
    if args.movie:
        with KB(kb_path) as kb:
            kb.set_setting("evidence_movie", json.dumps({"path": str(Path(args.movie).resolve()), "until": args.until}))
            kb.commit()
    entries = None
    if args.entry:
        with KB(kb_path) as kb:
            found = [resolve_routine(kb, contexts(kb), e) for e in args.entry]
        bad = [f for f in found if isinstance(f, str)]
        if bad:
            print("\n".join(bad), file=sys.stderr)
            return 2
        entries = found
    if entries is None and args.limit:
        with KB(kb_path) as kb:
            done = set(kb.db.execute("SELECT bank, entry FROM knockouts WHERE bank>=0"))
            entries = [(c.bank, e) for c in contexts(kb) for e in sorted(c.notes) if (c.bank, e) not in done][:args.limit]
    from .experiment import capture_media
    n = capture_media(kb_path)
    if n:
        print(f"captured screenshots for {n} earlier experiments")
    if args.experiments_only:
        return 0
    try:
        r = evidence.collect(kb_path, entries, args.frames)
    except FileNotFoundError as e:
        print(f"evidence: {e}", file=sys.stderr)
        return 2
    print(f"{r.reached} of {r.routines} routines ran during the playback: {r.traced} traced, {r.knocked} knocked out")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from . import review

    if args.log:
        # Started at logon by pythonw (no console): stdout/stderr go nowhere, so log to a file.
        log = open(args.log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    source = Path(args.kb) if args.kb else ROOT / "work"
    review.serve(source, _build, args.port, not args.no_browser)
    return 0


def research_mode(kb: KB) -> str:
    """benchmark (default when a reference disassembly is configured) or open."""
    return kb.setting("research_mode") or ("benchmark" if kb.setting("reference") else "open")


def cmd_brief(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    with KB(Path(args.kb)) as kb:
        if args.action == "mode":
            if args.text:
                if args.text[0] not in ("benchmark", "open"):
                    print("mode is benchmark or open", file=sys.stderr)
                    return 2
                kb.set_setting("research_mode", args.text[0])
                kb.commit()
            print(f"research mode: {research_mode(kb)}")
            return 0
        if args.action == "add":
            text = " ".join(args.text).strip()
            if not text or not args.source:
                print("brief add needs text and --source <url or citation>", file=sys.stderr)
                return 2
            kb.db.execute("INSERT INTO brief (kind, text, source, created) VALUES (?,?,?,?)",
                          (args.kind, text[:600], args.source, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            kb.commit()
            print(f"added [{args.kind}] {text[:80]}")
            return 0
        rows = kb.db.execute("SELECT id, kind, text, source FROM brief ORDER BY kind, id").fetchall()
        print(f"research mode: {research_mode(kb)}; {len(rows)} facts")
        for bid, kind, text, source in rows:
            print(f"#{bid} [{kind}] {text}  ({source})")
    return 0


def cmd_codes(args: argparse.Namespace) -> int:
    from . import codes

    kb_path = Path(args.kb)
    with KB(kb_path) as kb:
        mode = research_mode(kb)
        if args.action == "add":
            use = args.use or ("test" if mode == "benchmark" else "research")
            lines = Path(args.arg).read_text(encoding="utf-8").splitlines()
            added, problems = codes.add_codes(kb, lines, args.source or args.arg, use)
            for p in problems:
                print(f"  skipped: {p}")
            print(f"added {added} codes as {use} codes" + (" (evaluation only: their descriptions stay hidden from the annotator)"
                                                          if use == "test" else ""))
            return 0
        if args.action == "list":
            n_test = kb.db.execute("SELECT count(*) FROM codes WHERE use='test'").fetchone()[0]
            print(codes.research_listing(kb))
            if n_test:
                print(f"({n_test} test codes hidden; use 'codes check' for aggregate scores)")
            return 0
        if args.action == "check":
            details = ROOT / "out" / stem_of(kb_path.name) / "codes_details.md"
            result = codes.evaluate(kb, details)
            for k, v in result.items():
                print(f"{k:>28}: {v}")
            print(f"(per-code details for the human reviewer: {details})")
            return 0
    if args.action == "try":
        r = codes.try_code(kb_path, args.arg.upper(), args.frames)
        print(r["message"])
        for d in r.get("differs", [])[:20]:
            print(f"  {d['name']}: control {d['control']:02X}, with code {d['treatment']:02X}")
        return 0 if r["ok"] else 1
    return 2


def _unclassified(kb: KB) -> list[tuple[int, int, str, list[str]]]:
    """PRG ranges neither code, pointer words nor data the emulator saw read (the "unclassified" of
    analyze): (first, last, where, routines whose operands point into the range)."""
    from .opcodes import ABSOLUTE_MODES, OPCODES, operand_value

    from .annotate import contexts

    out = []
    for ctx in contexts(kb):
        out += [(a, b, f"bank {ctx.bank}, {where}" if ctx.multi else where, readers)
                for a, b, where, readers in _unclassified_bank(kb, ctx)]
    return out


def _unclassified_bank(kb: KB, ctx) -> list[tuple[int, int, str, list[str]]]:
    from .opcodes import ABSOLUTE_MODES, OPCODES, operand_value

    prg, base = ctx.prg, ctx.base
    cdl = ctx.cdl or bytes(len(prg))
    kinds = ctx.kinds
    known = lambda a: kinds.get(a) in (*analyze.CODE_KINDS, "word", "word_hi") or cdl[a - base] & analyze.CDL_DATA
    spans = [(e, last) for e, (last, _) in ctx.routines.items()]
    names = {l.addr: l.name for l in kb.labels(ctx.bank)}
    owner = lambda a: next((e for e, last in spans if e <= a <= last), None)
    refs = [(pc, operand_value(OPCODES[prg[pc - base]], prg, pc - base)) for pc, k in kinds.items()
            if k == "op" and OPCODES[prg[pc - base]].mode in ABSOLUTE_MODES]
    out, a, end = [], base, base + len(prg)
    while a < end:
        if known(a):
            a += 1
            continue
        b = a
        while b + 1 < end and not known(b + 1):
            b += 1
        inside = owner(a)
        name = lambda e: names.get(e, f"${e:04X}")
        before = max((x for x in names if x < a), default=None)
        after = min((x for x in names if x > b), default=None)
        where = (f"inside {name(inside)}" if inside is not None and inside == owner(b) else
                 f"between {name(before) if before is not None else 'start'} and {name(after) if after is not None else 'end'}")
        readers = sorted({name(owner(pc)) for pc, v in refs if v is not None and a <= v <= b and owner(pc) is not None})
        out.append((a, b, where, readers))
        a = b + 1
    return out


def cmd_coverage(args: argparse.Namespace) -> int:
    if args.unclassified:
        with KB(Path(args.kb)) as kb:
            ranges = _unclassified(kb)
        total = sum(b - a + 1 for a, b, _, _ in ranges)
        print(f"{total} unclassified bytes in {len(ranges)} ranges (largest first): never executed, never read as "
              "data, not a pointer table")
        for a, b, where, readers in sorted(ranges, key=lambda r: r[0] - r[1])[:args.limit]:
            print(f"  ${a:04X}-${b:04X} {b - a + 1:>5} bytes  {where}"
                  + (f"; operands of {', '.join(readers[:4])} point here" if readers else ""))
        if len(ranges) > args.limit:
            print(f"  ... {len(ranges) - args.limit} more (--limit)")
        return 0
    with KB(Path(args.kb)) as kb:
        prg_size = len(kb.rom().prg)
        cdl = kb.coverage("nesPrgRom", "cdl")
        runs = kb.db.execute("SELECT id, started, seed, frames, wall_seconds, code_bytes FROM runs "
                             "WHERE frames IS NOT NULL ORDER BY id").fetchall()
        writes = kb.counters("nesInternalRam", "writeCount")
    if cdl is None:
        print("no dynamic coverage yet; run 'explore' first")
        return 1
    flags = {"code": 0x01, "data": 0x02, "jump targets": 0x04, "subroutine entries": 0x08}
    for name, bit in flags.items():
        n = sum(1 for f in cdl if f & bit)
        print(f"{name:>20}: {n:>6}" + (f"  ({n / prg_size:.1%} of PRG)" if bit < 4 else ""))
    either = sum(1 for f in cdl if f & 0x03)
    print(f"{'code or data':>20}: {either:>6}  ({either / prg_size:.1%} of PRG)")
    if writes:
        print(f"{'RAM bytes written':>20}: {sum(1 for w in writes if w):>6}  of {len(writes)}")
    for run_id, started, seed, frames, secs, code in runs:
        print(f"  run {run_id} {started} seed={seed} frames={frames} {secs}s code-after={code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="renes")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="create a knowledge base from a ROM")
    pi.add_argument("rom")
    pi.add_argument("--kb")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_init)
    pb = sub.add_parser("build", help="render, assemble and verify the round trip")
    pb.add_argument("kb")
    pb.add_argument("--out")
    pb.set_defaults(func=cmd_build)
    pn = sub.add_parser("next", help="print annotation work units (bottom-up along the call graph)")
    pn.add_argument("kb")
    pn.add_argument("--count", type=int, default=1)
    pn.add_argument("--entry", nargs="+", help="specific routine entry addresses, e.g. $C200")
    pn.set_defaults(func=cmd_next)
    pg = sub.add_parser("grade", help="semantic benchmark: sample name pairs for an isolated grader, record aggregates")
    pg.add_argument("kb")
    pg.add_argument("action", choices=["prepare", "record", "show"])
    pg.add_argument("--sample", type=int, default=150)
    pg.add_argument("--seed", type=int, default=1)
    pg.set_defaults(func=cmd_grade)
    pc = sub.add_parser("confidence", help="recompute evidence scores and list the least supported claims")
    pc.add_argument("kb")
    pc.add_argument("--top", type=int, default=15)
    pc.set_defaults(func=cmd_confidence)
    pw = sub.add_parser("rework", help="re-queue routines whose placeholder names can now be improved")
    pw.add_argument("kb")
    pw.set_defaults(func=cmd_rework)
    pp = sub.add_parser("apply", help="validate and apply an annotation proposal (JSON), keep only if ROM verifies")
    pp.add_argument("kb")
    pp.add_argument("proposal")
    pp.add_argument("--out")
    pp.set_defaults(func=cmd_apply)
    pg = sub.add_parser("progress", help="annotation progress")
    pg.add_argument("kb")
    pg.set_defaults(func=cmd_progress)
    px = sub.add_parser("experiment", help="run A/B experiments from a JSON spec; passing ones verify labels")
    px.add_argument("kb")
    px.add_argument("spec")
    px.set_defaults(func=cmd_experiment)
    pl = sub.add_parser("experiments", help="list recorded experiments")
    pl.add_argument("kb")
    pl.add_argument("--invalidate", type=int, nargs="+", metavar="ID",
                    help="mark these experiments as proving nothing (no longer evidence)")
    pl.add_argument("--reason", help="why, for --invalidate")
    pl.set_defaults(func=cmd_experiments)
    pr = sub.add_parser("probe", help="report RAM that reacts to each button, and counters, from a start state")
    pr.add_argument("kb")
    pr.add_argument("--setup", default='[[60, "none"], [4, "start"], [240, "none"]]',
                    help="input script from power-on, as JSON [[frames, buttons], ...]")
    pr.add_argument("--state", type=int, help="explorer archive state id instead of --setup")
    pr.add_argument("--frames", type=int, default=60)
    pr.add_argument("--changes", action="store_true",
                    help="also list every byte that changes while idle (pick experiment watches from it)")
    pr.set_defaults(func=cmd_probe)
    pst = sub.add_parser("states", help="list archived savestates, filtered by RAM values (start states for experiments)")
    pst.add_argument("kb")
    pst.add_argument("where", nargs="*", help="LABEL=value or $ADDR=value, e.g. Bonus_Mode=1")
    pst.add_argument("--show", action="append", default=[], help="extra RAM label/address column (repeatable)")
    pst.add_argument("--limit", type=int, default=30)
    pst.add_argument("--writes", metavar="LABEL|$ADDR",
                     help="play each matching state idle and list where code writes this byte (start states)")
    pst.add_argument("--frames", type=int, default=60, help="--writes: frames to play from each state")
    pst.add_argument("--retire", metavar="RUNS|input-bug",
                     help="stop listing and picking these runs' states: run ids (7,8,9) or input-bug")
    pst.add_argument("--restore", metavar="RUNS", help="undo --retire for these run ids")
    pst.add_argument("--why", help="--retire: the reason, shown when a spec names one of the states")
    pst.set_defaults(func=cmd_states)
    pa = sub.add_parser("analyze", help="re-run code/data analysis using recorded coverage")
    pa.add_argument("kb")
    pa.set_defaults(func=cmd_analyze)
    pe = sub.add_parser("explore", help="fuzz the game in the emulator and record coverage")
    pe.add_argument("kb")
    pe.add_argument("--minutes", type=float, default=5.0)
    pe.add_argument("--frames", type=int, help="stop after this many emulated frames")
    pe.add_argument("--seed", type=int, default=1)
    pe.add_argument("--state", type=int, help="start from this archived state (renes states), e.g. a TAS state")
    pe.add_argument("--setup", help="inputs played first, as JSON: '[[60,\"none\"],[4,\"select\"]]'")
    pe.set_defaults(func=cmd_explore)
    pg2 = sub.add_parser("agent", help="Stage B exploration: a policy picks macro-actions from a plan")
    pg2.add_argument("kb")
    pg2.add_argument("--plan", default=None,     # default: work/<project>.explore_plan.json (one per project)
                     help="plan JSON (goal, actions, watched variables, start); written with defaults if missing")
    pg2.add_argument("--init-plan", action="store_true", help="(re)write the default plan and exit")
    pg2.add_argument("--policy", choices=("novelty", "random", "jev"), default="novelty")
    pg2.add_argument("--steps", type=int, default=300, help="macro-actions to take")
    pg2.add_argument("--frames", type=int, help="also stop after this many emulated frames")
    pg2.add_argument("--minutes", type=float, help="also stop after this much wall time")
    pg2.add_argument("--max-calls", type=int, default=100, help="Jev call budget (then falls back to novelty)")
    pg2.add_argument("--seed", type=int, default=1)
    pg2.add_argument("--report", default=None, help="default: work/<project>.explore_report.md")
    pg2.set_defaults(func=cmd_agent)
    pt = sub.add_parser("report", help="coach report for an agent run (default: the latest)")
    pt.add_argument("kb")
    pt.add_argument("--run", type=int)
    pt.add_argument("--unreached", type=int, default=20, help="unreached routines to list")
    pt.set_defaults(func=cmd_report)
    pf2 = sub.add_parser("finish", help="finishing checklist: is this project done?")
    pf2.add_argument("kb")
    pf2.set_defaults(func=cmd_finish)
    pe2 = sub.add_parser("export", help="write the hand-over package (annotated source, README, routine and RAM maps)")
    pe2.add_argument("kb")
    pe2.set_defaults(func=cmd_export)
    pm = sub.add_parser("movies", help="find TAS movies for this ROM on TASVideos; --download one")
    pm.add_argument("kb")
    pm.add_argument("--download", type=int, metavar="ID", help="publication id (e.g. 2963) to save in roms/tas/<id>M/")
    pm.set_defaults(func=cmd_movies)
    ps = sub.add_parser("tas", help="replay a TAS movie (.fm2/.bk2): coverage, savestates, rare-change RAM")
    ps.add_argument("kb")
    ps.add_argument("movie")
    ps.add_argument("--name", help="short name for this movie (default: its folder, e.g. the TASVideos id)")
    ps.add_argument("--until", type=int, help="stop at this frame (before a known desync)")
    ps.add_argument("--every", type=int, default=600, help="archive a savestate every N frames")
    ps.add_argument("--shots", type=int, default=3000, help="screenshot every N frames for the sync check (0: none)")
    ps.add_argument("--fix", help='resync edits JSON: [{"frame": 17950, "insert": 1}, {"frame": 30000, "delete": 1}]')
    ps.add_argument("--force", action="store_true", help="replay even if the movie's ROM checksum differs")
    ps.add_argument("--report", default=None, help="default: work/<project>.tas_report.md")
    ps.set_defaults(func=cmd_tas)
    pb2 = sub.add_parser("benchmark", help="aggregate scores against a reference disassembly (no names shown)")
    pb2.add_argument("kb")
    pb2.add_argument("--reference", help="reference .asm that reassembles to this ROM")
    pb2.add_argument("--history", action="store_true", help="list earlier benchmark results")
    pb2.set_defaults(func=cmd_benchmark)
    pq = sub.add_parser("status", help="state of the work loop and the recommended next stage")
    pq.add_argument("kb")
    pq.set_defaults(func=cmd_status)
    pu = sub.add_parser("round", help="record a loop round (start/end), list rounds, or resume after a stall")
    pu.add_argument("action", choices=["start", "end", "list", "resume"])
    pu.add_argument("kb")
    pu.add_argument("--note", default="", help="end: one line on what the round did")
    pu.add_argument("--limit", type=int, default=20)
    pu.add_argument("--agent", help="your agent name (or set RENES_AGENT): start claims the project for you")
    pu.add_argument("--force", action="store_true", help="start: take the project from another agent's fresh claim")
    pu.set_defaults(func=cmd_round)
    pa = sub.add_parser("audit", help="reference-free quality audit of a sample of names by an isolated auditor agent")
    pa.add_argument("kb")
    pa.add_argument("action", choices=["prepare", "record", "show"])
    pa.add_argument("--routines", type=int, default=45)
    pa.add_argument("--ram", type=int, default=15)
    pa.add_argument("--seed", type=int, default=1)
    pa.add_argument("--like-grading", action="store_true", help="prepare: audit the items of the last grading packet")
    pa.add_argument("--repeat", action="store_true", help="prepare: audit the same items as last time (after rework)")
    pa.set_defaults(func=cmd_audit)
    pl = sub.add_parser("library", help="find routines with the same code in other projects (hints for work units)")
    pl.add_argument("kb")
    pl.set_defaults(func=cmd_library)
    pk = sub.add_parser("claim", help="show, take or release this project's loop claim (one loop per project)")
    pk.add_argument("kb")
    pk.add_argument("action", nargs="?", choices=["show", "take", "release"], default="show")
    pk.add_argument("--agent", help="your agent name (or set RENES_AGENT)")
    pk.add_argument("--force", action="store_true")
    pk.set_defaults(func=cmd_claim)
    pf = sub.add_parser("feedback", help="notes from loop agents to the renes developer: bugs, tool limits, ideas")
    pf.add_argument("action", choices=["add", "list", "close", "wait", "ack", "patch", "news"],
                    help="wait: block until new notes arrive (developer) or, with --project, replies to them (agent); "
                         "ack: tell agents their notes are being worked on; patch: publish a patch note; news: list them")
    pf.add_argument("text", nargs="?", help="add: the note; close: the reply; ack/patch: the message")
    pf.add_argument("--kind", choices=["bug", "limit", "idea", "insight"], default="bug")
    pf.add_argument("--project", help="add: the knowledge base it is about; list: only this project")
    pf.add_argument("--agent", help="your agent name (or set RENES_AGENT)")
    pf.add_argument("--id", help="close/ack/patch: note number(s), e.g. 4 or 4,9 (patch: the notes it fixes)")
    pf.add_argument("--wontfix", action="store_true", help="close without a change")
    pf.add_argument("--all", action="store_true", help="list: closed notes too")
    pf.add_argument("--after", type=int, help="wait: only notes after this number (default: from now on)")
    pf.add_argument("--minutes", type=float, default=720, help="wait: give up after this long")
    pf.add_argument("--limit", type=int, default=20, help="news: how many recent messages")
    pf.add_argument("--file", help="read the note/reply text from this file ('-': stdin) instead of the argument")
    pf.set_defaults(func=cmd_feedback)
    pz = sub.add_parser("visual", help="images for annotation: tile sheets, dispatch tables, probes of values")
    pz.add_argument("kb")
    pz.add_argument("action", choices=["tiles", "metatiles", "dispatch", "probe", "values", "skip"])
    pz.add_argument("target", nargs="?", help="probe/skip: the dispatch site ($XXXX); values: a RAM label or $address")
    pz.add_argument("--reason", default="no start state shows it", help="skip: why the table is left unprobed")
    pz.add_argument("--values", help="values: hex range, e.g. 0-1F")
    pz.add_argument("--slot", type=int, default=None,
                    help="offset added to the address for arrays indexed by X/Y (default: picked from a run)")
    pz.add_argument("--state", type=int, help="explorer archive state to start from (default: press Start, wait)")
    pz.add_argument("--frames", type=int, default=40, help="frames to hold each value before the screenshot")
    pz.set_defaults(func=cmd_visual)
    pv = sub.add_parser("evidence", help="record traces and knock-out screenshots for annotated routines")
    pv.add_argument("kb")
    pv.add_argument("--entry", nargs="*", help="routine entries ($XXXX); default: annotated routines without evidence")
    pv.add_argument("--limit", type=int, help="at most this many routines")
    pv.add_argument("--frames", type=int, default=120, help="knock-out comparison length")
    pv.add_argument("--movie", help="TAS movie to play back (remembered); default: the configured one, else scripted play")
    pv.add_argument("--until", type=int, help="with --movie: last in-sync frame")
    pv.add_argument("--experiments-only", action="store_true", help="only add screenshots to earlier experiments")
    pv.set_defaults(func=cmd_evidence)
    pw = sub.add_parser("review", aliases=["dashboard"],
                        help="local project dashboard (127.0.0.1): overview, research review, annotation review, activity")
    pw.add_argument("kb", nargs="?", help="one knowledge base (default: every project in work/)")
    pw.add_argument("--port", type=int, default=8765)
    pw.add_argument("--no-browser", action="store_true")
    pw.add_argument("--log", help="append output to this file (for running without a console, e.g. at logon)")
    pw.set_defaults(func=cmd_review)
    pf = sub.add_parser("brief", help="game research facts with sources (manual, walkthroughs, maps)")
    pf.add_argument("kb")
    pf.add_argument("action", choices=("add", "list", "mode"))
    pf.add_argument("text", nargs="*", help="add: the fact; mode: benchmark|open")
    pf.add_argument("--kind", default="mechanic", choices=BRIEF_KINDS)
    pf.add_argument("--source", help="URL or citation (required for add)")
    pf.set_defaults(func=cmd_brief)
    pk = sub.add_parser("codes", help="Game Genie codes: add, list (research), check (test set), try")
    pk.add_argument("kb")
    pk.add_argument("action", choices=("add", "list", "check", "try"))
    pk.add_argument("arg", nargs="?", help="add: file of 'CODE description' lines; try: the code")
    pk.add_argument("--source", help="where the codes came from")
    pk.add_argument("--use", choices=("research", "test"), help="default: test in benchmark mode, else research")
    pk.add_argument("--frames", type=int, default=600)
    pk.set_defaults(func=cmd_codes)
    pc = sub.add_parser("coverage", help="summarize recorded dynamic coverage")
    pc.add_argument("kb")
    pc.add_argument("--unclassified", action="store_true",
                    help="list the ROM ranges nothing classified yet, with the routines around them")
    pc.add_argument("--limit", type=int, default=40)
    pc.set_defaults(func=cmd_coverage)
    args = p.parse_args(argv)
    kb = getattr(args, "kb", None)
    if kb and args.cmd != "init" and not Path(kb).exists():
        # Opening a missing path would silently create an empty knowledge base (e.g. a file named
        # "list" from `renes claim list`): refuse, and say which projects exist.
        known = ", ".join(sorted(str(q).replace("\\", "/") for q in (ROOT / "work").glob("*.db")
                                 if "backup" not in q.stem.lower())) or "none yet"
        print(f"renes {args.cmd}: no knowledge base at {kb!r}. The project file comes first, e.g. "
              f"`python -m renes {args.cmd} work/<game>.db ...`; create one with `python -m renes init <rom>`. "
              f"Projects: {known}", file=sys.stderr)
        return 2
    if args.cmd not in LOGGED or not Path(getattr(args, "kb", None) or "").is_file():
        return args.func(args)
    return _logged(args, argv if argv is not None else sys.argv[1:])


# Commands that change the project; the dashboard's activity feed shows them.
LOGGED = {"build", "analyze", "apply", "experiment", "explore", "agent", "tas", "evidence", "benchmark", "visual",
          "brief", "codes", "rework", "grade", "audit", "library", "export"}


def _logged(args: argparse.Namespace, argv: list[str]) -> int:
    from datetime import datetime, timezone

    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    kb_path = Path(args.kb)
    shown = " ".join(a for a in argv[1:] if a != args.kb)[:200]
    from . import claims

    with KB(kb_path) as kb:
        current = claims.owner(kb)
        agent = claims.agent_name() or (current["agent"] if current and current["fresh"] else None)
        row = kb.db.execute("INSERT INTO activity (command, args, started, agent) VALUES (?,?,?,?)",
                            (args.cmd, shown, now(), agent)).lastrowid
        kb.commit()
    code = 1
    try:
        code = args.func(args)
        return code
    finally:
        with KB(kb_path) as kb:
            kb.db.execute("UPDATE activity SET finished=?, exit_code=? WHERE id=?", (now(), code, row))
            kb.commit()


def record_build(kb_path: Path, ok: bool, message: str) -> None:
    from datetime import datetime, timezone
    with KB(kb_path) as kb:
        kb.set_setting("last_build", json.dumps({"ok": ok, "message": message,
                                                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        kb.commit()
