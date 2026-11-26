"""Minimal PNG reading and writing (8-bit RGB/RGBA, no interlace), standard library only.

Enough for the emulator's screenshots and the images renes draws itself (tile sheets, crops of
what changed on screen). Images are (width, height, rows) with each row a bytes of RGB triples.
"""
import struct
import zlib

Image = tuple[int, int, list[bytes]]


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def decode(data: bytes) -> Image:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    w, h, depth, color, _, _, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, color type {color}, interlace {interlace})")
    bpp = 3 if color == 2 else 4
    raw, stride, prev, rows = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        ftype, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        prev = line
        rows.append(bytes(line) if bpp == 3 else bytes(v for k, v in enumerate(line) if k % 4 != 3))
    return w, h, rows


def encode(img: Image) -> bytes:
    w, h, rows = img

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def crop(img: Image, x0: int, y0: int, x1: int, y1: int) -> Image:
    _, _, rows = img
    return x1 - x0, y1 - y0, [r[x0 * 3:x1 * 3] for r in rows[y0:y1]]


def scale(img: Image, k: int) -> Image:
    w, h, rows = img
    out = []
    for r in rows:
        line = b"".join(r[i:i + 3] * k for i in range(0, len(r), 3))
        out += [line] * k
    return w * k, h * k, out


def diff_box(a: Image, b: Image) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the pixels that differ between two same-sized images."""
    w, h, ra = a
    _, _, rb = b
    ys = [y for y in range(h) if ra[y] != rb[y]]
    if not ys:
        return None
    xs = [x for y in ys for x in range(w) if ra[y][x * 3:x * 3 + 3] != rb[y][x * 3:x * 3 + 3]]
    return min(xs), ys[0], max(xs) + 1, ys[-1] + 1


def blank(w: int, h: int, rgb: tuple[int, int, int] = (0, 0, 0)) -> Image:
    return w, h, [bytes(rgb) * w for _ in range(h)]
