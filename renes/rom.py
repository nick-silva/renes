"""iNES / NES 2.0 ROM loading."""
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

HEADER_SIZE = 16
PRG_UNIT = 0x4000
CHR_UNIT = 0x2000
MAPPERS = {0: "NROM", 7: "AxROM"}      # supported: how PRG appears in CPU space
AXROM_BANK = 0x8000                    # AxROM swaps all 32 KB at $8000-$FFFF at once


class RomError(ValueError):
    pass


@dataclass(frozen=True)
class Bank:
    index: int        # the KB's bank number for its addresses
    base: int         # CPU address of its first byte while it is switched in
    data: bytes
    offset: int       # where it starts in PRG (CDL and other per-PRG-byte data)


@dataclass(frozen=True)
class Rom:
    name: str
    header: bytes
    prg: bytes
    chr: bytes
    mapper: int
    mirroring: str
    sha1: str

    @property
    def prg_base(self) -> int:
        """CPU address of the first PRG byte for NROM (16 KB is mirrored at $C000)."""
        return 0x10000 - len(self.prg)

    @property
    def raw(self) -> bytes:
        return self.header + self.prg + self.chr

    def banks(self) -> list[Bank]:
        """PRG as the CPU sees it: NROM is one bank; AxROM is 32 KB banks, each at $8000."""
        if self.mapper == 7:
            return [Bank(i, AXROM_BANK, self.prg[o:o + 0x8000], o)
                    for i, o in enumerate(range(0, len(self.prg), 0x8000))]
        return [Bank(0, self.prg_base, self.prg, 0)]

    def bank_rom(self, bank: Bank) -> "Rom":
        """This ROM as if it only had the one bank (its prg_base is then the bank's base)."""
        return replace(self, prg=bank.data)


def parse(data: bytes, name: str = "rom") -> Rom:
    if len(data) < HEADER_SIZE or data[:4] != b"NES\x1a":
        raise RomError("not an iNES file (missing NES<EOF> magic)")
    header = data[:HEADER_SIZE]
    prg_size = header[4] * PRG_UNIT
    chr_size = header[5] * CHR_UNIT
    flags6, flags7 = header[6], header[7]
    if flags6 & 0x04:
        raise RomError("ROMs with a 512-byte trainer are not supported")
    mapper = (flags6 >> 4) | (flags7 & 0xF0)
    expected = HEADER_SIZE + prg_size + chr_size
    if len(data) != expected:
        raise RomError(f"size mismatch: header implies {expected} bytes, file has {len(data)}")
    prg = data[HEADER_SIZE:HEADER_SIZE + prg_size]
    chr_ = data[HEADER_SIZE + prg_size:]
    mirroring = "four-screen" if flags6 & 0x08 else ("vertical" if flags6 & 0x01 else "horizontal")
    return Rom(name, header, prg, chr_, mapper, mirroring, hashlib.sha1(data).hexdigest())


def load(path: Path) -> Rom:
    return parse(path.read_bytes(), path.name)
