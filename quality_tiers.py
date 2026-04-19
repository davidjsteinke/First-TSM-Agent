#!/usr/bin/env python3
"""
Quality tier detection for Midnight (WoW 11.x) items.

Two encoding systems coexist in Midnight:

  1. Separate item IDs per quality tier — used for reagents, consumables,
     enchants, and crafting materials.  Within a same-name group, lower
     item ID = T1, next = T2, etc.  Detected by grouping item_names.json
     entries with identical names.

  2. Bonus IDs on per-realm AH auctions and TSM item strings — used for
     crafted gear, profession tools, and profession bags.  Same item_id,
     different bonus ID per tier:
       12498 = T1   12499 = T2   12500 = T3   12501 = T4   12502 = T5

Items that match neither system return '' (no quality tier).
"""

import json
from pathlib import Path

SCRIPT_DIR    = Path(__file__).parent
NAMES_FILE    = SCRIPT_DIR / "item_names.json"
TIER_MAP_FILE = SCRIPT_DIR / "quality_tier_map.json"

MIDNIGHT_MIN_ID = 236_000

# Bonus ID → quality tier label (crafted gear / profession tools)
QUALITY_BONUS_IDS: dict[int, str] = {
    12498: "T1",
    12499: "T2",
    12500: "T3",
    12501: "T4",
    12502: "T5",
}

_tier_map_cache: dict[int, str] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_from_names(names: dict[str, str]) -> dict[int, str]:
    """
    Build {item_id: 'T1'|'T2'|...} for Midnight items that share the same
    display name.  Lower item ID = T1, next = T2, etc.
    """
    from collections import defaultdict
    by_name: dict[str, list[int]] = defaultdict(list)
    for k, v in names.items():
        iid = int(k)
        if iid >= MIDNIGHT_MIN_ID:
            by_name[v].append(iid)

    result: dict[int, str] = {}
    tier_labels = ["T1", "T2", "T3", "T4", "T5"]
    for name, ids in by_name.items():
        if len(ids) > 1:
            for rank, iid in enumerate(sorted(ids)):
                label = tier_labels[rank] if rank < len(tier_labels) else f"T{rank + 1}"
                result[iid] = label
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rebuild_tier_map() -> dict[int, str]:
    """
    Re-read item_names.json and rebuild quality_tier_map.json from scratch.
    Call this after item names are fetched / updated.
    """
    global _tier_map_cache
    names: dict[str, str] = {}
    if NAMES_FILE.exists():
        try:
            names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    _tier_map_cache = _build_from_names(names)
    TIER_MAP_FILE.write_text(
        json.dumps({str(k): v for k, v in _tier_map_cache.items()},
                   separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return _tier_map_cache


def get_tier_map() -> dict[int, str]:
    """Return the {item_id: tier} map, loading from disk or building if missing."""
    global _tier_map_cache
    if _tier_map_cache is not None:
        return _tier_map_cache
    if TIER_MAP_FILE.exists():
        try:
            raw = json.loads(TIER_MAP_FILE.read_text(encoding="utf-8"))
            _tier_map_cache = {int(k): v for k, v in raw.items()}
            return _tier_map_cache
        except Exception:
            pass
    return rebuild_tier_map()


def tier_from_bonus_list(bonus_list: list[int]) -> str:
    """
    Return the quality tier string from a list of Blizzard bonus IDs.
    Returns '' if no recognised quality bonus ID is present.
    """
    for bid in bonus_list:
        t = QUALITY_BONUS_IDS.get(bid)
        if t:
            return t
    return ""


def tier_from_tsm_bonus(bonus_str: str | None) -> str:
    """
    Parse a TSM bonus_ids string (e.g. '3:12251:12252:12502') and return the
    quality tier label.  The first integer is the count of bonus IDs that follow.
    Returns '' if no quality bonus ID is found or string is malformed.
    """
    if not bonus_str:
        return ""
    parts = bonus_str.split(":")
    try:
        count = int(parts[0])
        ids = [int(parts[i]) for i in range(1, min(count + 1, len(parts)))]
        return tier_from_bonus_list(ids)
    except (ValueError, IndexError):
        return ""


def get_item_quality(item_id: int, bonus_list: list[int] | None = None) -> str:
    """
    Return quality tier string ('T1'..'T5') for an item.

    Priority:
      1. bonus_list (explicit, from per-realm AH or TSM item string)
      2. tier_map   (inferred from same-name item ID grouping)
      3. ''         (no quality tier detected)
    """
    if bonus_list:
        t = tier_from_bonus_list(bonus_list)
        if t:
            return t
    return get_tier_map().get(item_id, "")


def fmt_quality(quality_tier: str) -> str:
    """Return display string for a quality tier, e.g. '[T2]'. Empty string for no tier."""
    return f"[{quality_tier}]" if quality_tier else ""
