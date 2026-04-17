#!/usr/bin/env python3
"""
Blizzard Auction House API client.

Fetches live AH data for Reagents and Consumables across 5 US realms.
Reuses authentication from blizzard_api.py (OAuth2 client credentials flow).

Key endpoints used:
  /data/wow/realm/{slug}                             → connected realm ID lookup
  /data/wow/connected-realm/{id}/auctions            → per-realm regular auctions
  /data/wow/auctions/commodities                     → NA-wide commodity auctions
  /data/wow/item-class/index                         → discover category class IDs
  /data/wow/item/{id}                                → per-item class lookup (cached)

Commodity AH (shared across all NA realms) is the primary source for reagents
and crafting materials since Dragonflight. Per-realm AH is used for gear and
non-commodity items.

Future extensibility:
  - Weapons/armor filtering: add item class IDs to FILTER_CLASS_IDS
  - Multi-character flipping: pass a list of flippers instead of "Bankarang"
  - Cross-faction arbitrage: add separate calls for faction-specific realms
  - Rate limit throttling: _request_count is logged; add sleep() calls here
  - Profession crafting profit: combine live AH prices with recipe costs
"""

import gzip
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from blizzard_api import _get_token

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR          = Path(__file__).parent
REALM_CACHE_FILE    = SCRIPT_DIR / "connected_realms.json"
ITEM_CATEGORY_FILE  = SCRIPT_DIR / "item_categories.json"
ITEM_CLASS_CACHE    = SCRIPT_DIR / "item_class_cache.json"

API_BASE = "https://us.api.blizzard.com"

# The 5 target US realms and their API slugs
REALM_SLUGS: dict[str, str] = {
    "Malfurion":  "malfurion",
    "Maelstrom":  "maelstrom",
    "Moon Guard": "moon-guard",
    "Mal'Ganis":  "malganis",
    "Thrall":     "thrall",
}

# Item class IDs for Reagent (5), Consumable (0), Tradeskill (7)
# Fetched and cached from the API; these defaults are used if the API call fails.
# Tradeskill (7) covers raw crafting materials like ore, herbs, cloth.
DEFAULT_FILTER_CLASS_IDS: set[int] = {0, 5, 7}

_request_count = 0

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _api_get(path: str, namespace: str = "dynamic-us", locale: str = "en_US",
             timeout: int = 60) -> dict:
    """Authenticated GET against the Blizzard API. Logs request count for rate-limit awareness."""
    global _request_count
    token = _get_token()
    sep = "&" if "?" in path else "?"
    url = f"{API_BASE}{path}{sep}namespace={namespace}&locale={locale}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
        },
    )
    _request_count += 1
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        logger.error(f"HTTP {exc.code} for {path}: {exc.reason}")
        raise
    except Exception as exc:
        logger.error(f"Request failed for {path}: {exc}")
        raise


# ---------------------------------------------------------------------------
# Connected realm ID lookup
# ---------------------------------------------------------------------------

def _extract_id_from_href(href: str) -> int:
    """Parse the numeric ID out of a Blizzard API href URL."""
    m = re.search(r"/(\d+)[/?]", href)
    if not m:
        raise ValueError(f"Could not parse ID from href: {href}")
    return int(m.group(1))


def _fetch_connected_realm_id(realm_name: str, slug: str) -> int:
    """Fetch connected realm ID for one realm via the realm API."""
    data = _api_get(f"/data/wow/realm/{slug}", namespace="dynamic-us")
    href = data["connected_realm"]["href"]
    crid = _extract_id_from_href(href)
    logger.info(f"  {realm_name} → connected realm {crid}")
    return crid


def get_connected_realm_ids() -> dict[str, int]:
    """
    Return {realm_name: connected_realm_id} for all 5 target realms.
    Reads from cache on subsequent runs to avoid redundant API calls.
    """
    if REALM_CACHE_FILE.exists():
        try:
            cached = json.loads(REALM_CACHE_FILE.read_text())
            if all(r in cached for r in REALM_SLUGS):
                logger.info(f"Connected realm IDs loaded from cache ({REALM_CACHE_FILE})")
                return {k: int(v) for k, v in cached.items()}
        except Exception:
            pass

    logger.info("Looking up connected realm IDs (first run — caching for future runs)...")
    result: dict[str, int] = {}
    for name, slug in REALM_SLUGS.items():
        try:
            result[name] = _fetch_connected_realm_id(name, slug)
        except Exception as exc:
            logger.error(f"  Failed to get realm ID for {name}: {exc}")

    if result:
        REALM_CACHE_FILE.write_text(json.dumps(result, indent=2))
        logger.info(f"Cached {len(result)} realm IDs → {REALM_CACHE_FILE}")

    return result


# ---------------------------------------------------------------------------
# Item category discovery
# ---------------------------------------------------------------------------

def fetch_item_categories() -> dict:
    """
    Fetch Reagents and Consumables class IDs from the Blizzard item-class index.
    Caches result to ITEM_CATEGORY_FILE.
    Returns {class_id: {name, subclasses: [...]}}.
    """
    if ITEM_CATEGORY_FILE.exists():
        try:
            cached = json.loads(ITEM_CATEGORY_FILE.read_text())
            logger.info(f"Item categories loaded from cache ({ITEM_CATEGORY_FILE})")
            return cached
        except Exception:
            pass

    logger.info("Fetching item class index from Blizzard API...")
    index = _api_get("/data/wow/item-class/index", namespace="static-us")

    target_names = {"Consumable", "Reagent", "Tradeskill"}
    categories: dict[str, dict] = {}

    for cls in index.get("item_classes", []):
        if cls.get("name") not in target_names:
            continue
        cid = cls["id"]
        name = cls["name"]
        # Fetch subclasses for documentation
        try:
            detail = _api_get(f"/data/wow/item-class/{cid}", namespace="static-us")
            subclasses = [
                {"id": sc["id"], "name": sc.get("display_name", sc.get("name", ""))}
                for sc in detail.get("item_subclasses", [])
            ]
        except Exception:
            subclasses = []

        categories[str(cid)] = {"name": name, "subclasses": subclasses}
        logger.info(f"  Class {cid}: {name} ({len(subclasses)} subclasses)")

    if categories:
        ITEM_CATEGORY_FILE.write_text(json.dumps(categories, indent=2))
        logger.info(f"Cached item categories → {ITEM_CATEGORY_FILE}")

    return categories


def get_filter_class_ids() -> set[int]:
    """Return set of item class IDs to include (Consumable + Reagents)."""
    try:
        categories = fetch_item_categories()
        return {int(k) for k in categories}
    except Exception:
        logger.warning("Could not fetch item categories — using default class IDs {0, 15}")
        return DEFAULT_FILTER_CLASS_IDS


# ---------------------------------------------------------------------------
# Item class cache (for per-realm auction filtering)
# ---------------------------------------------------------------------------

def _load_item_class_cache() -> dict[str, int]:
    if ITEM_CLASS_CACHE.exists():
        try:
            return json.loads(ITEM_CLASS_CACHE.read_text())
        except Exception:
            pass
    return {}


def _save_item_class_cache(cache: dict[str, int]) -> None:
    ITEM_CLASS_CACHE.write_text(json.dumps(cache, indent=2))


def update_item_class_cache(item_ids: list[int], max_fetch: int = 100) -> dict[str, int]:
    """
    Fetch and cache item class IDs for unknown item IDs.
    Limits to max_fetch new lookups per call to avoid rate limiting.
    Returns the updated cache.
    """
    cache = _load_item_class_cache()
    uncached = [iid for iid in item_ids if str(iid) not in cache]

    if not uncached:
        return cache

    to_fetch = uncached[:max_fetch]
    logger.info(f"Fetching item class for {len(to_fetch)} items "
                f"({len(uncached) - len(to_fetch)} deferred to next run)...")

    for iid in to_fetch:
        try:
            data = _api_get(f"/data/wow/item/{iid}", namespace="static-us")
            class_id = data.get("item_class", {}).get("id")
            if class_id is not None:
                cache[str(iid)] = class_id
        except Exception:
            pass  # Item not found or error — skip silently

    _save_item_class_cache(cache)
    logger.info(f"Item class cache now has {len(cache)} entries")
    return cache


# ---------------------------------------------------------------------------
# Auction normalization helpers
# ---------------------------------------------------------------------------

def _normalize_commodity(auction: dict, realm: str) -> dict | None:
    """
    Normalize a commodity auction entry to a standard format.
    Commodity auctions use unit_price (per-unit, in copper).
    """
    iid = auction.get("item", {}).get("id")
    unit_price_copper = auction.get("unit_price")
    qty = auction.get("quantity", 1)

    if not iid or unit_price_copper is None:
        return None

    return {
        "item_id":          iid,
        "quantity":         qty,
        "buyout_per_unit":  round(unit_price_copper / 10_000, 4),  # copper → gold
        "time_left":        "COMMODITY",
        "realm":            realm,
        "is_commodity":     True,
    }


def _normalize_regular(auction: dict, realm: str) -> dict | None:
    """
    Normalize a regular (non-commodity) auction entry to a standard format.
    Regular auctions use buyout (total stack price, in copper).
    """
    iid = auction.get("item", {}).get("id")
    qty = auction.get("quantity", 1)
    buyout = auction.get("buyout") or auction.get("unit_price")  # unit_price fallback
    time_left = auction.get("time_left", "UNKNOWN")

    if not iid or buyout is None:
        return None

    return {
        "item_id":          iid,
        "quantity":         qty,
        "buyout_per_unit":  round(buyout / qty / 10_000, 4),  # copper → gold, per unit
        "time_left":        time_left,
        "realm":            realm,
        "is_commodity":     False,
    }


# ---------------------------------------------------------------------------
# Auction filtering
# ---------------------------------------------------------------------------

_GEAR_EXCL_KEYWORDS = {
    "sabatons", "greaves", "gauntlets", "handguards", "helm", "coif",
    "pauldrons", "spaulders", "shoulderguards", "shoulderpads",
    "breastplate", "cuirass", "tunic", "vest", "doublet", "jerkin",
    "leggings", "breeches", "trousers", "waders",
    "bracers", "wristwraps", "armguards",
    "waistband", "sash", "cinch",
    "cloak", "cape", "shawl",
    "signet", "locket", "necklace", "pendant", "amulet", "medallion",
    "sword", "blade", "dagger", "maul", "club", "cudgel",
    "wand", "scepter", "crossbow", "arquebus",
    "shield", "aegis", "bulwark",
    "greatsword", "polearm", "glaive", "spear",
}


def _name_is_gear(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in _GEAR_EXCL_KEYWORDS)


def filter_auctions(
    auctions: list[dict],
    is_commodity: bool,
    names: dict[str, str] | None = None,
    class_ids: set[int] | None = None,
) -> list[dict]:
    """
    Filter auctions to Reagents (class 15) and Consumables (class 0).

    For commodity auctions: all included — the commodity AH is by definition
    reagents, crafting materials, and stackable consumables.

    For per-realm auctions: check item class cache first; fall back to
    excluding obvious gear by name keywords.

    Weapons and armor filtering may be added later — easy to extend by
    adding more item class IDs to the filter.
    """
    if is_commodity:
        # Commodity AH = reagents + crafting mats + stackable consumables
        return auctions

    if class_ids is None:
        class_ids = DEFAULT_FILTER_CLASS_IDS

    item_class_cache = _load_item_class_cache()
    result = []

    for a in auctions:
        iid = a["item_id"]
        cached_class = item_class_cache.get(str(iid))
        if cached_class is not None:
            if cached_class in class_ids:
                result.append(a)
        else:
            # Unknown class: exclude obvious gear by name, include everything else
            name = (names or {}).get(str(iid), "")
            if not _name_is_gear(name):
                result.append(a)

    return result


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

def fetch_auctions(connected_realm_id: int) -> list[dict]:
    """
    Fetch regular (non-commodity) auctions for a connected realm.
    Returns raw auction list from the API response.
    Handles HTTP errors with logging.
    """
    logger.info(f"  Fetching regular auctions for connected realm {connected_realm_id}...")
    data = _api_get(
        f"/data/wow/connected-realm/{connected_realm_id}/auctions",
        namespace="dynamic-us",
        timeout=90,
    )
    auctions = data.get("auctions", [])
    logger.info(f"    → {len(auctions):,} regular auction entries (request #{_request_count})")
    return auctions


def fetch_commodities() -> list[dict]:
    """
    Fetch commodity (shared NA-wide) auctions.
    These include all stackable reagents and crafting materials.
    Returns raw auction list from the API response.
    """
    logger.info("  Fetching commodity auctions (NA-wide)...")
    data = _api_get("/data/wow/auctions/commodities", namespace="dynamic-us", timeout=120)
    auctions = data.get("auctions", [])
    logger.info(f"    → {len(auctions):,} commodity entries (request #{_request_count})")
    return auctions


def fetch_all_realms() -> dict[str, list[dict]]:
    """
    Fetch and filter AH data for all 5 target realms.

    Fetches commodity auctions once (NA-wide) and per-realm regular auctions
    for each realm. Returns {realm_name: [filtered_auction_records]}.

    Commodity prices apply equally to all NA realms since they share one AH.
    """
    realm_ids  = get_connected_realm_ids()
    class_ids  = get_filter_class_ids()

    # Load item name cache for fallback filtering of per-realm gear
    names_file = Path.home() / "item_names.json"
    names: dict[str, str] = {}
    if names_file.exists():
        try:
            names = json.loads(names_file.read_text())
        except Exception:
            pass

    # Commodity auctions: fetch once, shared across all NA realms
    try:
        raw_commodities = fetch_commodities()
        commodity_normalized: list[dict] = []
        for a in raw_commodities:
            norm = _normalize_commodity(a, "Commodities")
            if norm:
                commodity_normalized.append(norm)
        commodity_filtered = filter_auctions(commodity_normalized, is_commodity=True)
        logger.info(f"Commodities: {len(commodity_filtered):,} filtered records")
    except Exception as exc:
        logger.error(f"Commodity fetch failed: {exc}")
        commodity_filtered = []

    results: dict[str, list[dict]] = {}
    total_requests_before = _request_count

    for realm_name, crid in realm_ids.items():
        try:
            raw = fetch_auctions(crid)
            regular_norm: list[dict] = []
            for a in raw:
                norm = _normalize_regular(a, realm_name)
                if norm:
                    regular_norm.append(norm)
            regular_filtered = filter_auctions(
                regular_norm, is_commodity=False, names=names, class_ids=class_ids
            )

            # Tag commodity records with this realm and merge
            realm_commodities = [
                {**a, "realm": realm_name} for a in commodity_filtered
            ]
            combined = realm_commodities + regular_filtered
            results[realm_name] = combined

            logger.info(
                f"{realm_name}: {len(combined):,} total "
                f"({len(realm_commodities):,} commodity + {len(regular_filtered):,} regular)"
            )
        except Exception as exc:
            logger.error(f"Failed to fetch auctions for {realm_name}: {exc}")
            # Still include commodity data for this realm
            results[realm_name] = [{**a, "realm": realm_name} for a in commodity_filtered]

    logger.info(
        f"fetch_all_realms complete — "
        f"{_request_count - total_requests_before + 1} API requests total"
    )
    return results


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")

    print("=== Blizzard AH API Test ===\n")

    print("1. Connected realm IDs:")
    realm_ids = get_connected_realm_ids()
    for name, crid in realm_ids.items():
        print(f"   {name:<12} → {crid}")

    print("\n2. Item categories:")
    cats = fetch_item_categories()
    for cid, info in cats.items():
        print(f"   Class {cid}: {info['name']} ({len(info.get('subclasses', []))} subclasses)")

    print("\n3. Fetching all realms (this may take a minute)...")
    data = fetch_all_realms()
    for realm, auctions in data.items():
        print(f"   {realm:<12} → {len(auctions):,} filtered auctions")

    print(f"\nTotal API requests this run: {_request_count}")
