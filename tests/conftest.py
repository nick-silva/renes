import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from renes.verify import CC65_BIN  # noqa: E402

from renes.emu.mesen import MESEN  # noqa: E402

ROMS = ROOT / "roms"

# The integration tests drive a real game in the emulator. No ROM ships with renes, so they read
# a small description of a ROM you supply: tests/example_rom.json (git-ignored), or the file
# named by RENES_TEST_CONFIG. tests/example_rom.sample.json explains every field. Without the
# file (or without Mesen) these tests are skipped.
_CONFIG_PATH = Path(os.environ.get("RENES_TEST_CONFIG", Path(__file__).with_name("example_rom.json")))
_CONFIG: dict = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")) if _CONFIG_PATH.exists() else {}
EXAMPLE_ROM = Path(_CONFIG.get("rom", ROMS / "example.nes"))
if not EXAMPLE_ROM.is_absolute():
    EXAMPLE_ROM = ROOT / EXAMPLE_ROM
# Inputs that take the example game from power-on into play: [[frames, buttons], ...]
EXAMPLE_SETUP: list = _CONFIG.get("setup", [[60, "none"], [4, "start"], [240, "none"]])

needs_mesen = pytest.mark.skipif(not MESEN.exists() or not EXAMPLE_ROM.exists(),
                                 reason="Mesen or the example ROM is missing (see tests/example_rom.sample.json)")


def rom_fact(name: str) -> int:
    """An address the tests need to know about the example ROM; skips the test when it isn't configured."""
    value = _CONFIG.get(name)
    if value is None:
        pytest.skip(f"'{name}' is not set in {_CONFIG_PATH.name}")
    return int(value.lstrip("$"), 16) if isinstance(value, str) else int(value)


def make_nes(prg: bytes, chr_: bytes = b"\x00" * 0x2000, mapper: int = 0) -> bytes:
    assert len(prg) % 0x4000 == 0 and len(chr_) % 0x2000 == 0
    header = bytes([0x4E, 0x45, 0x53, 0x1A, len(prg) // 0x4000, len(chr_) // 0x2000,
                    0x01 | ((mapper & 0x0F) << 4), mapper & 0xF0]) + bytes(8)
    return header + prg + chr_


def make_prg(code: dict[int, bytes], reset: int, nmi: int | None = None, irq: int | None = None,
             size: int = 0x8000, fill: int = 0xFF) -> bytes:
    """Build a PRG image: `code` maps CPU addresses to byte strings."""
    base = 0x10000 - size
    prg = bytearray([fill] * size)
    for addr, blob in code.items():
        prg[addr - base:addr - base + len(blob)] = blob
    for vec, target in ((0xFFFA, nmi or reset), (0xFFFC, reset), (0xFFFE, irq or reset)):
        prg[vec - base] = target & 0xFF
        prg[vec - base + 1] = target >> 8
    return bytes(prg)


needs_cc65 = pytest.mark.skipif(shutil.which(str(CC65_BIN / "ca65")) is None and
                                not (CC65_BIN / "ca65.exe").exists(), reason="cc65 not installed")


def real_roms() -> list[Path]:
    return sorted(ROMS.glob("*.nes")) if ROMS.exists() else []
