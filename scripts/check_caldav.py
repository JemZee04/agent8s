#!/usr/bin/env python3
"""Standalone check: is the Yandex CalDAV app password active yet?

Reads YANDEX_CALDAV_URL / YANDEX_CALDAV_LOGIN / YANDEX_CALDAV_PASSWORD from
.env and does a bare PROPFIND — no caldav library, no calendar parsing, just
"does auth succeed". Safe to re-run as often as you like.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    url = os.environ.get("YANDEX_CALDAV_URL", "").strip()
    login = os.environ.get("YANDEX_CALDAV_LOGIN", "").strip()
    password = os.environ.get("YANDEX_CALDAV_PASSWORD", "").strip()

    if not (url and login and password):
        print("YANDEX_CALDAV_URL / YANDEX_CALDAV_LOGIN / YANDEX_CALDAV_PASSWORD not all set in .env")
        return 2

    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    req = urllib.request.Request(url, method="PROPFIND", headers={"Authorization": f"Basic {creds}", "Depth": "0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"OK — CalDAV auth works (HTTP {resp.status}).")
            return 0
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Still 401 Unauthorized — password not active yet (or wrong), try again later.")
        else:
            print(f"HTTP {e.code} {e.reason} — auth may be fine, but check the URL.")
        return 1
    except urllib.error.URLError as e:
        print(f"Network error: {e.reason}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
