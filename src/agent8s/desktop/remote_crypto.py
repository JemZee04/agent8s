"""End-to-end encryption for the phone <-> Mac channel through the relay.

One 32-byte pairing secret K lives on the Mac and in the phone (scanned from a
QR code). Everything is derived from it with HKDF-SHA256 (empty salt, so the
browser's WebCrypto produces identical bytes):

    room_id = HKDF(K, "agent8s/room/v1", 16 bytes)  -> 32 hex, the relay's rendezvous capability
    enc_key = HKDF(K, "agent8s/enc/v1", 32 bytes)   -> AES-256-GCM key

A frame is base64url(nonce12 || AES-GCM(plaintext)). The associated data binds
the direction and the room, so a frame cannot be reflected back at its sender
or moved to another room. Plaintext = 1 flag byte (1 = zlib-compressed) + JSON.
Compression happens *before* encryption (ciphertext is incompressible), which
matters for large chat snapshots over mobile networks.
"""
from __future__ import annotations

import base64
import json
import os
import zlib
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

C2H = "c2h"
H2C = "h2c"
COMPRESS_OVER = 1024
MAX_PLAINTEXT = 16 * 1024 * 1024


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key() -> bytes:
    return os.urandom(32)


def _hkdf(key: bytes, info: str, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info.encode()).derive(key)


def derive(key: bytes) -> tuple[str, bytes]:
    """(room_id, enc_key) for a pairing secret."""
    if len(key) != 32:
        raise ValueError("pairing key must be 32 bytes")
    return _hkdf(key, "agent8s/room/v1", 16).hex(), _hkdf(key, "agent8s/enc/v1", 32)


def _aad(direction: str, room_id: str) -> bytes:
    return f"agent8s/v1|{direction}|{room_id}".encode()


def seal(enc_key: bytes, room_id: str, direction: str, obj: Any) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    flag = b"\x00"
    if len(raw) > COMPRESS_OVER:
        packed = zlib.compress(raw, 6)
        if len(packed) < len(raw):
            raw, flag = packed, b"\x01"
    nonce = os.urandom(12)
    return b64url(nonce + AESGCM(enc_key).encrypt(nonce, flag + raw, _aad(direction, room_id)))


def open_frame(enc_key: bytes, room_id: str, direction: str, frame: str) -> Optional[Any]:
    """Decrypt and parse a frame; None for anything forged, corrupt or oversized."""
    try:
        blob = b64url_decode(frame)
        if len(blob) < 12 + 16 + 1:
            return None
        plain = AESGCM(enc_key).decrypt(blob[:12], blob[12:], _aad(direction, room_id))
        flag, body = plain[:1], plain[1:]
        if flag == b"\x01":
            inflater = zlib.decompressobj()
            body = inflater.decompress(body, MAX_PLAINTEXT)
            if inflater.unconsumed_tail:  # would exceed the cap: a decompression bomb
                return None
        elif flag != b"\x00":
            return None
        return json.loads(body)
    except (InvalidTag, ValueError, zlib.error):
        return None
