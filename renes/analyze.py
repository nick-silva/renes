"""Code/data separation: emulator evidence first, static recursive descent second.

Order of trust:
1. Interrupt vectors.
2. Instruction starts the emulator proved: CDL jump targets, subroutine entries, and the first
   byte of every executed run. (Mesen's execCount counts operand bytes too, so it cannot mark
   starts by itself.) A proven start inside another instruction's operand splits that
   instruction: the outer opcode becomes data, as in the `BIT abs` ($2C) skip trick.
3. Recursive descent from those into code the fuzzer never reached, never claiming a byte the
   emulator only ever read as data (CDL data flag without code flag).
4. Jump tables placed after `JSR <jump engine>` (a routine that pops its return address and
   ends in `JMP (ind)`), decoded as `.addr` words whose targets become routines.

Classification only has to be *consistent* for the round trip to hold; this module makes it
*correct* as far as the evidence allows.
"""
from dataclasses import dataclass, field

from .hardware import VECTORS
from .kb import KB, Label
from .opcodes import ABSOLUTE_MODES, IMM, IND, IZY, OPCODES, REL, TERMINATORS, branch_target, operand_value
from .rom import Rom

PRG_BANK = 0
CDL_CODE, CDL_DATA, CDL_JUMP, CDL_SUB = 0x01, 0x02, 0x04, 0x08
MAX_TABLE_ENTRIES = 128
# Name precedence when several references land on one address.
_RANK = {"vector": 0, "routine": 1, "table": 2, "code": 3, "data": 4}
_PREFIX = {"routine": "Sub", "table": "Tbl", "code": "L", "data": "Data"}
CODE_KINDS = ("op", "operand")


@dataclass
class Analysis:
    kinds: dict[int, str] = field(default_factory=dict)          # cpu addr -> op | operand | word | word_hi | data
    sources: dict[int, str] = field(default_factory=dict)        # cpu addr -> exec | static | table | vector | skip
    labels: dict[int, tuple[str, str]] = field(default_factory=dict)  # cpu addr -> (name, kind)
    routines: dict[int, tuple[int, str]] = field(default_factory=dict)  # entry -> (last byte, how found)
    calls: set[tuple[int, int, int, str]] = field(default_factory=set)  # (caller entry, callee, site, jsr|jmp|table)
    jump_engines: set[int] = field(default_factory=set)
    tables: dict[int, int] = field(default_factory=dict)         # table start -> entries
    bank: int = PRG_BANK
    prefix: str = ""           # "B1_" in multi-bank ROMs: names are unique across banks

    def name(self, addr: int, kind: str, name: str | None = None) -> None:
        current = self.labels.get(addr)
        if current is None or _RANK[kind] < _RANK[current[1]]:
            self.labels[addr] = (self.prefix + (name or f"{_PREFIX[kind]}{addr:04X}"), kind)

    def code_bytes(self) -> int:
        return sum(1 for k in self.kinds.values() if k in CODE_KINDS)


def is_jump_engine(prg: bytes, base: int, addr: int) -> bool:
    """A routine that pulls its own return address and dispatches through JMP (ind)."""
    pulls = indirect_loads = 0
    pc = addr
    for _ in range(16):
        if not base <= pc < base + len(prg):
            return False
        op = OPCODES.get(prg[pc - base])
        if op is None or pc + op.size > base + len(prg):
            return False
        if op.mnemonic == "pla":
            pulls += 1
        elif op.mnemonic == "lda" and op.mode == IZY:
            indirect_loads += 1
        elif op.mnemonic == "jmp":
            return op.mode == IND and pulls >= 2 and indirect_loads >= 1
        elif op.mnemonic in ("rts", "rti", "brk"):
            return False
        pc += op.size
    return False


def analyze(rom: Rom, cdl: bytes | None = None, bank: int = PRG_BANK, prefix: str = "",
            entries: set[int] = frozenset()) -> Analysis:
    """One bank: `rom` holds just its PRG (Rom.bank_rom) and `cdl` its slice of the CDL. `entries`:
    routine entries known from elsewhere (code other banks call through a bank switch)."""
    prg, base = rom.prg, rom.prg_base
    end = base + len(prg)
    a = Analysis(bank=bank, prefix=prefix)

    def in_prg(addr: int) -> bool:
        return base <= addr < end

    def word_at(addr: int) -> int:
        return prg[addr - base] | (prg[addr - base + 1] << 8)

    def executed(addr: int) -> bool:
        return cdl is not None and in_prg(addr) and bool(cdl[addr - base] & CDL_CODE)

    def data_only(addr: int) -> bool:
        return cdl is not None and bool(cdl[addr - base] & CDL_DATA) and not cdl[addr - base] & CDL_CODE

    def name(addr: int, kind: str) -> None:
        if in_prg(addr):
            a.name(addr, kind)

    def claim(pc: int, op, proven_only: bool, relaxed: bool = False) -> bool:
        span = range(pc, pc + op.size)
        if span[-1] >= end or any(x in a.kinds for x in span) or (not relaxed and any(data_only(x) for x in span)):
            return False
        all_executed = all(executed(x) for x in span)
        if proven_only and not all_executed:
            return False
        a.kinds[pc] = "op"
        for x in span[1:]:
            a.kinds[x] = "operand"
        for x in span:
            a.sources[x] = "exec" if all_executed else "static"
        return True

    engine_cache: dict[int, bool] = {}

    def jump_engine(addr: int) -> bool:
        if addr not in engine_cache:
            engine_cache[addr] = in_prg(addr) and is_jump_engine(prg, base, addr)
            if engine_cache[addr]:
                a.jump_engines.add(addr)
        return engine_cache[addr]

    # 1. Vectors
    seeds: list[int] = []
    for vaddr, vname in VECTORS:
        a.kinds[vaddr], a.kinds[vaddr + 1] = "word", "word_hi"
        a.sources[vaddr] = a.sources[vaddr + 1] = "vector"
        target = word_at(vaddr)
        if in_prg(target):
            a.name(target, "vector", vname)
            a.routines[target] = (target, "vector")
            seeds.append(target)
    for target in sorted(entries):
        if in_prg(target) and target not in a.routines:
            a.name(target, "routine")
            a.routines[target] = (target, "far call")
            seeds.append(target)

    # 2. Proven instruction starts
    proven: set[int] = set(seeds)
    if cdl is not None:
        for i, f in enumerate(cdl):
            addr = base + i
            if f & (CDL_JUMP | CDL_SUB) or (f & CDL_CODE and not executed(addr - 1)):
                proven.add(addr)
    seeds += sorted(proven - set(seeds))

    visited: set[int] = set()

    def split_at(t: int) -> None:
        """A proven start inside an operand: the enclosing instruction's head becomes data."""
        s = t
        while a.kinds.get(s) == "operand":
            s -= 1
        if a.kinds.get(s) != "op":
            return
        size = OPCODES[prg[s - base]].size
        for y in range(s, t):
            a.kinds[y], a.sources[y] = "data", "skip"
        for y in range(t, s + size):
            a.kinds.pop(y, None)
            a.sources.pop(y, None)
        visited.discard(s)

    # 3+4. Recursive descent, with jump tables after calls to a jump engine
    def parse_table(start: int) -> list[int]:
        targets, addr = [], start
        while len(targets) < MAX_TABLE_ENTRIES and in_prg(addr + 1):
            # Words the game read as data are fine (the jump engine reads them), even if some
            # glitchy path once fetched them as code too; bytes only ever executed are not a table.
            if (a.kinds.get(addr, "data") != "data" or a.kinds.get(addr + 1, "data") != "data"
                    or (executed(addr) and not cdl[addr - base] & CDL_DATA)
                    or (executed(addr + 1) and not cdl[addr + 1 - base] & CDL_DATA)):
                break
            target = word_at(addr)
            if not in_prg(target) or OPCODES.get(prg[target - base]) is None or data_only(target):
                break
            a.kinds[addr], a.kinds[addr + 1] = "word", "word_hi"
            a.sources[addr] = a.sources[addr + 1] = "table"
            targets.append(target)
            addr += 2
        if targets:
            a.tables[start] = len(targets)
            a.name(start, "table")
        return targets

    data_refs: set[int] = set()
    table_sites: list[tuple[int, list[int]]] = []

    def descend(work: list[int], proven_only: bool, relaxed: bool = False) -> list[int]:
        """Returns the addresses it could not decode under the current rules (the frontier).
        relaxed: may claim bytes the CDL only saw read as data (for checked fall-throughs)."""
        frontier: list[int] = []
        while work:
            pc = work.pop()
            while in_prg(pc) and pc not in visited:
                if a.kinds.get(pc) == "operand" and pc in proven:
                    split_at(pc)
                if a.kinds.get(pc) == "op":
                    op = OPCODES[prg[pc - base]]
                else:
                    op = OPCODES.get(prg[pc - base])
                    if op is None or op.mnemonic == "brk":
                        break  # undocumented opcode or BRK: almost certainly data
                    if not claim(pc, op, proven_only, relaxed):
                        hidden = next((y for y in range(pc + 1, pc + op.size) if a.kinds.get(y) == "op"), None)
                        if op.mnemonic == "bit" and hidden is not None and pc not in a.kinds:
                            # BIT skip trick seen from the other side: its operand is a known instruction.
                            for y in range(pc, hidden):
                                a.kinds[y], a.sources[y] = "data", "skip"
                        else:
                            frontier.append(pc)
                        break  # overlap, known data, or (phase A) not proven
                visited.add(pc)
                value = operand_value(op, prg, pc - base)

                if op.mode == REL:
                    target = branch_target(pc, value)
                    name(target, "code")
                    work.append(target)
                elif op.mnemonic == "jsr":
                    name(value, "routine")
                    work.append(value)
                    if jump_engine(value):
                        targets = parse_table(pc + 3)
                        table_sites.append((pc, targets))
                        for t in targets:
                            name(t, "routine")
                            work.append(t)
                        break  # the engine never returns here
                elif op.mnemonic == "jmp" and op.mode != IND:
                    name(value, "code")
                    work.append(value)
                elif op.mode in ABSOLUTE_MODES and in_prg(value):
                    data_refs.add(value)  # includes JMP (ind) pointers

                if op.mnemonic in TERMINATORS:
                    break
                pc += op.size
        return frontier

    frontier = descend(list(reversed(seeds)), proven_only=cdl is not None)  # phase A: what ran
    descend(frontier, proven_only=False)                                    # phase B: what could run

    # Phase C: some games read their own code as data (a checksum, random numbers from ROM), so the
    # CDL data flag also lands on code that simply never ran. Decode the fall-through of a
    # conditional branch anyway when the branch can go either way and the bytes decode cleanly up
    # to an RTS/RTI/JMP or known code.
    def always_taken(pc: int) -> bool:
        prev = next((p for p in range(pc - 3, pc) if a.kinds.get(p) == "op"
                     and p + OPCODES[prg[p - base]].size == pc), None)
        if prev is None:
            return False
        flag, branch = OPCODES[prg[prev - base]], OPCODES[prg[pc - base]].mnemonic
        pairs = {"clc": "bcc", "sec": "bcs", "clv": "bvc"}
        if pairs.get(flag.mnemonic) == branch:
            return True
        if flag.mnemonic in ("lda", "ldx", "ldy") and flag.mode == IMM:
            v = prg[prev + 1 - base]
            return {"bne": v != 0, "beq": v == 0, "bpl": v < 0x80, "bmi": v >= 0x80}.get(branch, False)
        return False

    def decodes_cleanly(pc: int) -> bool:
        for _ in range(256):
            if a.kinds.get(pc) == "op":
                return True                            # runs into known code
            op = OPCODES.get(prg[pc - base]) if in_prg(pc) else None
            if op is None or op.mnemonic == "brk" or pc + op.size > end \
                    or any(x in a.kinds for x in range(pc, pc + op.size)):
                return False
            if op.mnemonic in TERMINATORS:
                return True
            pc += op.size
        return False

    for _ in range(16):                                # each decoded chunk can expose more
        found = [pc + 2 for pc in sorted(visited) if a.kinds.get(pc) == "op" and OPCODES[prg[pc - base]].mode == REL
                 and in_prg(pc + 2) and pc + 2 not in a.kinds and data_only(pc + 2)
                 and not always_taken(pc) and decodes_cleanly(pc + 2)]
        if not found:
            break
        descend(found, proven_only=False, relaxed=True)

    for addr in data_refs:
        kind = a.kinds.get(addr)
        if kind in (None, "data"):
            a.name(addr, "data")
        elif kind == "op":
            a.name(addr, "code")

    _routines_and_calls(a, prg, base, cdl, table_sites)

    # Only keep labels the renderer can place on a line of their own.
    a.labels = {addr: v for addr, v in a.labels.items()
                if not in_prg(addr) or a.kinds.get(addr) in (None, "data", "op", "word")}
    return a


def _absorb_gaps(a: Analysis, prg: bytes, base: int, is_code) -> None:
    """A routine's code can continue past a gap (an embedded table, bytes only seen as data): code it
    branches or jumps into before the next entry is its own, so its calls and RAM uses count."""
    starts = sorted(a.routines)
    owned = {x for e, (last, _) in a.routines.items() for x in range(e, last + 1)}
    changed = True
    while changed:
        changed = False
        for i, e in enumerate(starts):
            last, how = a.routines[e]
            limit = starts[i + 1] if i + 1 < len(starts) else base + len(prg)
            pc = e
            while pc <= last:
                if a.kinds.get(pc) != "op":
                    pc += 1
                    continue
                op = OPCODES[prg[pc - base]]
                value = operand_value(op, prg, pc - base)
                target = branch_target(pc, value) if op.mode == REL else \
                    value if op.mnemonic == "jmp" and op.mode != IND else None
                if target is not None and last < target < limit and a.kinds.get(target) == "op" \
                        and target not in owned:
                    x = target
                    while x < limit and is_code(x):
                        last = x
                        x += 1
                    owned.update(range(e, last + 1))
                    changed = True
                pc += op.size
            a.routines[e] = (last, how)


def _falls_into(a: Analysis, prg: bytes, base: int, addr: int) -> bool:
    """Whether the instruction just before addr can run on into it."""
    for pc in range(addr - 3, addr):
        if a.kinds.get(pc) == "op" and pc + OPCODES[prg[pc - base]].size == addr:
            return OPCODES[prg[pc - base]].mnemonic not in TERMINATORS
    return False


def _routines_and_calls(a: Analysis, prg: bytes, base: int, cdl: bytes | None,
                        table_sites: list[tuple[int, list[int]]]) -> None:
    entries = {addr for addr, (_, kind) in a.labels.items() if kind in ("routine", "vector")}
    table_targets = {t for _, ts in table_sites for t in ts}
    if cdl is not None:
        # The logger also marks a few mid-routine addresses as entries (probably where an NMI interrupted:
        # wait loops) that no JSR or JMP in the ROM names; code that runs straight
        # into such an address keeps it.
        named = {operand_value(OPCODES[prg[pc - base]], prg, pc - base) for pc, k in a.kinds.items()
                 if k == "op" and OPCODES[prg[pc - base]].mnemonic in ("jsr", "jmp")} | entries | table_targets
        entries |= {base + i for i, f in enumerate(cdl) if f & CDL_SUB
                    and (base + i in named or not _falls_into(a, prg, base, base + i))}
    entries = {e for e in entries if a.kinds.get(e) == "op"}

    def is_code(pc: int) -> bool:
        return a.kinds.get(pc) in CODE_KINDS or a.sources.get(pc) == "skip"

    def extent(e: int, stops: set[int]) -> int:
        last = pc = e
        while is_code(pc) and (pc == e or pc not in stops):
            last = pc
            pc += 1
        return last

    for e in sorted(entries):
        how = a.routines.get(e, (0, ""))[1] or ("table" if e in table_targets else
                                                "jsr" if a.labels.get(e, ("", ""))[1] == "routine" else "cdl")
        a.routines[e] = (extent(e, entries), how)
        a.name(e, "routine")  # no-op for vectors, which outrank routines
    _absorb_gaps(a, prg, base, is_code)

    # Code still owned by no routine is only reached from elsewhere (shared code that other routines
    # jump or branch into): each such run becomes a routine of its own.
    owned = {x for e, (last, _) in a.routines.items() for x in range(e, last + 1)}
    heads = [pc for pc, kind in sorted(a.kinds.items())
             if kind == "op" and pc not in owned and not (is_code(pc - 1) and pc - 1 not in owned)]
    if heads:
        stops = set(a.routines) | set(heads)
        for h in heads:
            a.routines[h] = (extent(h, stops), "jmp")
            a.name(h, "routine")
        _absorb_gaps(a, prg, base, is_code)

    owner: dict[int, int] = {}
    for e, (last, _) in a.routines.items():
        for x in range(e, last + 1):
            owner[x] = e
    sites = dict(table_sites)
    for pc, kind in a.kinds.items():
        if kind != "op" or pc not in owner:
            continue
        op = OPCODES[prg[pc - base]]
        value = operand_value(op, prg, pc - base)
        if op.mnemonic == "jsr":
            a.calls.add((owner[pc], value, pc, "jsr"))
            for t in sites.get(pc, ()):
                a.calls.add((owner[pc], t, pc, "table"))
        elif op.mnemonic == "jmp" and op.mode != IND and value in a.routines and value != owner[pc]:
            a.calls.add((owner[pc], value, pc, "jmp"))
        elif op.mode == REL and branch_target(pc, value) in a.routines and branch_target(pc, value) != owner[pc]:
            a.calls.add((owner[pc], branch_target(pc, value), pc, "branch"))   # into shared code


def store(kb: KB, a: Analysis) -> None:
    kb.clear_analysis(a.bank)
    kb.set_classes([(a.bank, addr, kind, a.sources.get(addr, "static")) for addr, kind in sorted(a.kinds.items())])
    for addr, (name, kind) in sorted(a.labels.items()):
        kb.add_label(Label(a.bank, addr, name, kind, "builtin" if kind == "vector" else "auto", "analysis"))
    kb.set_routines(a.bank, [(e, last, how) for e, (last, how) in sorted(a.routines.items())])
    kb.set_calls(a.bank, sorted(a.calls))


def analyze_banks(rom: Rom, cdl: bytes | None = None, entries: dict[int, set[int]] | None = None) -> list[Analysis]:
    """Every PRG bank on its own (an AxROM bank is a complete 32 KB program with its own vectors).
    Code that switches banks continues in the new bank at the next address; the CDL proves those
    entries, and Rare-style switch stubs are duplicated at the same place in every bank."""
    banks = rom.banks()
    multi = len(banks) > 1
    return [analyze(rom.bank_rom(b), cdl[b.offset:b.offset + len(b.data)] if cdl is not None else None,
                    b.index, f"B{b.index}_" if multi else "", (entries or {}).get(b.index, frozenset()))
            for b in banks]


def analyze_and_store(kb: KB, rom: Rom, cdl: bytes | None = None) -> list[Analysis]:
    """analyze_banks, stored; with several banks, again with the entries calls from other banks reach
    (banking.far_calls follows them through the switch, which needs the first pass stored)."""
    results = analyze_banks(rom, cdl)
    for r in results:
        store(kb, r)
    if len(results) > 1:
        from .banking import far_calls

        known = {r.bank: set(r.routines) for r in results}
        entries: dict[int, set[int]] = {}
        for f in far_calls(kb):
            entries.setdefault(f.target_bank, set()).add(f.target)
        # all of them: extra seeds change the analysis, which could otherwise lose an entry it had
        if any(t not in known[b] for b, ts in entries.items() for t in ts):
            results = analyze_banks(rom, cdl, entries)
            for r in results:
                store(kb, r)
    return results
