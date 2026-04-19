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

_PROJECT_DIR     = Path(__file__).parent
ENV_FILE         = Path.home() / ".env"
CACHE_FILE       = _PROJECT_DIR / "item_names.json"
ITEM_CLASS_FILE  = _PROJECT_DIR / "item_class_ids.json"

# Item class/subclass combos to exclude from flipping analysis.
# class 17        = Battle Pets
# class 15 sub 2  = Companion Pets (old cage-pet format)
# class 15 sub 5  = Mounts
_EXCLUDED_CLASS_IDS: frozenset[int] = frozenset({17})
_EXCLUDED_CLASS_SUB_PAIRS: frozenset[tuple] = frozenset({(15, 2), (15, 5)})

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
# Item class cache (persisted to item_class_ids.json)
# Stores {item_id_str: {"c": class_id, "s": subclass_id}}
# ---------------------------------------------------------------------------

def _load_class_cache() -> dict[str, dict]:
    if ITEM_CLASS_FILE.exists():
        try:
            return json.loads(ITEM_CLASS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_class_cache(cache: dict[str, dict]) -> None:
    ITEM_CLASS_FILE.write_text(
        json.dumps(cache, separators=(',', ':'), ensure_ascii=False),
        encoding="utf-8",
    )


_class_cache: dict[str, dict] = _load_class_cache()


def is_excluded_item(item_id: int) -> bool:
    """Return True for Battle Pets, Companion Pets, and Mounts."""
    entry = _class_cache.get(str(item_id))
    if entry is None:
        return False  # unknown class → include by default
    c = entry.get("c")
    s = entry.get("s")
    return c in _EXCLUDED_CLASS_IDS or (c, s) in _EXCLUDED_CLASS_SUB_PAIRS


# ---------------------------------------------------------------------------
# API lookup
# ---------------------------------------------------------------------------

def _fetch_item_info(item_id: int, token: str) -> tuple[str, int | None, int | None]:
    """Hit the Blizzard item API and return (name, class_id, subclass_id)."""
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
    class_id    = data.get("item_class",    {}).get("id")
    subclass_id = data.get("item_subclass", {}).get("id")
    return data["name"], class_id, subclass_id


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_item_name(item_id: int) -> str:
    """
    Return the WoW item name for item_id.
    Also saves item class/subclass to item_class_ids.json on each new lookup.
    Results are cached in item_names.json.
    Falls back to 'Unknown Item ({item_id})' if the lookup fails.
    """
    key = str(item_id)

    if key in _cache:
        return _cache[key]

    try:
        token = _get_token()
        name, class_id, subclass_id = _fetch_item_info(item_id, token)
        _class_cache[key] = {"c": class_id, "s": subclass_id}
        _save_class_cache(_class_cache)
    except Exception as exc:
        # Non-fatal: bad IDs (bonus-ID variants, test items) return 404
        print(f"  [blizzard_api] lookup failed for item {item_id}: {exc}")
        name = f"Unknown Item ({item_id})"

    _cache[key] = name
    _save_cache(_cache)
    return name


def prefetch_item_names(item_ids: list[int], max_new: int = 200,
                        delay: float = 0.05) -> dict[int, str]:
    """
    Bulk-fetch names for a list of item IDs, skipping those already cached.
    Also saves item class/subclass on each new lookup.
    Returns {item_id: name} for all IDs.

    max_new  — cap on new API calls per invocation (rest deferred to next run)
    delay    — seconds to sleep between API calls (default 0.05s = 20 req/s)
    """
    uncached = [iid for iid in item_ids if str(iid) not in _cache]
    to_fetch = uncached[:max_new]
    deferred = len(uncached) - len(to_fetch)

    if to_fetch:
        print(f"  [blizzard_api] fetching {len(to_fetch)} new item names "
              f"({len(item_ids) - len(uncached)} cached"
              + (f", {deferred} deferred to next run" if deferred else "") + ")...")
        for i, iid in enumerate(to_fetch, 1):
            get_item_name(iid)
            if delay > 0:
                time.sleep(delay)
            if i % 20 == 0:
                print(f"    {i}/{len(to_fetch)} fetched")
        print(f"  [blizzard_api] done — cache now has {len(_cache)} entries")

    return {iid: _cache.get(str(iid), f"Unknown Item ({iid})") for iid in item_ids}


def prefetch_item_classes(item_ids: list[int], max_new: int = 200,
                          delay: float = 0.05) -> None:
    """
    Backfill item class/subclass for IDs already in the name cache but lacking
    class info.  Useful for items fetched before class tracking was added.
    Does NOT re-fetch names — only saves class data.
    """
    uncached = [iid for iid in item_ids if str(iid) not in _class_cache]
    to_fetch = uncached[:max_new]
    if not to_fetch:
        return

    print(f"  [blizzard_api] backfilling class info for {len(to_fetch)} items "
          f"({len(uncached) - len(to_fetch)} deferred)...")
    try:
        token = _get_token()
    except Exception as exc:
        print(f"  [blizzard_api] class backfill skipped (auth error): {exc}")
        return

    fetched = 0
    for iid in to_fetch:
        key = str(iid)
        try:
            _, class_id, subclass_id = _fetch_item_info(iid, token)
            _class_cache[key] = {"c": class_id, "s": subclass_id}
            fetched += 1
        except Exception:
            pass
        if delay > 0:
            time.sleep(delay)

    _save_class_cache(_class_cache)
    print(f"  [blizzard_api] class cache now has {len(_class_cache)} entries")


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_ids = [240974, 244616, 239201, 238512, 99999999]
    print("Testing Blizzard API item lookups:")
    for iid in test_ids:
        name = get_item_name(iid)
        print(f"  {iid:>9}  →  {name}")
