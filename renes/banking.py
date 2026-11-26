"""Calls into other banks (AxROM): which routine a bank-switching call really reaches.

An AxROM game swaps all 32 KB at once, so a call into another bank goes through a trampoline
that sits at the same address in every bank: it writes the bank register (any store to
$8000-$FFFF) and then jumps or calls into the new bank, often through a jump table. Who Framed
One such game loads X with a jump-table offset and calls $FFCA-$FFD6 (one entry per
bank); the stub stores X in $54 and calls $8000 in the new bank, which is `jmp ($0054)`.

Rather than recognise one game's convention, each JSR/JMP is followed symbolically from the call
site: registers start with the immediates loaded just before the call, RAM with the values it
holds in (nearly) every archived explorer state, and the bank switches when the code stores to
ROM space. After a switch, jumps through thunks (an instruction that is itself a JMP) are
followed; the first target that does real work is the far callee.
"""
from collections import Counter
from dataclasses import dataclass

from .kb import KB
from .opcodes import IMM, IND, OPCODES, REL, branch_target, operand_value

STEPS = 60              # instructions followed from a call site
SETUP = 4               # instructions before the call searched for register immediates
TYPICAL = 0.95          # a RAM byte counts as known when this share of archived states agree


@dataclass(frozen=True)
class FarCall:
    bank: int           # the caller's bank
    caller: int         # entry of the calling routine
    site: int           # the JSR/JMP that starts the call
    target_bank: int
    target: int         # first instruction doing real work in the target bank


class _Unknown(Exception):
    pass


def typical_ram(kb: KB) -> dict[int, int]:
    """RAM bytes (zero page and $0100-$07FF) that hold the same value in nearly every archived state."""
    rows = [bytes(r) for (r,) in kb.db.execute("SELECT ram FROM states WHERE ram IS NOT NULL")]
    if not rows:
        return {}
    out = {}
    for a in range(min(0x800, min(len(r) for r in rows))):
        value, n = Counter(r[a] for r in rows).most_common(1)[0]
        if n >= TYPICAL * len(rows):
            out[a] = value
    return out


def _landing(bk, target: int) -> int | None:
    """After a switch: follow thunks (JMP abs) to the code that does the work; None for JMP (ind)."""
    for _ in range(4):
        if not bk.base <= target < bk.base + len(bk.data):
            return None
        op = OPCODES.get(bk.data[target - bk.base])
        if op is None:
            return None
        if op.mnemonic != "jmp":
            return target
        if op.mode == IND:
            return None                            # dispatch through RAM: the interpreter follows it
        target = operand_value(op, bk.data, target - bk.base)
    return None


def _follow(banks: dict, bank: int, site: int, regs: dict, ram: dict) -> tuple[int, int] | None:
    """Run from the call at `site` until it lands on real code in another bank; None if it never
    switches or reaches something this small interpreter can't evaluate."""
    mem = dict(ram)
    stack: list = []
    flags: dict[str, bool] = {}
    pc, cur, switched, jumps = site, bank, False, 0

    def read(addr: int) -> int | None:
        return mem.get(addr)                        # None: unknown, fine until it decides something

    try:
        for _ in range(STEPS):
            bk = banks[cur]
            if not bk.base <= pc < bk.base + len(bk.data):
                return None
            op = OPCODES.get(bk.data[pc - bk.base])
            if op is None:
                return None
            v = operand_value(op, bk.data, pc - bk.base)
            m = op.mnemonic
            nxt = pc + op.size
            if m in ("lda", "ldx", "ldy"):
                if op.mode == IMM:
                    val = v
                elif op.mode in ("zp", "abs"):
                    val = read(v)
                else:
                    raise _Unknown
                regs[m[2]] = val
                flags = {} if val is None else {"z": val == 0, "n": val >= 0x80}
            elif m in ("sta", "stx", "sty") and op.mode in ("zp", "abs"):
                r = {"sta": "a", "stx": "x", "sty": "y"}[m]
                if r not in regs:
                    raise _Unknown
                if v >= 0x8000:                     # the bank register
                    if regs[r] is None:
                        raise _Unknown
                    cur = regs[r] % len(banks)
                    switched = True
                else:
                    mem[v] = regs[r]
            elif m in ("tax", "tay", "txa", "tya"):
                src, dst = m[1], m[2]
                if src not in regs:
                    raise _Unknown
                regs[dst] = regs[src]
                flags = {} if regs[dst] is None else {"z": regs[dst] == 0, "n": regs[dst] >= 0x80}
            elif m in ("inx", "iny", "dex", "dey"):
                r = m[2]
                if regs.get(r) is None:
                    raise _Unknown
                regs[r] = (regs[r] + (1 if m[0] == "i" else -1)) & 0xFF
                flags = {"z": regs[r] == 0, "n": regs[r] >= 0x80}
            elif m == "pha":
                stack.append(regs.get("a"))
            elif m == "pla":
                if not stack or isinstance(stack[-1], tuple):
                    raise _Unknown
                regs["a"] = stack.pop()
            elif op.mode == REL:
                f = {"beq": ("z", True), "bne": ("z", False), "bmi": ("n", True), "bpl": ("n", False)}.get(m)
                if f is None or f[0] not in flags:
                    raise _Unknown
                if flags[f[0]] == f[1]:
                    nxt = branch_target(pc, v)
            elif m in ("jsr", "jmp"):
                if not switched:
                    jumps += 1
                    if jumps > 1:                   # an ordinary call that reaches a far call further in:
                        return None                 # that far call is counted at its own site
                if op.mode == IND:
                    lo, hi = read(v), read(v + 1)
                    if lo is None or hi is None:
                        raise _Unknown
                    target = lo | hi << 8
                else:
                    target = v
                if switched:
                    land = _landing(banks[cur], target)
                    if land is not None:
                        return cur, land
                if m == "jsr":
                    stack.append(("ret", nxt))
                nxt = target
            elif m == "rts":
                return None                         # back in the caller without reaching other code
            else:
                raise _Unknown
            pc = nxt
    except _Unknown:
        return None
    return None


def _setup(bk, before: list[int]) -> dict:
    """Register immediates loaded in the few instructions before the call site."""
    regs: dict[str, int] = {}
    for pc in before:
        op = OPCODES[bk.data[pc - bk.base]]
        if op.mnemonic in ("lda", "ldx", "ldy") and op.mode == IMM:
            regs[op.mnemonic[2]] = operand_value(op, bk.data, pc - bk.base)
        elif op.mode == REL or op.mnemonic in ("jsr", "jmp", "rts"):
            regs = {}                               # another path may arrive here: start over
    return regs


def far_calls(kb: KB) -> list[FarCall]:
    """Every call in a multi-bank ROM that switches banks, and where it lands. Empty for one bank."""
    rom = kb.rom()
    banks = {b.index: b for b in rom.banks()}
    if len(banks) < 2:
        return []
    ram = typical_ram(kb)
    out = []
    for b, bk in banks.items():
        starts = sorted(a for a, k in kb.classes(b).items() if k == "op")
        spans = sorted((e, last) for e, last, _ in kb.routines(b))
        for i, site in enumerate(starts):
            op = OPCODES[bk.data[site - bk.base]]
            if op.mnemonic not in ("jsr", "jmp") or op.mode == IND:
                continue
            hit = _follow(banks, b, site, _setup(bk, starts[max(0, i - SETUP):i]), ram)
            if hit is None or hit[0] == b:
                continue
            caller = max((e for e, last in spans if e <= site <= last), default=site)
            out.append(FarCall(b, caller, site, hit[0], hit[1]))
    return out
