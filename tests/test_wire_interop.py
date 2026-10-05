import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent8s.desktop.remote_crypto import C2H, H2C, b64url, derive, open_frame, seal

ROOT = Path(__file__).resolve().parent.parent
KEY = bytes(range(32))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_python_and_browser_crypto_agree_in_both_directions():
    room, enc = derive(KEY)
    small = {"k": "welcome", "cn": "abc", "sn": "def", "note": "привет"}
    big = {"k": "ev", "e": [{"t": "msg_ops", "text": "очень длинный текст " * 3000}]}
    payload = {
        "secret": b64url(KEY),
        "frames": {"small": seal(enc, room, H2C, small), "big": seal(enc, room, H2C, big)},
    }
    proc = subprocess.run(
        ["node", str(ROOT / "tests/interop/wire_interop.mjs")],
        input=json.dumps(payload), capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["room"] == room  # HKDF derivation matches
    assert out["opened"] == {"small": small, "big": big}  # Python -> browser, incl. zlib-compressed
    assert out["reflected"] is None  # direction is bound into the AAD
    assert open_frame(enc, room, C2H, out["sealed"]["small"]) == {"k": "req", "id": 1, "p": "/api/bootstrap", "note": "привет"}
    big_back = open_frame(enc, room, C2H, out["sealed"]["big"])  # browser -> Python, incl. compressed
    assert big_back["b"]["text"].startswith("длинный текст")
