"""M0 spikes S2-S5: socket bridge, command set, savestate round trip, freeze, throughput.

The addresses below describe the ROM the spike is run on. They are only used to *check* the
harness, not as pipeline input: set them for your own ROM before running.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from renes.emu.mesen import Mesen  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ROM = ROOT / "roms" / "example.nes"
GAME_MODE = 0x0000        # a byte that reads PLAYING once Start has left the title screen
PLAYING = 1
PLAYER_X = 0x0000         # the player's X position, low byte...
PLAYER_X_PAGE = 0x0000    # ...and high byte (screen page)
TIMER_DIGITS = 0x0000     # three bytes of an on-screen timer; the last one is frozen in S5
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")


def player_x(m: Mesen) -> int:
    page, x = m.read(PLAYER_X_PAGE)[0], m.read(PLAYER_X)[0]
    return page * 256 + x


def main() -> int:
    m = Mesen(ROM)

    # S2: connection
    check("S2 ping", m.cmd("ping") == "pong")

    # S3: get from title screen into gameplay
    m.step(60)
    m.step(4, "start")
    m.step(240)
    mode = m.read(GAME_MODE)[0]
    check("S3 reached gameplay", mode == PLAYING, f"mode={mode}")
    m.screenshot(OUT / "s3_start.png")
    m.save("start")

    x0 = player_x(m)
    m.step(120, "right+b")
    x1 = player_x(m)
    check("S3 input moves the player", x1 > x0, f"x {x0} -> {x1}")

    # S3: savestate round trip
    size = m.save("a")
    ram_a = m.ram()
    m.step(90, "right+a")
    ram_b = m.ram()
    m.load("a")
    ram_c = m.ram()
    check("S3 savestate restores RAM", ram_a == ram_c and ram_a != ram_b,
          f"state={size}B, diff-after-play={sum(p != q for p, q in zip(ram_a, ram_b))} bytes")

    # Determinism: replaying the same input from the same state gives identical RAM
    m.step(90, "right+a")
    ram_d = m.ram()
    check("S3 deterministic replay", ram_d == ram_b)
    m.load("a")

    # CDL and access counters
    cdl = m.cdl()
    code = sum(1 for b in cdl if b & 0x01)
    data = sum(1 for b in cdl if b & 0x02)
    check("S3 CDL readable", len(cdl) == 0x8000,
          f"PRG={len(cdl)}B code={code} ({code / len(cdl):.1%}) data={data} ({data / len(cdl):.1%})")
    (OUT / "example.cdl").write_bytes(cdl)

    writes = m.counters("nesInternalRam", "writeCount")
    check("S3 access counters", len(writes) == 0x800,
          f"RAM bytes ever written={sum(1 for w in writes if w)}")

    # S5: freeze the timer's ones digit; it must stop changing, then resume after unfreeze.
    # Run from level start: standing still elsewhere can get the player killed, which stops the timer.
    m.load("start")
    m.freeze(TIMER_DIGITS + 2, 0x07)
    m.step(120)
    fd = m.read(TIMER_DIGITS, 3)
    m.screenshot(OUT / "s5_frozen.png")
    m.step(120)
    fd2 = m.read(TIMER_DIGITS, 3)
    m.unfreeze(TIMER_DIGITS + 2)
    m.step(120)
    ud = m.read(TIMER_DIGITS, 3)
    check("S5 freeze pins a variable", fd[2] == 7 and fd2[2] == 7 and ud != fd2,
          f"timer while frozen={fd.hex()},{fd2.hex()} after unfreeze={ud.hex()}")
    m.load("a")

    # S4: throughput
    t = time.perf_counter(); m.step(600); dt = time.perf_counter() - t
    check("S4 bulk stepping", True, f"{600 / dt:.0f} fps (1 command / 600 frames)")

    t = time.perf_counter()
    for _ in range(300):
        m.step(1, "right")
    dt = time.perf_counter() - t
    check("S4 per-frame control", True, f"{300 / dt:.0f} fps (1 command / frame)")

    t = time.perf_counter()
    for _ in range(300):
        m.step(1, "right")
        m.ram()
    dt = time.perf_counter() - t
    check("S4 per-frame control + 2KB RAM read", True, f"{300 / dt:.0f} fps")

    for label, lo, hi in (("player X only", PLAYER_X, PLAYER_X), ("$0700-$07FF", 0x0700, 0x07FF)):
        m.load("a")
        m.log_writes(lo, hi)
        t = time.perf_counter(); m.step(120, "right+b"); dt = time.perf_counter() - t
        writers = m.writers()
        m.cmd("logwrites 0 0")  # replace with a 1-byte range (cheap) for the next pass
        check(f"S4 write-log {label}", True,
              f"{120 / dt:.1f} fps; {len(writers)} (addr,pc) pairs, {len({pc for _, pc, _ in writers})} writer PCs")
        if lo == PLAYER_X:
            print("      writers of player X:", sorted({hex(pc) for a, pc, _ in writers}))

    code = m.close()
    check("S2 clean shutdown", code == 0, f"exit={code}")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
