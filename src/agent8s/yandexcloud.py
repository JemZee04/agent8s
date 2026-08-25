from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt

from .config import Config

IAM_TOKEN_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
BILLING_ACCOUNT_URL = "https://billing.api.cloud.yandex.net/billing/v1/billingAccounts/{id}"
TIMEOUT_SECONDS = 15
# Refresh a bit before the real ~12h expiry so a slow request never hands
# back an already-expired token.
IAM_TOKEN_SAFETY_MARGIN = timedelta(minutes=30)


class YandexCloudError(RuntimeError):
    pass


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


_cache: Optional[_CachedToken] = None


def _build_signed_jwt(key_file: Path) -> str:
    try:
        data = json.loads(key_file.read_text())
    except OSError as e:
        raise YandexCloudError(f"can't read service account key file {key_file}: {e}") from e
    except json.JSONDecodeError as e:
        raise YandexCloudError(f"service account key file {key_file} is not valid JSON: {e}") from e

    now = int(time.time())
    payload = {
        "aud": IAM_TOKEN_URL,
        "iss": data["service_account_id"],
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, data["private_key"], algorithm="PS256", headers={"kid": data["id"]})


def _exchange_for_iam_token(signed_jwt: str) -> _CachedToken:
    req = urllib.request.Request(
        IAM_TOKEN_URL,
        data=json.dumps({"jwt": signed_jwt}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise YandexCloudError(f"IAM token exchange failed: HTTP {e.code} {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise YandexCloudError(f"IAM token exchange failed: {e.reason}") from e

    token = data.get("iamToken") or data.get("iam_token")
    if not token:
        raise YandexCloudError(f"IAM token exchange returned no token: {data}")
    # Response includes expiresAt, but the JWT itself is only valid ~1h and
    # exchanges happen well within that — safe to just trust our own exp.
    return _CachedToken(token=token, expires_at=datetime.now(timezone.utc) + timedelta(hours=11))


def _get_iam_token(config: Config) -> str:
    global _cache
    if _cache is not None and datetime.now(timezone.utc) < _cache.expires_at - IAM_TOKEN_SAFETY_MARGIN:
        return _cache.token

    signed_jwt = _build_signed_jwt(config.yc_service_account_key_file)
    _cache = _exchange_for_iam_token(signed_jwt)
    return _cache.token


def get_balance(config: Config) -> float:
    if not config.yc_configured:
        raise YandexCloudError("Yandex Cloud is not configured (YC_SERVICE_ACCOUNT_KEY_FILE / YC_BILLING_ACCOUNT_ID)")

    iam_token = _get_iam_token(config)
    url = BILLING_ACCOUNT_URL.format(id=config.yc_billing_account_id)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {iam_token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise YandexCloudError(f"billing account lookup failed: HTTP {e.code} {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise YandexCloudError(f"billing account lookup failed: {e.reason}") from e

    try:
        return float(data["balance"])
    except (KeyError, TypeError, ValueError) as e:
        raise YandexCloudError(f"unexpected billing account response: {data}") from e
