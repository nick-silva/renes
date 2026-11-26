"""Maps of what is understood: the ROM byte by byte, the RAM byte by byte, and the call graph
grouped into subsystems. Served to the dashboard's Map tab; also snapshotted at the end of each
loop round so the maps can later be replayed over time.

ROM state per byte (one code per byte, highest wins):
  0 unknown   1 data   2 code (static only)   3 code that ran
  4 annotated candidate   5 annotated, effective confidence < 0.5 (confidence.py)   6 verified   7 approved
RAM state per byte:
  0 never read   1 read, unlabeled   2 candidate label   3 verified   4 approved   5 builtin/hardware
"""
import base64
import math
import re
import zlib
from collections import Counter, defaultdict

from .analyze import CDL_CODE, PRG_BANK
from .kb import KB, NON_ROM

ROM_STATES = ["unknown", "data", "code (static)", "code that ran", "annotated", "annotated, low confidence",
              "verified", "approved"]
RAM_STATES = ["never read", "read, unlabeled", "candidate label", "verified", "approved", "builtin"]
STOP_WORDS = {"the", "and", "set", "get", "to", "of", "for", "from", "with", "then", "if", "on", "at", "by", "or",
              "update", "run", "check", "init", "draw", "store", "load", "compute", "handle", "queue", "find",
              "slot", "object", "objects", "next", "add", "clear", "move", "do", "is", "a", "x", "y"}
HUB_CALLERS = 6          # routines called from this many places are "shared helpers"
HUB_GROUP = -1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _heat(values: list[int] | None, n: int) -> bytes:
    """0-255 log-scaled intensity per byte."""
    if not values:
        return bytes(n)
    top = max(values[:n]) or 1
    scale = 255 / math.log1p(top)
    return bytes(min(255, int(math.log1p(v) * scale)) for v in values[:n])


def linear(rom, bank: int, addr: int) -> int:
    """Map position of a CPU address in a bank: the first bank's base plus the PRG offset, so the banks
    of an AxROM game lie end to end (bank 1's $8000 is $10000). The CPU address itself for NROM."""
    b = rom.banks()
    where = next(x for x in b if x.index == bank)
    return b[0].base + where.offset + addr - where.base


def rom_state(kb: KB) -> tuple[bytes, list[dict]]:
    rom = kb.rom()
    base, size = rom.banks()[0].base, len(rom.prg)
    state = bytearray(size)
    banks = [b.index for b in rom.banks()]
    for bank in banks:
        for addr, kind in kb.classes(bank).items():
            i = linear(rom, bank, addr) - base
            if 0 <= i < size:
                state[i] = 2 if kind in ("op", "operand") else 1 if kind in ("data", "word", "word_hi") else 0
    cdl = kb.coverage("nesPrgRom", "cdl") or b""
    for i, f in enumerate(cdl[:size]):
        if f & CDL_CODE and state[i] == 2:
            state[i] = 3
    from . import confidence
    routines = []
    for bank in banks:
        checked = confidence.scores(kb, bank)
        notes = {e: (status, checked[e][2] if e in checked else conf) for e, _, status, _, conf in kb.routine_notes(bank)}
        names = {l.addr: (l.name, l.status) for l in kb.labels(bank)}
        for entry, last, _ in kb.routines(bank):
            name, label_status = names.get(entry, (f"Sub{entry:04X}", "auto"))
            status, conf = notes.get(entry, (None, None))
            level = 0
            if status is not None:
                level = 7 if label_status == "approved" else 6 if label_status == "verified" else \
                    5 if conf is not None and conf < 0.5 else 4
            first, end = linear(rom, bank, entry), linear(rom, bank, last)
            for i in range(max(0, first - base), min(size, end - base + 1)):
                if level and state[i] >= 2:
                    state[i] = level
            routines.append({"entry": first, "last": end, "name": name, "annotated": status is not None,
                             "status": label_status if status is not None else "unannotated", "confidence": conf,
                             "evidence": checked[entry][0] if entry in checked else None, "bank": bank})
    return bytes(state), routines


def rom_map(kb: KB) -> dict:
    rom = kb.rom()
    state, routines = rom_state(kb)
    size = len(rom.prg)
    data_names = [{"addr": linear(rom, b.index, l.addr), "name": l.name, "kind": l.kind}
                  for b in rom.banks() for l in kb.labels(b.index) if l.kind in ("data", "table")]
    return {"base": rom.banks()[0].base, "size": size, "states": ROM_STATES, "state": _b64(state),
            "exec_heat": _b64(_heat(kb.counters("nesPrgRom", "execCount"), size)),
            "read_heat": _b64(_heat(kb.counters("nesPrgRom", "readCount"), size)),
            "routines": routines, "data": data_names,
            "counts": dict(Counter(ROM_STATES[s] for s in state))}


def ram_state(kb: KB) -> tuple[bytes, list[dict]]:
    size = 0x800
    reads = kb.counters("nesInternalRam", "readCount") or [0] * size
    # Read more often than the least-read byte: reset loops write (and some re-read) every byte,
    # so neither writes nor that baseline mean "used".
    floor = min(reads[:size])
    state = bytearray(1 if reads[i] > floor else 0 for i in range(size))
    code = {"candidate": 2, "auto": 2, "verified": 3, "approved": 4, "builtin": 5}
    labels = []
    for l in kb.labels(NON_ROM):
        if l.kind != "ram" or l.addr >= size:
            continue
        n = max(1, l.size or 1)
        for a in range(l.addr, min(size, l.addr + n)):
            state[a] = max(state[a], code.get(l.status, 2))
        labels.append({"addr": l.addr, "size": n, "name": l.name, "status": l.status, "confidence": l.confidence})
    return bytes(state), labels


def ram_map(kb: KB) -> dict:
    size = 0x800
    state, labels = ram_state(kb)
    reads = kb.counters("nesInternalRam", "readCount") or [0] * size
    writes = kb.counters("nesInternalRam", "writeCount") or [0] * size
    return {"size": size, "states": RAM_STATES, "state": _b64(state),
            "heat": _b64(_heat([r + w for r, w in zip(reads, writes)], size)),
            "reads": reads[:size], "writes": writes[:size], "labels": labels,
            "regions": [{"addr": 0x0000, "size": 0x100, "name": "zero page"},
                        {"addr": 0x0100, "size": 0x100, "name": "stack page"},
                        {"addr": 0x0200, "size": 0x100, "name": "sprite page (OAM DMA)"}],
            "counts": dict(Counter(RAM_STATES[s] for s in state))}


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+", name)]


def subsystems(kb: KB, rounds: int = 12) -> dict:
    """Routines grouped by the call graph (label propagation), each group named by its common words."""
    _, routines = rom_state(kb)
    by_entry = {r["entry"]: r for r in routines}
    edges = set()
    rom = kb.rom()
    for b in rom.banks():                       # routine ids are map positions (unique across banks)
        for caller, callee, _, kind in kb.calls(b.index):
            caller, callee = linear(rom, b.index, caller), linear(rom, b.index, callee)
            if caller in by_entry and callee in by_entry and caller != callee:
                edges.add((caller, callee))
    # Helpers called from many places (jump engine, sprite utilities) would glue every group
    # together: they form their own group and don't take part in the propagation.
    callers_of = Counter(b for _, b in edges)
    hubs = {e for e, n in callers_of.items() if n >= HUB_CALLERS}
    adj: dict[int, set[int]] = defaultdict(set)
    for a, b in edges:
        if a not in hubs and b not in hubs:
            adj[a].add(b)
            adj[b].add(a)
    group = {e: (HUB_GROUP if e in hubs else e) for e in by_entry}
    order = sorted(by_entry)
    for _ in range(rounds):
        changed = False
        for e in order:
            if not adj[e]:
                continue
            counts = Counter(group[n] for n in adj[e])
            best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if counts[best] > counts.get(group[e], 0) and best != group[e]:
                group[e] = best
                changed = True
        if not changed:
            break
    members: dict[int, list[int]] = defaultdict(list)
    for e, g in group.items():
        members[g].append(e)
    clusters = []
    for g, es in members.items():
        words = Counter(w for e in es for w in _words(by_entry[e]["name"]) if w not in STOP_WORDS and len(w) > 2
                        and by_entry[e]["annotated"])
        top = [w for w, _ in words.most_common(3)]
        size = sum(by_entry[e]["last"] - by_entry[e]["entry"] + 1 for e in es)
        annotated = [e for e in es if by_entry[e]["annotated"]]
        label = "shared helpers" if g == HUB_GROUP else " / ".join(top) if top else f"group ${g:04X}"
        clusters.append({"id": g, "name": label, "routines": len(es),
                         "bytes": size, "annotated": len(annotated),
                         "verified": sum(1 for e in es if by_entry[e]["status"] in ("verified", "approved")),
                         "members": sorted(es)})
    clusters.sort(key=lambda c: -c["bytes"])
    nodes = [{"id": e, "name": r["name"], "bytes": r["last"] - e + 1, "group": group[e], "status": r["status"],
              "annotated": r["annotated"], "confidence": r["confidence"]} for e, r in by_entry.items()]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in sorted(edges)], "clusters": clusters}


def record_snapshot(kb: KB, round_id: int | None, created: str) -> None:
    """Per-byte ROM and RAM state, compressed, so the maps can be replayed later."""
    kb.db.execute("""CREATE TABLE IF NOT EXISTS map_snapshots (
        id INTEGER PRIMARY KEY, round_id INTEGER, created TEXT NOT NULL, rom BLOB NOT NULL, ram BLOB NOT NULL)""")
    rom, _ = rom_state(kb)
    ram, _ = ram_state(kb)
    kb.db.execute("INSERT INTO map_snapshots (round_id, created, rom, ram) VALUES (?,?,?,?)",
                  (round_id, created, zlib.compress(rom, 9), zlib.compress(ram, 9)))
