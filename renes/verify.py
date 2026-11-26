"""Assemble rendered output with cc65 and byte-compare it with the original ROM."""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .render import CHR_FILE, Rendered

ROOT = Path(__file__).resolve().parent.parent
CC65_BIN = Path(os.environ.get("RENES_CC65_BIN", ROOT / "tools" / "cc65" / "bin"))


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    rom_path: Path
    identical: bool
    first_diff: int | None
    size: int


def write(rendered: Rendered, out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    asm, cfg = out_dir / f"{stem}.asm", out_dir / f"{stem}.cfg"
    asm.write_text(rendered.asm, encoding="utf-8")
    cfg.write_text(rendered.cfg, encoding="utf-8")
    (out_dir / CHR_FILE).write_bytes(rendered.chr)
    return asm, cfg


def assemble(asm: Path, cfg: Path) -> Path:
    obj, nes = asm.with_suffix(".o"), asm.with_suffix(".nes")
    for cmd in ([str(CC65_BIN / "ca65"), asm.name, "-o", obj.name],
                [str(CC65_BIN / "ld65"), "-C", cfg.name, obj.name, "-o", nes.name]):
        proc = subprocess.run(cmd, cwd=asm.parent, capture_output=True, text=True)
        if proc.returncode != 0:
            raise BuildError(f"{Path(cmd[0]).name} failed:\n{proc.stdout}{proc.stderr}")
    return nes


def compare(built: bytes, original: bytes) -> int | None:
    for i, (x, y) in enumerate(zip(built, original)):
        if x != y:
            return i
    return None if len(built) == len(original) else min(len(built), len(original))


def build_and_verify(rendered: Rendered, original: bytes, out_dir: Path, stem: str) -> Result:
    asm, cfg = write(rendered, out_dir, stem)
    nes = assemble(asm, cfg)
    built = nes.read_bytes()
    diff = compare(built, original)
    return Result(nes, diff is None, diff, len(built))
