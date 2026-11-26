"""Python side of the renes bridge: launches headless Mesen and drives it over TCP.

See bridge.lua for the line protocol.
"""
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MESEN = Path(os.environ.get("RENES_MESEN", ROOT / "tools" / "mesen" / "Mesen.exe"))
BRIDGE_LUA = Path(__file__).resolve().parent / "bridge.lua"


class MesenError(RuntimeError):
    pass


def _ensure_controllers() -> None:
    """Plug standard controllers into both NES ports in Mesen's settings.

    Mesen otherwise takes the input devices from its game database, by checksum: a ROM it doesn't
    know (a patched game, another dump) ran with no controller, so no button reached it."""
    import json

    path = MESEN.parent / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return
    nes = settings.setdefault("Nes", {})
    if all(nes.get(p, {}).get("Type") == "NesController" for p in ("Port1", "Port2")):
        return
    for port in ("Port1", "Port2"):
        nes.setdefault(port, {})["Type"] = "NesController"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class Mesen:
    """One headless Mesen process. port=0 lets the OS pick a free port (parallel runs)."""

    def __init__(self, rom: Path, port: int = 0, timeout: float = 30.0):
        _ensure_controllers()
        self._server = socket.create_server(("127.0.0.1", port))
        self._server.settimeout(timeout)
        port = self._server.getsockname()[1]
        env = dict(os.environ, RENES_PORT=str(port))
        self.proc = subprocess.Popen(
            [str(MESEN), "--testRunner", "--timeout=86400", str(BRIDGE_LUA), str(rom)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._conn, _ = self._server.accept()
        self._conn.settimeout(timeout)
        self._buf = self._conn.makefile("rb")
        hello = self._readline()
        if not hello.startswith("hello"):
            raise MesenError(f"unexpected greeting: {hello!r}")

    def _readline(self) -> str:
        line = self._buf.readline()
        if not line:
            raise MesenError(f"connection closed (mesen exit={self.proc.poll()})")
        return line.decode("ascii").rstrip("\n")

    def cmd(self, line: str) -> str:
        self._conn.sendall((line + "\n").encode("ascii"))
        reply = self._readline()
        if not reply.startswith("ok"):
            raise MesenError(f"{line!r} -> {reply!r}")
        return reply[3:]

    def step(self, frames: int = 1, buttons: str = "none") -> int:
        return int(self.cmd(f"step {frames} {buttons}").split("=")[1])

    def watch_hits(self, addrs: list[int], cap: int = 400, memtype: str = "nesMemory") -> None:
        """Record the frames each address executes, up to `cap` frames each (see hit_frames).
        Calls add to the watched set; clear_hits() starts over. With memtype="nesPrgRom" the
        addresses are PRG offsets, so only one bank's code counts (banked mappers)."""
        for i in range(0, len(addrs), 400):        # keep command lines short
            self.cmd("hits " + ",".join(str(a) for a in addrs[i:i + 400]) + f" {cap} {memtype}")

    def clear_hits(self) -> None:
        self.cmd("hitsclear")

    def hit_frames(self) -> dict[int, list[int]]:
        reply = self.cmd("hitlog").strip()
        out = {}
        for part in filter(None, reply.split(",")):
            addr, frames = part.split(":")
            out[int(addr, 16)] = [int(f) for f in frames.split(";")]
        return out

    def trace(self, start: int, end: int, max_lines: int = 300, memtype: str = "nesMemory",
              cpu_start: int | None = None) -> None:
        """Arm a register trace for the next call that enters `start`, within [start, end]
        (PRG offsets with memtype="nesPrgRom", then cpu_start is the entry's CPU address; the rows
        give CPU addresses)."""
        self.cmd(f"trace {start} {end} {max_lines} {memtype} {start if cpu_start is None else cpu_start}")

    def trace_log(self) -> tuple[bool, list[dict]]:
        """(finished, rows): registers before each traced instruction."""
        reply = self.cmd("tracelog").strip()
        state, _, body = reply.partition(" ")
        rows = []
        for p in filter(None, body.split(",")):
            pc, a, x, y, sp, ps, frame = p.split(":")
            rows.append({"pc": int(pc, 16), "a": int(a, 16), "x": int(x, 16), "y": int(y, 16),
                         "sp": int(sp, 16), "p": int(ps, 16), "frame": int(frame)})
        return state == "done", rows

    def write_prg(self, addr: int, data: bytes, prg_size: int) -> None:
        """Patch PRG-ROM at CPU address addr (NROM: 16 KB images are mirrored)."""
        self.write((addr - 0x8000) % prg_size, data, memtype="nesPrgRom")

    def reset(self) -> None:
        """Soft reset (the console's Reset button); takes effect on the next step."""
        self.cmd("reset")

    def read(self, addr: int, length: int = 1, memtype: str = "nesDebug") -> bytes:
        return bytes.fromhex(self.cmd(f"read {addr} {length} {memtype}"))

    def write(self, addr: int, data: bytes, memtype: str = "nesDebug") -> None:
        self.cmd(f"write {addr} {data.hex()} {memtype}")

    def ram(self) -> bytes:
        return self.read(0x0000, 0x800, "nesInternalRam")

    def save(self, slot: str) -> int:
        return int(self.cmd(f"save {slot}").split("=")[1])

    def load(self, slot: str) -> None:
        self.cmd(f"load {slot}")

    def save_bytes(self) -> bytes:
        return bytes.fromhex(self.cmd("savex"))

    def load_bytes(self, state: bytes) -> None:
        self.cmd(f"loadx {state.hex()}")

    def cdl(self) -> bytes:
        return bytes.fromhex(self.cmd("cdl"))

    def cdl_stats(self) -> tuple[int, int, int, int]:
        """(code, data, jump-target, subroutine-entry) PRG byte counts."""
        code, data, jump, sub = (int(x) for x in self.cmd("cdlstat").split())
        return code, data, jump, sub

    def counters(self, memtype: str = "nesInternalRam", kind: str = "writeCount") -> list[int]:
        return [int(x) for x in self.cmd(f"counters {memtype} {kind}").split(",")]

    def freeze(self, addr: int, value: int) -> None:
        self.cmd(f"freeze {addr} {value}")

    def unfreeze(self, addr: int) -> None:
        self.cmd(f"unfreeze {addr}")

    def poke_at_pc(self, pc: int, addr: int, value: int) -> None:
        """Write value to addr every time the instruction at pc is about to run."""
        self.cmd(f"pokepc {pc} {addr} {value}")

    def clear_pc_pokes(self) -> None:
        self.cmd("pokepcclear")

    def log_apu(self) -> None:
        """Start (or restart) recording CPU writes to the APU registers $4000-$4017."""
        self.cmd("apulog")

    def apu_writes(self) -> list[tuple[int, int, int]]:
        """(frame, register, value) for each APU write since log_apu, in order. $4014 (sprite DMA)
        and $4016 (controller strobe) share the range but are not sound, so they are left out."""
        out = []
        for item in filter(None, self.cmd("apuwrites").split(",")):
            f, addr, value = item.split(":")
            if int(addr, 16) not in (0x4014, 0x4016):
                out.append((int(f), int(addr, 16), int(value, 16)))
        return out

    def log_writes(self, start: int, end: int) -> None:
        self.cmd(f"logwrites {start} {end}")

    def writers(self) -> list[tuple[int, int, int]]:
        body = self.cmd("writers")
        out = []
        for item in filter(None, body.split(",")):
            a, pc, n = item.split(":")
            out.append((int(a, 16), int(pc, 16), int(n)))
        return out

    def state(self) -> dict[str, float]:
        return {k: float(v) for k, v in (kv.split("=") for kv in self.cmd("state").split())}

    def screenshot_bytes(self) -> bytes:
        """PNG of the current frame."""
        return bytes.fromhex(self.cmd("shot"))

    def screenshot(self, path: Path) -> None:
        path.write_bytes(self.screenshot_bytes())

    def __enter__(self) -> "Mesen":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self, code: int = 0) -> int:
        try:
            self.cmd(f"quit {code}")
        except MesenError:
            pass
        self._conn.close()
        self._server.close()
        try:
            return self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=10)
