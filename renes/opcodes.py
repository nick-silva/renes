"""Official 6502 opcode table (151 opcodes) and addressing-mode helpers."""
from dataclasses import dataclass

IMP, ACC, IMM, ZP, ZPX, ZPY, IZX, IZY, ABS, ABX, ABY, IND, REL = (
    "imp", "acc", "imm", "zp", "zpx", "zpy", "izx", "izy", "abs", "abx", "aby", "ind", "rel")

SIZE = {IMP: 1, ACC: 1, IMM: 2, ZP: 2, ZPX: 2, ZPY: 2, IZX: 2, IZY: 2, REL: 2, ABS: 3, ABX: 3, ABY: 3, IND: 3}


@dataclass(frozen=True)
class Op:
    code: int
    mnemonic: str
    mode: str

    @property
    def size(self) -> int:
        return SIZE[self.mode]


_ALU_MODES = (IMM, ZP, ZPX, ABS, ABX, ABY, IZX, IZY)
_SHIFT_MODES = (ACC, ZP, ZPX, ABS, ABX)

_TABLE: list[tuple[str, tuple[int, ...], tuple[str, ...]]] = [
    ("adc", (0x69, 0x65, 0x75, 0x6D, 0x7D, 0x79, 0x61, 0x71), _ALU_MODES),
    ("and", (0x29, 0x25, 0x35, 0x2D, 0x3D, 0x39, 0x21, 0x31), _ALU_MODES),
    ("cmp", (0xC9, 0xC5, 0xD5, 0xCD, 0xDD, 0xD9, 0xC1, 0xD1), _ALU_MODES),
    ("eor", (0x49, 0x45, 0x55, 0x4D, 0x5D, 0x59, 0x41, 0x51), _ALU_MODES),
    ("lda", (0xA9, 0xA5, 0xB5, 0xAD, 0xBD, 0xB9, 0xA1, 0xB1), _ALU_MODES),
    ("ora", (0x09, 0x05, 0x15, 0x0D, 0x1D, 0x19, 0x01, 0x11), _ALU_MODES),
    ("sbc", (0xE9, 0xE5, 0xF5, 0xED, 0xFD, 0xF9, 0xE1, 0xF1), _ALU_MODES),
    ("sta", (0x85, 0x95, 0x8D, 0x9D, 0x99, 0x81, 0x91), (ZP, ZPX, ABS, ABX, ABY, IZX, IZY)),
    ("asl", (0x0A, 0x06, 0x16, 0x0E, 0x1E), _SHIFT_MODES),
    ("lsr", (0x4A, 0x46, 0x56, 0x4E, 0x5E), _SHIFT_MODES),
    ("rol", (0x2A, 0x26, 0x36, 0x2E, 0x3E), _SHIFT_MODES),
    ("ror", (0x6A, 0x66, 0x76, 0x6E, 0x7E), _SHIFT_MODES),
    ("dec", (0xC6, 0xD6, 0xCE, 0xDE), (ZP, ZPX, ABS, ABX)),
    ("inc", (0xE6, 0xF6, 0xEE, 0xFE), (ZP, ZPX, ABS, ABX)),
    ("ldx", (0xA2, 0xA6, 0xB6, 0xAE, 0xBE), (IMM, ZP, ZPY, ABS, ABY)),
    ("ldy", (0xA0, 0xA4, 0xB4, 0xAC, 0xBC), (IMM, ZP, ZPX, ABS, ABX)),
    ("stx", (0x86, 0x96, 0x8E), (ZP, ZPY, ABS)),
    ("sty", (0x84, 0x94, 0x8C), (ZP, ZPX, ABS)),
    ("cpx", (0xE0, 0xE4, 0xEC), (IMM, ZP, ABS)),
    ("cpy", (0xC0, 0xC4, 0xCC), (IMM, ZP, ABS)),
    ("bit", (0x24, 0x2C), (ZP, ABS)),
    ("jmp", (0x4C, 0x6C), (ABS, IND)),
    ("jsr", (0x20,), (ABS,)),
]
_REL = {"bpl": 0x10, "bmi": 0x30, "bvc": 0x50, "bvs": 0x70, "bcc": 0x90, "bcs": 0xB0, "bne": 0xD0, "beq": 0xF0}
_IMP = {
    "brk": 0x00, "php": 0x08, "clc": 0x18, "plp": 0x28, "sec": 0x38, "rti": 0x40, "pha": 0x48, "cli": 0x58,
    "rts": 0x60, "pla": 0x68, "sei": 0x78, "dey": 0x88, "txa": 0x8A, "tya": 0x98, "txs": 0x9A, "tay": 0xA8,
    "tax": 0xAA, "clv": 0xB8, "tsx": 0xBA, "iny": 0xC8, "dex": 0xCA, "cld": 0xD8, "inx": 0xE8, "nop": 0xEA,
    "sed": 0xF8,
}

OPCODES: dict[int, Op] = {}
for _mn, _codes, _modes in _TABLE:
    for _c, _m in zip(_codes, _modes, strict=True):
        OPCODES[_c] = Op(_c, _mn, _m)
for _mn, _c in _REL.items():
    OPCODES[_c] = Op(_c, _mn, REL)
for _mn, _c in _IMP.items():
    OPCODES[_c] = Op(_c, _mn, IMP)

# Instructions after which execution does not fall through.
TERMINATORS = {"rts", "rti", "jmp", "brk"}
# Modes whose operand is a 16-bit address.
ABSOLUTE_MODES = {ABS, ABX, ABY, IND}
ZEROPAGE_MODES = {ZP, ZPX, ZPY, IZX, IZY}


def operand_value(op: Op, data: bytes, offset: int) -> int | None:
    """Raw operand of the instruction at data[offset] (None for implied/accumulator)."""
    if op.size == 1:
        return None
    if op.size == 2:
        return data[offset + 1]
    return data[offset + 1] | (data[offset + 2] << 8)


def branch_target(pc: int, rel: int) -> int:
    return (pc + 2 + (rel - 256 if rel & 0x80 else rel)) & 0xFFFF
