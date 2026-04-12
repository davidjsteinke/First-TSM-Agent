#!/usr/bin/env python3
"""
Blizzard Game Data API — item name lookup with local cache.

Authenticates via client-credentials OAuth2 (no user login required).
Credentials are read from ~/.env or environment variables.

Public interface:
    get_item_name(item_id: int) -> str
        Returns the item name, e.g. "Algari Competitor's Helm".
        Falls back to "Item {item_id}" on any error.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENV_FILE   = Path.home() / ".env"
CACHE_FILE = Path.home() / "item_names.json"

OAUTH_URL  = "https://oauth.battle.net/token"
# US region; item names are region-agnostic for en_US
API_BASE   = "https://us.api.blizzard.com"
NAMESPACE  = "static-us"
LOCALE     = "en_US"

# Token refresh buffer: renew 60 s before actual expiry
TOKEN_BUFFER = 60


# ---------------------------------------------------------------------------
# .env loader (no external deps)
# ---------------------------------------------------------------------------

def _load_env(path: Path) -> None:
    """Parse a simple KEY=VALUE .env file into os.environ (skip comments/blanks)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env(ENV_FILE)


# ---------------------------------------------------------------------------
# OAuth2 token (module-level cache — lives for the process lifetime)
# ---------------------------------------------------------------------------

_token: str | None = None
_token_expires_at: float = 0.0


def _get_token() -> str:
    global _token, _token_expires_at

    if _token and time.time() < _token_expires_at:
        return _token

    client_id     = os.environ.get("BLIZZARD_CLIENT_ID", "")
    client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET must be set "
            "(in ~/.env or environment variables)"
        )

    credentials = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    _token = data["access_token"]
    _token_expires_at = time.time() + data.get("expires_in", 86400) - TOKEN_BUFFER
    return _token


# ---------------------------------------------------------------------------
# Local name cache (persisted to item_names.json)
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# Module-level cache loaded once per process
_cache: dict[str, str] = _load_cache()


# ---------------------------------------------------------------------------
# API lookup
# ---------------------------------------------------------------------------

def _fetch_item_name(item_id: int, token: str) -> str:
    """Hit the Blizzard item API and return the en_US name."""
    url = (
        f"{API_BASE}/data/wow/item/{item_id}"
        f"?namespace={NAMESPACE}&locale={LOCALE}"
    )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data["name"]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_item_name(item_id: int) -> str:
    """
    Return the WoW item name for item_id.
    Results are cached in item_names.json.
    Falls back to 'Item {item_id}' if the lookup fails for any reason.
    """
    key = str(item_id)

    if key in _cache:
        return _cache[key]

    try:
        token = _get_token()
        name  = _fetch_item_name(item_id, token)
    except Exception as exc:
        # Non-fatal: bad IDs (bonus-ID variants, test items) return 404
        print(f"  [blizzard_api] lookup failed for item {item_id}: {exc}")
        name = f"Item {item_id}"

    _cache[key] = name
    _save_cache(_cache)
    return name


def prefetch_item_names(item_ids: list[int]) -> dict[int, str]:
    """
    Bulk-fetch names for a list of item IDs, skipping those already cached.
    Returns {item_id: name} for all IDs.
    Prints a progress line every 10 fetches.
    """
    uncached = [iid for iid in item_ids if str(iid) not in _cache]

    if uncached:
        print(f"  [blizzard_api] fetching {len(uncached)} new item names "
              f"({len(item_ids) - len(uncached)} already cached)...")
        for i, iid in enumerate(uncached, 1):
            get_item_name(iid)
            if i % 10 == 0:
                print(f"    {i}/{len(uncached)} fetched")
        print(f"  [blizzard_api] done — cache now has {len(_cache)} entries")

    return {iid: _cache.get(str(iid), f"Item {iid}") for iid in item_ids}


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_ids = [240974, 244616, 239201, 238512, 99999999]
    print("Testing Blizzard API item lookups:")
    for iid in test_ids:
        name = get_item_name(iid)
        print(f"  {iid:>9}  →  {name}")
