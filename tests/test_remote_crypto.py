import json
import os
import zlib

from agent8s.desktop.remote_crypto import (
    C2H, H2C, b64url, b64url_decode, derive, new_key, open_frame, seal,
)

KEY = bytes(range(32))


def test_derivation_is_deterministic_and_separated():
    room, enc = derive(KEY)
    assert derive(KEY) == (room, enc)
    assert len(room) == 32 and len(enc) == 32
    assert derive(bytes(32))[0] != room
    assert room != enc.hex()[:32]  # room id must not reveal the encryption key


def test_roundtrip_and_flags():
    room, enc = derive(KEY)
    small = {"k": "req", "n": 1}
    assert open_frame(enc, room, C2H, seal(enc, room, C2H, small)) == small
    big = {"k": "res", "b": {"text": "привет " * 5000}}
    frame = seal(enc, room, H2C, big)
    assert len(frame) < len(json.dumps(big, ensure_ascii=False)) // 2  # compressed before encrypting
    assert open_frame(enc, room, H2C, frame) == big


def test_forgery_tampering_reflection_and_cross_room_are_rejected():
    room, enc = derive(KEY)
    other_room, other_enc = derive(new_key())
    frame = seal(enc, room, C2H, {"k": "hello", "cn": "x"})

    assert open_frame(other_enc, room, C2H, frame) is None  # wrong key
    assert open_frame(enc, room, H2C, frame) is None  # reflected in the other direction
    assert open_frame(enc, other_room, C2H, frame) is None  # moved to another room
    raw = bytearray(b64url_decode(frame))
    raw[-1] ^= 1
    assert open_frame(enc, room, C2H, b64url(bytes(raw))) is None  # bit flip
    for junk in ("", "abc", "!!!", b64url(os.urandom(5)), b64url(os.urandom(64))):
        assert open_frame(enc, room, C2H, junk) is None


def test_decompression_bomb_is_rejected():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    room, enc = derive(KEY)
    bomb = zlib.compress(b"0" * (40 * 1024 * 1024))  # 40 MB of zeros in a few KB
    nonce = os.urandom(12)
    aad = f"agent8s/v1|{C2H}|{room}".encode()
    frame = b64url(nonce + AESGCM(enc).encrypt(nonce, b"\x01" + bomb, aad))
    assert open_frame(enc, room, C2H, frame) is None


def test_known_vector_for_the_browser_implementation():
    # tests/crypto_interop.mjs asserts the very same values from WebCrypto.
    room, enc = derive(KEY)
    assert room == "044c9a62234aeb8b45b9190e21c46920"
    assert enc.hex() == "690824f57ce58c08fb68015f9384ad7ccd1992f6f7513ed62d6425eea578f22c"
