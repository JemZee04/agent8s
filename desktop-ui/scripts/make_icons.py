"""Generates the PWA icons (pure Python, no dependencies): python3 scripts/make_icons.py"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "public"
SS = 3  # supersampling per axis


def png(width: int, height: int, rgb: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rgb)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def rounded_rect(px, py, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = abs(px - cx) - ((x1 - x0) / 2 - r), abs(py - cy) - ((y1 - y0) / 2 - r)
    return math.hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - r <= 0


def in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def render(size: int) -> bytes:
    u = size / 512
    top, bottom = (79, 70, 229), (124, 123, 245)  # indigo, matching --accent
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    px, py = (x + (sx + 0.5) / SS) / u, (y + (sy + 0.5) / SS) / u
                    t = (px + py) / 1024
                    colour = tuple(top[i] + (bottom[i] - top[i]) * t for i in range(3))
                    bubble = rounded_rect(px, py, 106, 132, 406, 340, 52) or in_triangle(
                        px, py, (150, 320), (150, 410), (236, 336))
                    if bubble:
                        colour = (255, 255, 255)
                        for cx in (196, 256, 316):
                            if math.hypot(px - cx, py - 236) <= 21:
                                colour = (79, 70, 229)
                    for i in range(3):
                        acc[i] += colour[i]
            row.extend(int(c / (SS * SS)) for c in acc)
        rows.append(bytes(row))
    return png(size, size, rows)


for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
    (OUT / name).write_bytes(render(size))
    print("wrote", name)
