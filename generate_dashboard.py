#!/usr/bin/env python3
"""
Generates dashboard.html — a self-contained interactive web dashboard
that reads from tsm_data.json and embeds all data as inline JSON.

No server, no external dependencies. Open the file directly in any browser.
"""

import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import blizzard_api
import live_ah_db
import quality_tiers

# Resolve paths relative to this file so the script works from any CWD
SCRIPT_DIR      = Path(__file__).parent
DATA_FILE       = Path.home() / "tsm_data.json"
NAMES_FILE      = SCRIPT_DIR / "item_names.json"
DASHBOARD       = SCRIPT_DIR / "dashboard.html"
PUBLIC_DASH     = SCRIPT_DIR / "dashboard_public.html"
DOCS_DIR        = SCRIPT_DIR / "docs"

PRIMARY_REALM    = "Malfurion"
AH_CUT           = 0.05
MIN_SPREAD_PCT   = 20.0

# Flipping analysis: Bankarang on Malfurion is the designated flipper.
# Other characters buy reagents for crafting — excluded from flip analysis.
# See agent.py for the full explanation.
FLIPPER = "Bankarang"

ALL_REALMS = ["Malfurion", "Maelstrom", "Moon Guard", "Mal'Ganis", "Thrall"]

# Midnight expansion item IDs start here (Thalassian / Blood Elf / Quel'Thalas themed)
MIDNIGHT_MIN_ID = 236000

# Keywords that identify gear/consumables to exclude from reagent classification
_GEAR_CONSUMABLE_EXCL = {
    "sabatons", "greaves", "gauntlets", "handguards", "helm", "coif", "cover",
    "pauldrons", "spaulders", "shoulderguards", "epaulets", "shoulderpads",
    "breastplate", "cuirass", "tunic", "vest", "doublet", "jerkin",
    "leggings", "breeches", "trousers", "waders",
    "bracers", "wristwraps", "armguards", "cuffs",
    "waistband", "sash", "cinch",
    "cloak", "cape", "shawl",
    "signet", "locket",
    "necklace", "pendant", "amulet", "medallion",
    "sword", "blade", "dagger",
    "maul", "club", "cudgel", "censer",
    "wand", "scepter",
    "crossbow", "arquebus",
    "shield", "aegis", "bulwark",
    "greatsword", "polearm", "glaive", "spear",
    "potion", "phial", "flask", "elixir", "tonic", "draught",
    "stew", "cutlets", "roast", "sandwich", "tea", "bites",
    "rations", "skewers", "butter", "spices", "fixings", "chutney",
    "enchant ", "vantus rune", "contract:", "missive",
    "glamour", "illusory adornment",
    "treatise",
}


def is_midnight_reagent(name: str, item_id: int) -> bool:
    if item_id < MIDNIGHT_MIN_ID:
        return False
    lower = name.lower()
    if _is_profession_item(name):
        return False
    return not any(kw in lower for kw in _GEAR_CONSUMABLE_EXCL)

# ---------------------------------------------------------------------------
# Analysis helpers (self-contained — no imports from agent/arbitrage)
# ---------------------------------------------------------------------------

def load_item_names() -> dict[str, str]:
    if NAMES_FILE.exists():
        try:
            return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def item_name(iid: int, cache: dict) -> str:
    return cache.get(str(iid), f"Unknown Item ({iid})")


def build_profit_stats(records: list[dict]) -> list[dict]:
    # Bankarang-only filter: other characters buy for crafting, not resale.
    buys  = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Buys"  and r.get("source") == "Auction"
             and r.get("player") == FLIPPER]
    sales = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Sales" and r.get("source") == "Auction"
             and r.get("player") == FLIPPER]

    buy_acc  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_acc = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    for r in buys:
        key = (r["item_id"], r.get("quality_tier", ""))
        d = buy_acc[key]
        d["gold"] += r["price_gold"]; d["qty"] += r["quantity"]; d["txns"] += 1

    for r in sales:
        key = (r["item_id"], r.get("quality_tier", ""))
        d = sell_acc[key]
        d["gold"] += r["price_gold"]; d["qty"] += r["quantity"]; d["txns"] += 1

    out = []
    for key in set(buy_acc) & set(sell_acc):
        iid, qt = key
        if blizzard_api.is_excluded_item(iid):
            continue
        b, s = buy_acc[key], sell_acc[key]
        avg_buy  = b["gold"] / b["qty"]
        avg_sell = s["gold"] / s["qty"]
        profit   = avg_sell - avg_buy
        margin   = (profit / avg_buy * 100) if avg_buy else 0.0
        out.append({
            "item_id": iid, "quality_tier": qt,
            "avg_buy": avg_buy, "avg_sell": avg_sell,
            "profit_per_item": profit, "margin_pct": margin,
            "buy_txns": b["txns"], "sell_txns": s["txns"],
            "total_volume": b["txns"] + s["txns"],
        })

    out.sort(key=lambda x: x["profit_per_item"], reverse=True)
    return out


def build_live_arbitrage(names: dict) -> list[dict]:
    """
    Cross-realm arbitrage using live AH min prices across all 5 realms.
    No character history filter — any item present on multiple realms qualifies.

    Opportunity: (sell_realm_min × 0.95) − buy_realm_min ≥ MIN_SPREAD_PCT%
    Returns top 100 by net profit descending.
    """
    try:
        live_ah_db.init_db()
    except Exception:
        return []

    realm_data: dict[str, dict[tuple, dict]] = {}
    for realm in ALL_REALMS:
        try:
            snaps = live_ah_db.get_all_latest_snapshots(realm)
            realm_data[realm] = {(s["item_id"], s["quality_tier"]): s for s in snaps}
        except Exception:
            realm_data[realm] = {}

    # Collect (item_id, quality_tier) keys present in at least 2 realms
    item_realms: dict[tuple, list[str]] = {}
    for realm, items in realm_data.items():
        for key in items:
            item_realms.setdefault(key, []).append(realm)

    opps = []
    for (iid, qt), realms_present in item_realms.items():
        if len(realms_present) < 2:
            continue

        if iid < MIDNIGHT_MIN_ID:
            continue

        if blizzard_api.is_excluded_item(iid):
            continue

        # Suppress items with no resolved name
        if str(iid) not in names:
            continue

        prices = {r: realm_data[r][(iid, qt)]["min_price"] for r in realms_present}
        buy_realm  = min(prices, key=prices.__getitem__)
        sell_realm = max(prices, key=prices.__getitem__)

        if buy_realm == sell_realm:
            continue

        buy_price  = prices[buy_realm]
        sell_price = prices[sell_realm]

        if buy_price <= 0:
            continue

        # Exclude parked/placeholder prices above a reasonable reagent ceiling.
        # Cap at 50k to exclude obvious gear-level items slipping through the filter.
        # Require ≥5 listings on both sides to confirm active markets (not parked prices).
        if sell_price >= 50_000 or buy_price >= 50_000:
            continue
        buy_snap  = realm_data[buy_realm][(iid, qt)]
        sell_snap = realm_data[sell_realm][(iid, qt)]
        if buy_snap["listing_count"] < 5 or sell_snap["listing_count"] < 5:
            continue

        sell_net   = sell_price * (1 - AH_CUT)
        net_profit = sell_net - buy_price
        spread_pct = (net_profit / buy_price) * 100

        if spread_pct < MIN_SPREAD_PCT:
            continue

        name = names[str(iid)]

        opps.append({
            "item_id":        iid,
            "quality_tier":   qt,
            "item_name":      name,
            "category":       _item_category(iid, name),
            "buy_realm":      buy_realm,
            "buy_at":         round(buy_price, 4),
            "sell_realm":     sell_realm,
            "sell_at":        round(sell_price, 4),
            "net_profit":     round(net_profit, 4),
            "spread_pct":     round(spread_pct, 1),
            "listing_count":  buy_snap["listing_count"],
            "total_quantity": buy_snap["total_quantity"],
        })

    opps.sort(key=lambda x: x["net_profit"], reverse=True)
    return opps[:100]


def build_repricing(records: list[dict]) -> list[dict]:
    ce: dict = defaultdict(lambda: {"cancels": 0, "expirations": 0, "cq": 0, "eq": 0})
    for r in records:
        if r.get("realm") != PRIMARY_REALM: continue
        key = (r["item_id"], r.get("quality_tier", ""))
        if r.get("type") == "Cancelled":
            ce[key]["cancels"]    += 1
            ce[key]["cq"]         += r.get("quantity", 0)
        elif r.get("type") == "Expired":
            ce[key]["expirations"] += 1
            ce[key]["eq"]          += r.get("quantity", 0)

    sells   = defaultdict(int)
    bprices = defaultdict(list)
    for r in records:
        if r.get("realm") != PRIMARY_REALM: continue
        key = (r["item_id"], r.get("quality_tier", ""))
        if r.get("type") == "Sales"  and r.get("source") == "Auction":
            sells[key] += 1
        if r.get("type") == "Buys"   and r.get("source") == "Auction":
            qty  = r.get("quantity") or 1
            bprices[key].append(r["price_gold"] / qty)

    out = []
    for key, d in ce.items():
        iid, qt = key
        failures  = d["cancels"] + d["expirations"]
        successes = sells.get(key, 0)
        total     = failures + successes
        fail_rate = (failures / total * 100) if total else 100.0
        bp        = bprices.get(key)
        out.append({
            "item_id": iid, "quality_tier": qt,
            "cancels": d["cancels"], "expirations": d["expirations"],
            "failed_qty": d["cq"] + d["eq"],
            "sell_successes": successes, "total_listings": total,
            "failure_rate": fail_rate,
            "avg_buy_price": (sum(bp) / len(bp)) if bp else None,
        })

    out.sort(key=lambda x: (x["cancels"] + x["expirations"], x["failure_rate"]), reverse=True)
    return out


PROFESSION_TOOL_KEYWORDS = {
    "knife", "needle", "hammer", "chisel", "awl", "apron", "gloves", "hat",
    "goggles", "wrench", "focuser", "backpack", "chapeau", "bifocals",
    "rolling pin", "satchel", "cover", "visor", "multitool", "snippers",
    "clampers", "cutters", "toolset", "screwdriver", "wrench", "tongs",
    "cap", "loupes", "quill", "rod", "shears", "pick", "pickaxe", "sickle",
}


def _is_profession_item(item_name: str) -> bool:
    """Return True if the item name contains any profession tool keyword."""
    lower = item_name.lower()
    return any(kw in lower for kw in PROFESSION_TOOL_KEYWORDS)


def build_stop_buying(profit_stats: list[dict], names: dict) -> list[dict]:
    """
    Items with negative margin — losing gold on every transaction.
    Profession tools and accessories are filtered out; these are typically
    one-off crafting purchases, not repeatable commodity trades.
    """
    stop = []
    for s in profit_stats:
        if s["profit_per_item"] >= 0:
            continue
        name = names.get(str(s["item_id"]), f"Unknown Item ({s['item_id']})")
        if _is_profession_item(name):
            continue
        loss_per_item   = abs(s["profit_per_item"])
        total_gold_lost = loss_per_item * s["buy_txns"]
        stop.append({
            **s,
            "loss_per_item":   loss_per_item,
            "loss_pct":        abs(s["margin_pct"]),
            "total_gold_lost": total_gold_lost,
        })
    stop.sort(key=lambda x: x["total_gold_lost"], reverse=True)
    return stop


def build_reagents(records: list[dict], names: dict,
                   market_values: dict[str, float],
                   live_ah_dict: dict[int, dict] | None = None) -> list[dict]:
    """
    Midnight-era reagents present in personal transaction history.
    Uses live AH price as primary reference; falls back to TSM MV if present.
    """
    buys  = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Buys"  and r.get("source") == "Auction"]
    sales = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Sales" and r.get("source") == "Auction"]

    buy_acc  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_acc = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    all_keys: set[tuple] = set()
    for r in buys:
        key = (r["item_id"], r.get("quality_tier", ""))
        buy_acc[key]["gold"] += r["price_gold"]
        buy_acc[key]["qty"]  += r["quantity"]
        buy_acc[key]["txns"] += 1
        all_keys.add(key)
    for r in sales:
        key = (r["item_id"], r.get("quality_tier", ""))
        sell_acc[key]["gold"] += r["price_gold"]
        sell_acc[key]["qty"]  += r["quantity"]
        sell_acc[key]["txns"] += 1
        all_keys.add(key)

    out = []
    for key in all_keys:
        iid, qt = key
        name = names.get(str(iid), f"Unknown Item ({iid})")
        if not is_midnight_reagent(name, iid):
            continue

        b = buy_acc.get(key)
        s = sell_acc.get(key)
        avg_buy  = (b["gold"] / b["qty"]) if b and b["qty"] else None
        avg_sell = (s["gold"] / s["qty"]) if s and s["qty"] else None
        txn_count = (b["txns"] if b else 0) + (s["txns"] if s else 0)

        live_snap   = (live_ah_dict or {}).get(key) or (live_ah_dict or {}).get((iid, ""))
        live_ah_min = live_snap["min_price"] if live_snap else None
        live_ah_avg = live_snap["avg_price"] if live_snap else None

        ref_price = live_ah_min or market_values.get(str(iid))
        profit_potential = (ref_price - avg_buy) if (ref_price and avg_buy) else None
        net_if_sold_at_min = round(live_ah_min * 0.95 - avg_buy, 4) if (live_ah_min is not None and avg_buy is not None) else None

        out.append({
            "item_id":            iid,
            "quality_tier":       qt,
            "item_name":          name,
            "live_ah_min":        live_ah_min,
            "buy_at":             live_ah_min,
            "live_ah_avg":        live_ah_avg,
            "avg_buy":            avg_buy,
            "avg_sell":           avg_sell,
            "profit_potential":   profit_potential,
            "net_if_sold_at_min": net_if_sold_at_min,
            "transaction_count":  txn_count,
        })

    out.sort(key=lambda x: (
        x["profit_potential"] if x["profit_potential"] is not None else float("-inf")
    ), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Live AH data (from live_ah.db)
# ---------------------------------------------------------------------------

def _bankarang_prices(records: list[dict]) -> dict[tuple, dict]:
    """
    Return {(item_id, quality_tier): {avg_buy, avg_sell, buy_txns, sell_txns}}
    for Bankarang/Malfurion.
    """
    buy_acc  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_acc = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    for r in records:
        if r.get("realm") != PRIMARY_REALM: continue
        if r.get("source") != "Auction":    continue
        if r.get("player") != FLIPPER:      continue
        key = (r["item_id"], r.get("quality_tier", ""))
        if r.get("type") == "Buys":
            buy_acc[key]["gold"] += r["price_gold"]
            buy_acc[key]["qty"]  += r["quantity"]
            buy_acc[key]["txns"] += 1
        elif r.get("type") == "Sales":
            sell_acc[key]["gold"] += r["price_gold"]
            sell_acc[key]["qty"]  += r["quantity"]
            sell_acc[key]["txns"] += 1

    out: dict[tuple, dict] = {}
    for key in set(buy_acc) | set(sell_acc):
        b = buy_acc.get(key)
        s = sell_acc.get(key)
        out[key] = {
            "avg_buy":   (b["gold"] / b["qty"]) if b and b["qty"] else None,
            "avg_sell":  (s["gold"] / s["qty"]) if s and s["qty"] else None,
            "buy_txns":  b["txns"] if b else 0,
            "sell_txns": s["txns"] if s else 0,
        }
    return out


def _item_category(item_id: int, item_name_str: str) -> str:
    """Infer item category for the Live AH tab display."""
    # Check item class cache built by blizzard_ah.py
    cache_file = SCRIPT_DIR / "item_class_cache.json"
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            cls = cache.get(str(item_id))
            if cls == 0:   return "Consumable"
            if cls == 5:   return "Reagent"
            if cls == 7:   return "Tradeskill"
        except Exception:
            pass
    # Keyword fallback
    lower = item_name_str.lower()
    consumable_kw = {"potion", "phial", "flask", "elixir", "food", "feast", "tonic", "draught"}
    if any(kw in lower for kw in consumable_kw):
        return "Consumable"
    return "Reagent"


MAX_LIVE_AH_ROWS_OTHER = 200   # cap for non-Malfurion realms (no Bankarang filter)


def build_live_ah_data(records: list[dict], names: dict) -> dict:
    """
    Build the Live AH tab data for all 5 realms.

    Malfurion: only rows where Bankarang has historical buy or sell data —
    the "—" rows for items Bankarang has never touched are noise here.

    Other realms: top MAX_LIVE_AH_ROWS_OTHER items by listing count (most active).
    Capped to keep the dashboard HTML small and tab-switching fast.
    """
    try:
        live_ah_db.init_db()
    except Exception:
        return {"realms": ALL_REALMS, "by_realm": {}, "last_updated": None,
                "total_counts": {}}

    ban_prices = _bankarang_prices(records)

    raw_by_realm: dict[str, list[dict]] = {}
    total_counts: dict[str, int] = {}
    last_updated = None

    for realm in ALL_REALMS:
        try:
            snapshots = live_ah_db.get_all_latest_snapshots(realm)
        except Exception:
            raw_by_realm[realm] = []
            total_counts[realm] = 0
            continue

        total_counts[realm] = len(snapshots)
        rows = []

        for snap in snapshots:
            iid  = snap["item_id"]
            qt   = snap.get("quality_tier", "")
            bp   = ban_prices.get((iid, qt)) or ban_prices.get((iid, ""), {})
            avg_buy  = bp.get("avg_buy")
            avg_sell = bp.get("avg_sell")

            # Malfurion: skip rows with no Bankarang history (they're just market noise)
            if realm == PRIMARY_REALM and avg_buy is None and avg_sell is None:
                continue

            if blizzard_api.is_excluded_item(iid):
                continue

            # Suppress items with no resolved name — unknown IDs are noise
            if str(iid) not in names:
                continue

            name     = names[str(iid)]
            live_min = snap["min_price"]
            live_avg = snap["avg_price"]

            # Spread: how far live AH min is from Bankarang's averages (Malfurion only)
            spread_pct = None
            if realm == PRIMARY_REALM:
                if avg_sell and live_min:
                    spread_pct = round((avg_sell - live_min) / avg_sell * 100, 1)
                elif avg_buy and live_min:
                    spread_pct = round((live_min - avg_buy) / avg_buy * 100, 1)

            ts = snap.get("timestamp_utc", "")
            if ts and (last_updated is None or ts > last_updated):
                last_updated = ts

            net_if_sold_at_min = round(live_min * 0.95 - avg_buy, 4) if avg_buy is not None else None

            rows.append({
                "item_id":              iid,
                "quality_tier":         qt,
                "item_name":            name,
                "category":             _item_category(iid, name),
                "live_ah_min":          round(live_min, 4),
                "buy_at":               round(live_min, 4),
                "live_ah_avg":          round(live_avg, 4),
                "total_quantity":       snap["total_quantity"],
                "listing_count":        snap["listing_count"],
                "bankarang_avg_buy":    round(avg_buy, 4) if avg_buy else None,
                "bankarang_avg_sell":   round(avg_sell, 4) if avg_sell else None,
                "buy_txns":             bp.get("buy_txns", 0),
                "sell_txns":            bp.get("sell_txns", 0),
                "spread_pct":           spread_pct,
                "net_if_sold_at_min":   net_if_sold_at_min,
                "last_updated":         ts,
            })

        raw_by_realm[realm] = rows

    # Compute multi-realm average min price per (item_id, quality_tier)
    # Used for cross-realm color coding on non-Malfurion tabs.
    from collections import defaultdict
    _key_mins: dict[tuple, list[float]] = defaultdict(list)
    for realm_rows in raw_by_realm.values():
        for row in realm_rows:
            _key_mins[(row["item_id"], row["quality_tier"])].append(row["live_ah_min"])
    multi_realm_avg: dict[tuple, float] = {
        k: round(sum(v) / len(v), 4) for k, v in _key_mins.items() if len(v) > 1
    }

    by_realm: dict[str, list[dict]] = {}
    for realm in ALL_REALMS:
        rows = raw_by_realm.get(realm, [])
        # Attach multi-realm average for cross-realm coloring
        for row in rows:
            avg = multi_realm_avg.get((row["item_id"], row["quality_tier"]))
            row["multi_realm_avg_min"] = avg

        # Sort: Malfurion by absolute spread desc, others by listing_count desc
        if realm == PRIMARY_REALM:
            rows.sort(
                key=lambda r: (abs(r["spread_pct"]) if r["spread_pct"] is not None else 0),
                reverse=True,
            )
        else:
            rows.sort(key=lambda r: r["listing_count"], reverse=True)
            rows = rows[:MAX_LIVE_AH_ROWS_OTHER]

        by_realm[realm] = rows

    return {
        "realms":       ALL_REALMS,
        "by_realm":     by_realm,
        "last_updated": last_updated,
        "total_counts": total_counts,
    }


# ---------------------------------------------------------------------------
# Bulk name prefetch — resolves unknown item IDs via Blizzard API before rendering
# ---------------------------------------------------------------------------

def _prefetch_dashboard_names(existing: dict) -> None:
    """
    Pre-populate item_names.json with names for items in live_ah.db that are not
    yet cached. Fetches at most 200 new Midnight names + 50 legacy names per run.
    Called once per generate_dashboard run so all tabs show real names.
    """
    try:
        live_ah_db.init_db()
        midnight_ids: list[int] = []
        legacy_ids:   list[int] = []
        for realm in ALL_REALMS:
            for snap in live_ah_db.get_all_latest_snapshots(realm):
                iid = snap["item_id"]
                if str(iid) not in existing:
                    if iid >= MIDNIGHT_MIN_ID:
                        midnight_ids.append(iid)
                    else:
                        legacy_ids.append(iid)

        def _dedup(ids: list[int]) -> list[int]:
            seen: set[int] = set()
            result = []
            for iid in ids:
                if iid not in seen:
                    seen.add(iid)
                    result.append(iid)
            return result

        rebuilt = False
        if midnight_ids:
            blizzard_api.prefetch_item_names(_dedup(midnight_ids), max_new=200, delay=0.05)
            rebuilt = True
        if legacy_ids:
            # Resolve pre-Midnight items with unknown names (e.g. Pet Cages, old world drops)
            blizzard_api.prefetch_item_names(_dedup(legacy_ids), max_new=50, delay=0.05)
            rebuilt = True
        if rebuilt:
            quality_tiers.rebuild_tier_map()

        # Backfill class info for items already named but lacking class data.
        all_live_ids: list[int] = []
        for realm in ALL_REALMS:
            for snap in live_ah_db.get_all_latest_snapshots(realm):
                iid = snap["item_id"]
                if iid >= MIDNIGHT_MIN_ID:
                    all_live_ids.append(iid)
        blizzard_api.prefetch_item_classes(all_live_ids, max_new=200, delay=0.05)
    except Exception as exc:
        print(f"  [name prefetch] warning: {exc}")


# ---------------------------------------------------------------------------
# Build dashboard data payload
# ---------------------------------------------------------------------------

def build_data() -> dict:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    records       = raw["records"]
    names         = load_item_names()
    market_values = raw.get("market_values", {})

    # Resolve item names for all live AH items not yet in the cache,
    # then reload so every tab gets real names.
    _prefetch_dashboard_names(names)
    names = load_item_names()

    # Build live AH lookup dict for reagents tab (full Malfurion data, not filtered)
    try:
        live_ah_db.init_db()
        _malf_snaps = live_ah_db.get_all_latest_snapshots(PRIMARY_REALM)
        live_malfurion_dict: dict[tuple, dict] = {
            (s["item_id"], s.get("quality_tier", "")): s for s in _malf_snaps
        }
    except Exception:
        live_malfurion_dict = {}

    profit_stats      = build_profit_stats(records)
    arbitrage         = build_live_arbitrage(names)
    repricing         = build_repricing(records)
    stop_buying_stats = build_stop_buying(profit_stats, names)
    reagent_rows      = build_reagents(records, names, market_values, live_malfurion_dict)
    live_ah           = build_live_ah_data(records, names)

    def enrich(s: dict) -> dict | None:
        iid = s["item_id"]
        if str(iid) not in names:
            return None  # suppress unresolved items from all tabs
        return {**s, "item_name": names[str(iid)], "quality_tier": s.get("quality_tier", "")}

    profit_rows      = [r for r in (enrich(s) for s in profit_stats)      if r is not None]
    arb_rows         = arbitrage   # already contains item_name from build_live_arbitrage
    reprice_rows     = [r for r in (enrich(r) for r in repricing)          if r is not None]
    stop_buying_rows = [r for r in (enrich(s) for s in stop_buying_stats)  if r is not None]

    profitable  = [s for s in profit_rows if s["profit_per_item"] > 0]
    best_flip   = profit_rows[0]      if profit_rows      else None
    best_arb    = arb_rows[0]         if arb_rows         else None
    worst_sb    = stop_buying_rows[0] if stop_buying_rows else None
    total_pot   = sum(max(0, s["profit_per_item"]) for s in profit_rows)
    total_lost  = sum(s["total_gold_lost"] for s in stop_buying_rows)

    malf_live = live_ah["by_realm"].get(PRIMARY_REALM, [])
    live_item_count = live_ah.get("total_counts", {}).get(PRIMARY_REALM, len(malf_live))

    return {
        "meta": {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "realm": PRIMARY_REALM,
            "total_records": len(records),
            "source_file": str(DATA_FILE),
        },
        "summary": {
            "total_profitable":    len(profitable),
            "best_flip_name":      best_flip["item_name"] if best_flip else "—",
            "best_flip_profit":    best_flip["profit_per_item"] if best_flip else 0,
            "best_arb_name":       best_arb["item_name"] if best_arb else "—",
            "best_arb_profit":     best_arb["net_profit"] if best_arb else 0,
            "total_potential":     total_pot,
            "arb_count":           len(arb_rows),
            "reprice_count":       len(reprice_rows),
            "stop_buying_count":   len(stop_buying_rows),
            "total_gold_lost":     total_lost,
            "worst_sb_name":       worst_sb["item_name"] if worst_sb else "—",
            "worst_sb_loss":       worst_sb["total_gold_lost"] if worst_sb else 0,
            "reagent_count":       len(reagent_rows),
            "live_ah_item_count":  live_item_count,
            "live_ah_updated":     live_ah.get("last_updated"),
        },
        "profit":      profit_rows,
        "arbitrage":   arb_rows,
        "repricing":   reprice_rows,
        "stop_buying": stop_buying_rows,
        "reagents":    reagent_rows,
        "live_ah":     live_ah,
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TSM Auction Dashboard</title>
<style>
:root {
  --bg:        #0d1117;
  --surface:   #161b22;
  --surface2:  #21262d;
  --border:    #30363d;
  --text:      #e6edf3;
  --muted:     #8b949e;
  --gold:      #f6c90e;
  --gold-dim:  #9a7d0a;
  --green:     #3fb950;
  --green-bg:  rgba(63,185,80,.12);
  --yellow:    #d29922;
  --yellow-bg: rgba(210,153,34,.12);
  --red:       #f85149;
  --red-bg:    rgba(248,81,73,.12);
  --blue:      #58a6ff;
  --radius:    8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 'Segoe UI', system-ui, sans-serif; }

/* ---- Header ---- */
.header {
  background: linear-gradient(135deg, #1a1200 0%, #161b22 60%);
  border-bottom: 1px solid var(--gold-dim);
  padding: 18px 28px;
  display: flex; align-items: center; justify-content: space-between;
}
.header h1 { font-size: 1.4rem; color: var(--gold); letter-spacing: .04em; }
.header h1 span { color: var(--muted); font-weight: 400; font-size: 1rem; margin-left: 10px; }
.header .meta { text-align: right; color: var(--muted); font-size: .8rem; line-height: 1.8; }
.header .meta strong { color: var(--text); }

/* ---- Summary cards ---- */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px; padding: 20px 28px;
  border-bottom: 1px solid var(--border);
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
}
.card .label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.card .value { font-size: 1.3rem; font-weight: 700; color: var(--gold); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card .sub   { font-size: .78rem; color: var(--muted); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ---- Tabs ---- */
.tabs { display: flex; gap: 0; padding: 0 28px; border-bottom: 1px solid var(--border); background: var(--surface); }
.tab {
  padding: 12px 22px; cursor: pointer; border-bottom: 2px solid transparent;
  color: var(--muted); font-weight: 500; transition: color .15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--gold); border-bottom-color: var(--gold); }

/* ---- Table container ---- */
.panel { display: none; padding: 24px 28px; }
.panel.active { display: block; }

.table-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: .83rem; }
thead th {
  background: var(--surface2);
  padding: 10px 14px; text-align: left;
  color: var(--muted); font-weight: 600;
  white-space: nowrap; cursor: pointer;
  user-select: none; position: sticky; top: 0;
  border-bottom: 1px solid var(--border);
}
thead th:hover { color: var(--text); }
thead th.sorted { color: var(--gold); }
thead th .sort-icon { margin-left: 4px; opacity: .7; }

tbody tr { border-bottom: 1px solid var(--border); transition: background .1s; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--surface2) !important; }
tbody td { padding: 9px 14px; vertical-align: middle; white-space: nowrap; }

/* Row colour bands */
.row-green  { background: var(--green-bg); }
.row-yellow { background: var(--yellow-bg); }
.row-red    { background: var(--red-bg); }

/* Value colours */
.pos    { color: var(--green); font-weight: 600; }
.neg    { color: var(--red);   font-weight: 600; }
.warn   { color: var(--yellow);}
.hi-pct { color: var(--gold);  font-weight: 700; }
.muted  { color: var(--muted); }
.realm-tag {
  display: inline-block; padding: 1px 7px;
  border-radius: 4px; font-size: .75rem; font-weight: 600;
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--blue);
}

/* ---- Danger cards (stop-buying) ---- */
.card-danger { border-color: rgba(248,81,73,.4); background: rgba(248,81,73,.06); }
.card-danger .value { color: var(--red); }
.card-danger .label { color: rgba(248,81,73,.8); }

/* ---- Loss value cells ---- */
.loss { color: var(--red); font-weight: 600; }

/* ---- Realm selector buttons (Live AH tab) ---- */
.realm-btn {
  padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: .82rem;
  border: 1px solid var(--border); background: var(--surface2); color: var(--muted);
  transition: all .15s;
}
.realm-btn:hover { color: var(--text); border-color: var(--muted); }
.realm-btn.active { background: var(--surface); color: var(--green); border-color: var(--green); font-weight: 600; }

/* ---- Clickable cards ---- */
.card[data-panel] { cursor: pointer; transition: background .15s, border-color .15s; }
.card[data-panel]:hover { background: #1e2530; border-color: var(--muted); }

/* ---- Quality tier badges ---- */
.qt { display: inline-block; font-size: .72rem; font-weight: 700; padding: 1px 5px; border-radius: 4px; margin-left: 5px; vertical-align: middle; }
.qt-T1 { background: rgba(139,148,158,.15); color: var(--muted); border: 1px solid rgba(139,148,158,.3); }
.qt-T2 { background: rgba(88,166,255,.12); color: #58a6ff; border: 1px solid rgba(88,166,255,.3); }
.qt-T3 { background: rgba(63,185,80,.12); color: var(--green); border: 1px solid rgba(63,185,80,.3); }
.qt-T4 { background: rgba(188,140,255,.12); color: #bc8cff; border: 1px solid rgba(188,140,255,.3); }
.qt-T5 { background: rgba(246,201,14,.12); color: var(--gold); border: 1px solid rgba(246,201,14,.3); }

/* ---- Footer ---- */
.footer { text-align: center; padding: 18px; color: var(--muted); font-size: .75rem; border-top: 1px solid var(--border); }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>⚔ TSM Auction Dashboard <span id="realm-name"></span></h1>
  </div>
  <div class="meta">
    <div>Last updated: <strong id="last-updated"></strong></div>
    <div>Source records: <strong id="total-records"></strong></div>
  </div>
</div>

<div class="cards" id="cards"></div>

<div class="tabs">
  <div class="tab active" data-panel="profit">Profit Opportunities</div>
  <div class="tab" data-panel="arbitrage">Cross-Realm Arbitrage</div>
  <div class="tab" data-panel="repricing">Repricing Concerns</div>
  <div class="tab" data-panel="stop-buying" style="color:var(--red)">⛔ Stop Buying</div>
  <div class="tab" data-panel="reagents" style="color:var(--blue)">⚗ Reagents</div>
  <div class="tab" data-panel="live-ah" style="color:var(--green)">📡 Live AH</div>
  <div class="tab" data-panel="top-opps" style="color:var(--gold)">⭐ Top Opportunities</div>
</div>

<div id="profit" class="panel active"></div>
<div id="arbitrage" class="panel"></div>
<div id="repricing" class="panel"></div>
<div id="stop-buying" class="panel"></div>
<div id="reagents" class="panel"></div>
<div id="live-ah" class="panel"></div>
<div id="top-opps" class="panel"></div>

<div class="footer">Generated from <span id="source-file"></span></div>

<script>
const DATA = __DATA_JSON__;

// ---- Utilities ----
const g = id => document.getElementById(id);
const fmt_g  = v => v == null ? '—' : (Math.abs(v) < 10 ? v.toFixed(2) : Math.round(v).toLocaleString()) + 'g';
const fmt_pct = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

// ---- Quality badge helper ----
function qtBadge(qt) {
  if (!qt) return '';
  return `<span class="qt qt-${qt}">${qt}</span>`;
}
function itemCell(row) {
  return (row.item_name || '—') + qtBadge(row.quality_tier || '');
}

function rowClass(val, type) {
  if (type === 'margin') {
    if (val >= 50)  return 'row-green';
    if (val >= 20)  return 'row-yellow';
    if (val < 0)    return 'row-red';
    return '';
  }
  if (type === 'fail') {
    if (val >= 80)  return 'row-red';
    if (val >= 50)  return 'row-yellow';
    return '';
  }
  if (type === 'spread') {
    if (val >= 50) return 'row-green';
    if (val >= 20) return 'row-yellow';
    return '';
  }
  return '';
}

function cellClass(val, type) {
  if (type === 'profit') return val > 0 ? 'pos' : val < 0 ? 'neg' : '';
  if (type === 'margin') {
    if (val >= 50) return 'hi-pct';
    if (val >= 20) return 'pos';
    if (val < 0)   return 'neg';
    return 'muted';
  }
  if (type === 'fail') {
    if (val >= 80) return 'neg';
    if (val >= 50) return 'warn';
    return '';
  }
  return '';
}

// ---- Sortable table factory ----
function makeTable(containerId, columns, rows, colorKey, colorType) {
  const container = g(containerId);
  let sortCol = 0, sortAsc = false;

  function render() {
    const sorted = [...rows].sort((a, b) => {
      const col = columns[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      const cmp = col.num
        ? (va - vb)
        : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = columns.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>') : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      return `<th${cls} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const tbody = sorted.map(row => {
      const colorVal = row[colorKey];
      const rc = rowClass(colorVal, colorType);
      const cells = columns.map(c => {
        let raw = row[c.key];
        let display = raw;
        let cc = '';
        if (c.format === 'gold')   { display = fmt_g(raw);   cc = cellClass(raw, 'profit'); }
        if (c.format === 'pct')    { display = fmt_pct(raw);  cc = cellClass(raw, c.cctype || 'margin'); }
        if (c.format === 'int')    { display = raw ?? '—'; }
        if (c.format === 'realm')  { display = raw ? `<span class="realm-tag">${raw}</span>` : '—'; }
        if (c.format === 'src')    { display = raw ? `<span class="muted">${raw}</span>` : ''; }
        if (c.format === 'fail')   { display = fmt_pct(raw);  cc = cellClass(raw, 'fail'); }
        if (c.format === 'opt_g')  { display = raw != null ? fmt_g(raw) : '<span class="muted">—</span>'; }
        if (c.format === 'item')   { display = itemCell(row); }
        return `<td${cc ? ` class="${cc}"` : ''}>${display}</td>`;
      }).join('');
      return `<tr class="${rc}">${cells}</tr>`;
    }).join('');

    container.innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${tbody}</tbody>
    </table></div>`;

    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc;
        else { sortCol = ci; sortAsc = false; }
        render();
      });
    });
  }
  render();
}

// ---- Populate header ----
function initMeta() {
  const m = DATA.meta;
  g('realm-name').textContent = m.realm;
  const dt = new Date(m.generated_at);
  g('last-updated').textContent = dt.toLocaleString();
  g('total-records').textContent = m.total_records.toLocaleString();
  g('source-file').textContent = m.source_file;
}

// ---- Summary cards ----
function initCards() {
  const s = DATA.summary;
  const liveUpdated = s.live_ah_updated
    ? new Date(s.live_ah_updated).toLocaleTimeString() : 'no data';
  const cards = [
    { label: 'Profitable Items',    value: s.total_profitable,        sub: `on ${DATA.meta.realm} · Bankarang`,     danger: false, panel: 'profit' },
    { label: 'Best Single Flip',    value: fmt_g(s.best_flip_profit), sub: s.best_flip_name,                        danger: false, panel: 'profit' },
    { label: 'Total Potential',     value: fmt_g(s.total_potential),  sub: 'if all flips executed',                 danger: false, panel: 'profit' },
    { label: 'Best Arbitrage',      value: fmt_g(s.best_arb_profit),  sub: s.best_arb_name + ' · live AH',         danger: false, panel: 'arbitrage' },
    { label: 'Arbitrage Opps',      value: s.arb_count,               sub: 'cross-realm · spread > 20% after cut', danger: false, panel: 'arbitrage' },
    { label: 'Repricing Concerns',  value: s.reprice_count,           sub: 'cancelled or expired',                  danger: false, panel: 'repricing' },
    { label: '⛔ Stop Buying Items', value: s.stop_buying_count,       sub: 'Bankarang · losing gold per flip',     danger: true,  panel: 'stop-buying' },
    { label: '📡 Live AH Items',    value: s.live_ah_item_count || 0, sub: `updated ${liveUpdated}`,                danger: false, panel: 'live-ah' },
  ];
  g('cards').innerHTML = cards.map(c => {
    const cls = c.danger ? 'card card-danger' : 'card';
    const val = typeof c.value === 'number' && !String(c.value).includes('g')
      ? c.value.toLocaleString() : c.value;
    return `<div class="${cls}" data-panel="${c.panel}" title="Go to ${c.label}">
      <div class="label">${c.label}</div>
      <div class="value">${val}</div>
      <div class="sub">${c.sub}</div>
    </div>`;
  }).join('');

  g('cards').addEventListener('click', e => {
    const card = e.target.closest('[data-panel]');
    if (!card) return;
    const panel = card.dataset.panel;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    const tab = document.querySelector(`.tab[data-panel="${panel}"]`);
    if (tab) tab.classList.add('active');
    const panelEl = g(panel);
    if (panelEl) panelEl.classList.add('active');
  });
}

// ---- Tabs ----
function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      g(tab.dataset.panel).classList.add('active');
    });
  });
}

// ---- Profit table ----
function initProfit() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false, format: 'item' },
    { key: 'avg_buy',         label: 'Avg Buy',       num: true,  format: 'gold' },
    { key: 'avg_sell',        label: 'Avg Sell',      num: true,  format: 'gold' },
    { key: 'profit_per_item', label: 'Profit / Item', num: true,  format: 'gold' },
    { key: 'margin_pct',      label: 'Margin %',      num: true,  format: 'pct' },
    { key: 'buy_txns',        label: 'Buy Txns',      num: true,  format: 'int' },
    { key: 'sell_txns',       label: 'Sell Txns',     num: true,  format: 'int' },
  ];
  makeTable('profit', cols, DATA.profit, 'margin_pct', 'margin');
}

// ---- Arbitrage table ----
function initArbitrage() {
  const container = g('arbitrage');
  const rows = DATA.arbitrage || [];

  if (!rows.length) {
    container.innerHTML = '<div style="padding:32px;text-align:center;color:var(--muted)">No cross-realm arbitrage opportunities found. Live AH data may not be populated — run refresh_live_ah.sh.</div>';
    return;
  }

  const note = `<div style="padding:8px 14px;font-size:.8rem;color:var(--muted)">
    📡 Live AH prices · Net Profit = (Sell Realm Min × 0.95) − Buy Realm Min · Showing top ${rows.length} of up to 100 opportunities
  </div>`;

  const cols = [
    { key: 'item_name',      label: 'Item',                num: false, format: 'item' },
    { key: 'category',       label: 'Category',            num: false },
    { key: 'buy_realm',      label: 'Buy Realm',           num: false, format: 'realm' },
    { key: 'buy_at',         label: 'Buy At',              num: true,  format: 'gold' },
    { key: 'sell_realm',     label: 'Sell Realm',          num: false, format: 'realm' },
    { key: 'sell_at',        label: 'Sell At',             num: true,  format: 'gold' },
    { key: 'net_profit',     label: 'Net Profit / Unit',   num: true,  format: 'gold' },
    { key: 'spread_pct',     label: 'Spread %',            num: true,  format: 'pct', cctype: 'spread' },
    { key: 'listing_count',  label: 'Listings Available',  num: true,  format: 'int' },
    { key: 'total_quantity', label: 'Total Qty Available', num: true,  format: 'int' },
  ];

  // Reuse makeTable but inject the note above it
  const scratch = document.createElement('div');
  scratch.id = '_arb_scratch';
  document.body.appendChild(scratch);
  makeTable('_arb_scratch', cols, rows, 'spread_pct', 'spread');
  container.innerHTML = note + scratch.innerHTML;
  scratch.remove();

  // Re-attach sort listeners
  let sortCol = 6, sortAsc = false;
  function rerender() {
    const sorted = [...rows].sort((a, b) => {
      const col = cols[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      const cmp = col.num ? (va - vb) : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });
    function rowCls(v) {
      if (v >= 50) return 'row-green';
      if (v >= 20) return 'row-yellow';
      return '';
    }
    const tbody = sorted.map(row => {
      const rc = rowCls(row.spread_pct);
      const cells = cols.map(c => {
        const raw = row[c.key];
        let display = raw, cc = '';
        if (c.format === 'item')  { display = itemCell(row); }
        if (c.format === 'gold')  { display = fmt_g(raw); cc = raw > 0 ? 'pos' : raw < 0 ? 'neg' : ''; }
        if (c.format === 'pct')   { display = fmt_pct(raw); cc = raw >= 50 ? 'hi-pct' : raw >= 20 ? 'pos' : 'muted'; }
        if (c.format === 'int')   { display = raw != null ? raw.toLocaleString() : '—'; }
        if (c.format === 'realm') { display = raw ? `<span class="realm-tag">${raw}</span>` : '—'; }
        return `<td${cc ? ` class="${cc}"` : ''}>${display}</td>`;
      }).join('');
      return `<tr class="${rc}">${cells}</tr>`;
    }).join('');

    const thead = cols.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>') : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      return `<th${cls} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    container.innerHTML = `${note}<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table></div>`;
    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc; else { sortCol = ci; sortAsc = false; }
        rerender();
      });
    });
  }
  rerender();
}

// ---- Repricing table ----
function initRepricing() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false, format: 'item' },
    { key: 'cancels',         label: 'Cancels',       num: true,  format: 'int' },
    { key: 'expirations',     label: 'Expired',       num: true,  format: 'int' },
    { key: 'failed_qty',      label: 'Failed Qty',    num: true,  format: 'int' },
    { key: 'sell_successes',  label: 'Sold OK',       num: true,  format: 'int' },
    { key: 'failure_rate',    label: 'Fail Rate',     num: true,  format: 'fail' },
    { key: 'avg_buy_price',   label: 'Avg Buy',       num: true,  format: 'opt_g' },
  ];
  makeTable('repricing', cols, DATA.repricing, 'failure_rate', 'fail');
}

// ---- Stop Buying table ----
function initStopBuying() {
  const cols = [
    { key: 'item_name',       label: 'Item',          num: false, format: 'item' },
    { key: 'avg_buy',         label: 'Avg Buy',       num: true,  format: 'gold' },
    { key: 'avg_sell',        label: 'Avg Sell',      num: true,  format: 'gold' },
    { key: 'loss_per_item',   label: 'Loss / Item',   num: true,  format: 'loss_g' },
    { key: 'loss_pct',        label: 'Loss %',        num: true,  format: 'loss_pct' },
    { key: 'buy_txns',        label: 'Buy Txns',      num: true,  format: 'int' },
    { key: 'total_gold_lost', label: 'Total Lost',    num: true,  format: 'loss_g' },
  ];
  // Extend makeTable's format handling inline via a wrapper
  const container = g('stop-buying');
  let sortCol = 6, sortAsc = false; // default: worst total lost first

  function render() {
    const rows = DATA.stop_buying;
    const sorted = [...rows].sort((a, b) => {
      const col = cols[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      const cmp = col.num ? (va - vb) : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = cols.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol
        ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>')
        : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      return `<th${cls} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const tbody = sorted.map(row => {
      const cells = cols.map(c => {
        const raw = row[c.key];
        if (c.format === 'item')    return `<td>${itemCell(row)}</td>`;
        if (c.format === 'loss_g')  return `<td class="loss">${raw != null ? fmt_g(raw) : '—'}</td>`;
        if (c.format === 'loss_pct') return `<td class="loss">-${raw != null ? raw.toFixed(1)+'%' : '—'}</td>`;
        if (c.format === 'gold')    return `<td>${raw != null ? fmt_g(raw) : '—'}</td>`;
        if (c.format === 'int')     return `<td>${raw ?? '—'}</td>`;
        return `<td>${raw ?? '—'}</td>`;
      }).join('');
      return `<tr class="row-red">${cells}</tr>`;
    }).join('');

    const empty = rows.length === 0
      ? '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--green)">✓ No loss-making items — great job!</td></tr>'
      : tbody;

    container.innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${empty}</tbody>
    </table></div>`;

    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc;
        else { sortCol = ci; sortAsc = false; }
        render();
      });
    });
  }
  render();
}

// ---- Reagents table ----
function initReagents() {
  const container = g('reagents');
  const rows = DATA.reagents || [];

  if (!rows.length) {
    container.innerHTML = '<div style="padding:32px;text-align:center;color:var(--muted)">No Midnight-era reagents found in transaction history.</div>';
    return;
  }

  const cols = [
    { key: 'item_name',          label: 'Item',               num: false, format: 'item' },
    { key: 'live_ah_min',        label: 'Live AH Min',        num: true,  format: 'gold_live' },
    { key: 'live_ah_avg',        label: 'Live AH Avg',        num: true,  format: 'gold_opt' },
    { key: 'avg_buy',            label: 'My Avg Buy',         num: true,  format: 'gold_opt' },
    { key: 'avg_sell',           label: 'My Avg Sell',        num: true,  format: 'gold_opt' },
    { key: 'profit_potential',   label: 'Profit Potential',   num: true,  format: 'profit_g' },
    { key: 'transaction_count',  label: 'Txns',               num: true,  format: 'int' },
    { key: 'buy_at',             label: 'Buy At',             num: true,  format: 'gold_live', tip: 'Buy any listing at or below this price' },
    { key: 'net_if_sold_at_min', label: 'Net If Sold At Min', num: true,  format: 'profit_g',  tip: 'Estimated profit per unit after the 5% AH cut, if you re-listed at the current minimum (revenue = Live AH Min × 0.95, cost = your avg buy)' },
  ];

  let sortCol = 5, sortAsc = false;

  function rowBg(row) {
    const pp = row.profit_potential;
    if (pp == null) return '';
    if (pp > 5)    return 'row-green';
    if (pp > 0)    return 'row-yellow';
    return 'row-red';
  }

  function render() {
    const sorted = [...rows].sort((a, b) => {
      const col = cols[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      const cmp = col.num ? (va - vb) : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = cols.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol
        ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>')
        : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      const tip = c.tip ? ` title="${c.tip}"` : '';
      return `<th${cls}${tip} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const tbody = sorted.map(row => {
      const rc = rowBg(row);
      const cells = cols.map(c => {
        const v = row[c.key];
        if (c.format === 'item')      return `<td>${itemCell(row)}</td>`;
        if (c.format === 'gold_live') return `<td>${v != null ? fmt_g(v) : '<span class="muted">No AH data</span>'}</td>`;
        if (c.format === 'gold_opt')  return `<td>${v != null ? fmt_g(v) : '<span class="muted">—</span>'}</td>`;
        if (c.format === 'profit_g') {
          if (v == null) return '<td class="muted">—</td>';
          const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
          return `<td class="${cls}">${(v >= 0 ? '+' : '') + fmt_g(v)}</td>`;
        }
        if (c.format === 'int') return `<td>${v ?? '—'}</td>`;
        return `<td>${v ?? '—'}</td>`;
      }).join('');
      return `<tr class="${rc}">${cells}</tr>`;
    }).join('');

    const hasLive = rows.some(r => r.live_ah_min != null);
    const note = hasLive
      ? '<div style="padding:8px 14px;font-size:.8rem;color:var(--muted)">📡 Prices from live Blizzard AH data · Profit = Live AH Min − Your Avg Buy</div>'
      : '<div style="padding:8px 14px;font-size:.8rem;color:var(--muted)">No live AH data yet — run refresh_live_ah.sh to populate.</div>';

    container.innerHTML = `${note}<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${tbody}</tbody>
    </table></div>`;

    container.querySelectorAll('thead th').forEach(th => {
      th.addEventListener('click', () => {
        const ci = +th.dataset.col;
        if (ci === sortCol) sortAsc = !sortAsc;
        else { sortCol = ci; sortAsc = false; }
        render();
      });
    });
  }
  render();
}

// ---- Live AH tab ----
function initLiveAH() {
  const container = g('live-ah');
  const liveData  = DATA.live_ah || {};
  const realms    = liveData.realms || [];
  const byRealm   = liveData.by_realm || {};
  const totals    = liveData.total_counts || {};

  if (!realms.length) {
    container.innerHTML = '<div style="padding:32px;text-align:center;color:var(--muted)">No Live AH data available. Run refresh_live_ah.sh to populate.</div>';
    return;
  }

  let activeRealm = realms[0];
  let sortCol = 9, sortAsc = false;  // default: spread_pct desc

  const TOOLTIPS = {
    'live_ah_min':          'Cheapest single listing currently on the AH',
    'live_ah_avg':          'Average price across all current listings',
    'total_quantity':       'Total units available across all listings',
    'listing_count':        'Number of separate auctions on the AH right now',
    'bankarang_avg_buy':    "Bankarang's weighted average buy price (Malfurion only)",
    'bankarang_avg_sell':   "Bankarang's weighted average sell price (Malfurion only)",
    'buy_txns':             'Number of Bankarang buy transactions',
    'spread_pct':           'Live AH vs Bankarang avg — positive = buy opportunity, negative = price above avg sell',
    'buy_at':               'Buy any listing at or below this price',
    'net_if_sold_at_min':   "Estimated profit per unit after the 5% AH cut, if you re-listed at the current minimum (revenue = Live AH Min × 0.95, cost = Bankarang's avg buy)",
  };

  const cols = [
    { key: 'item_name',          label: 'Item',               num: false, fmt: 'item' },
    { key: 'category',           label: 'Category',           num: false },
    { key: 'live_ah_min',        label: 'Live Min',           num: true,  fmt: 'gold' },
    { key: 'live_ah_avg',        label: 'Live Avg',           num: true,  fmt: 'gold' },
    { key: 'total_quantity',     label: 'Total Qty',          num: true,  fmt: 'int' },
    { key: 'listing_count',      label: 'Listings',           num: true,  fmt: 'int' },
    { key: 'bankarang_avg_buy',  label: 'Bnkrng Buy',         num: true,  fmt: 'gold_opt' },
    { key: 'bankarang_avg_sell', label: 'Bnkrng Sell',        num: true,  fmt: 'gold_opt' },
    { key: 'buy_txns',           label: 'Buy Txns',           num: true,  fmt: 'int_opt' },
    { key: 'spread_pct',         label: 'Spread %',           num: true,  fmt: 'spread' },
    { key: 'buy_at',             label: 'Buy At',             num: true,  fmt: 'gold' },
    { key: 'net_if_sold_at_min', label: 'Net If Sold At Min', num: true,  fmt: 'profit_net' },
    { key: 'last_updated',       label: 'Updated',            num: false, fmt: 'ts' },
  ];

  function rowBg(row) {
    const liveMin = row.live_ah_min;
    const avgBuy  = row.bankarang_avg_buy;
    const avgSell = row.bankarang_avg_sell;
    // Malfurion: Bankarang-based coloring
    if (avgBuy  != null && liveMin < avgBuy  * 0.80) return 'row-green';
    if (avgSell != null && liveMin > avgSell * 1.20) return 'row-red';
    if (row.spread_pct != null && row.spread_pct > 0) return 'row-yellow';
    // Non-Malfurion: cross-realm average coloring
    const mrAvg = row.multi_realm_avg_min;
    if (mrAvg != null && mrAvg > 0) {
      if (liveMin < mrAvg * 0.80) return 'row-green';
      if (liveMin > mrAvg * 1.20) return 'row-red';
    }
    return '';
  }

  function renderTable() {
    const rows = byRealm[activeRealm] || [];
    const sorted = [...rows].sort((a, b) => {
      const col = cols[sortCol];
      const va = a[col.key], vb = b[col.key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1; if (vb == null) return -1;
      const cmp = col.num ? (va - vb) : String(va).localeCompare(String(vb));
      return sortAsc ? cmp : -cmp;
    });

    const thead = cols.map((c, i) => {
      const cls = i === sortCol ? ' class="sorted"' : '';
      const icon = i === sortCol
        ? (sortAsc ? ' <span class="sort-icon">↑</span>' : ' <span class="sort-icon">↓</span>')
        : ' <span class="sort-icon" style="opacity:.25">↕</span>';
      const tip = TOOLTIPS[c.key] ? ` title="${TOOLTIPS[c.key]}"` : '';
      return `<th${cls}${tip} data-col="${i}">${c.label}${icon}</th>`;
    }).join('');

    const isMainRealm = activeRealm === 'Malfurion';

    const tbody = sorted.map(row => {
      const rc = rowBg(row);
      const cells = cols.map(c => {
        const v = row[c.key];
        switch (c.fmt) {
          case 'item':     return `<td>${itemCell(row)}</td>`;
          case 'gold':     return `<td>${fmt_g(v)}</td>`;
          case 'gold_opt': return `<td>${v != null ? fmt_g(v) : '<span class="muted">—</span>'}</td>`;
          case 'int':      return `<td>${v != null ? v.toLocaleString() : '—'}</td>`;
          case 'int_opt':  return `<td class="muted">${v || '—'}</td>`;
          case 'spread': {
            if (v == null) return '<td class="muted">—</td>';
            const cls = v > 20 ? 'pos' : v < -20 ? 'neg' : 'muted';
            return `<td class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(1)}%</td>`;
          }
          case 'profit_net': {
            if (v == null) return '<td class="muted">—</td>';
            const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
            return `<td class="${cls}">${(v >= 0 ? '+' : '') + fmt_g(v)}</td>`;
          }
          case 'ts': {
            if (!v) return '<td class="muted">—</td>';
            const d = new Date(v);
            return `<td class="muted" title="${v}">${d.toLocaleTimeString()}</td>`;
          }
          default: return `<td>${v ?? '—'}</td>`;
        }
      }).join('');
      return `<tr class="${rc}">${cells}</tr>`;
    }).join('');

    const empty = sorted.length === 0
      ? '<tr><td colspan="11" style="text-align:center;padding:24px;color:var(--muted)">No data for this realm</td></tr>'
      : tbody;

    const dispCount  = (byRealm[activeRealm] || []).length;
    const totalCount = totals[activeRealm] || dispCount;
    const note = activeRealm !== 'Malfurion'
      ? `<div style="padding:8px 14px;font-size:.8rem;color:var(--muted)">🟢 Green = cheapest across realms (&gt;20% below 5-realm avg) · 🔴 Red = most expensive (&gt;20% above avg) · Showing top ${dispCount} of ${totalCount.toLocaleString()} items by listing volume · Bankarang Buy/Sell columns are Malfurion-only</div>`
      : `<div style="padding:8px 14px;font-size:.8rem;color:var(--muted)">🟢 Green = live price &gt;20% below Bankarang avg buy · 🔴 Red = live price &gt;20% above Bankarang avg sell · Showing ${dispCount} items with Bankarang history (of ${totalCount.toLocaleString()} on AH)</div>`;

    return `${note}<div class="table-wrap"><table>
      <thead><tr>${thead}</tr></thead>
      <tbody>${empty}</tbody>
    </table></div>`;
  }

  function render() {
    const btnRow = realms.map(r =>
      `<button class="realm-btn${r === activeRealm ? ' active' : ''}" data-realm="${r}">${r}</button>`
    ).join('');

    container.innerHTML = `
      <div style="padding:16px 28px 8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span style="color:var(--muted);font-size:.85rem;margin-right:4px;">Realm:</span>
        ${btnRow}
      </div>
      <div style="padding:0 28px;" id="live-ah-table"></div>`;

    container.querySelectorAll('.realm-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeRealm = btn.dataset.realm;
        // Malfurion: sort by spread (most actionable); others: sort by listing_count
        sortCol = activeRealm === 'Malfurion' ? 9 : 5;
        sortAsc = false;
        render();
      });
    });

    const tableDiv = g('live-ah-table');
    tableDiv.innerHTML = renderTable();

    // Use event delegation to avoid re-attaching listeners after each re-render
    tableDiv.addEventListener('click', e => {
      const th = e.target.closest('thead th');
      if (!th) return;
      const ci = +th.dataset.col;
      if (ci === sortCol) sortAsc = !sortAsc;
      else { sortCol = ci; sortAsc = false; }
      tableDiv.innerHTML = renderTable();
    });
  }

  render();
}

// ---- Top Opportunities tab ----
function initTopOpportunities() {
  const container = g('top-opps');
  const malf = (DATA.live_ah && DATA.live_ah.by_realm && DATA.live_ah.by_realm['Malfurion']) || [];

  // TOP 25 BUYS: live_ah_min well below Bankarang avg sell → buy now, sell later
  const buys = malf
    .filter(r => r.live_ah_min != null && r.bankarang_avg_sell != null && r.bankarang_avg_sell > r.live_ah_min)
    .map(r => ({...r, opp_profit: r.bankarang_avg_sell - r.live_ah_min}))
    .sort((a, b) => b.opp_profit - a.opp_profit)
    .slice(0, 25);

  // TOP 25 SELLS: live_ah_min well above Bankarang avg buy → list now while price is high
  const sells = malf
    .filter(r => r.live_ah_min != null && r.bankarang_avg_buy != null && r.live_ah_min > r.bankarang_avg_buy)
    .map(r => ({...r, opp_premium: r.live_ah_min - r.bankarang_avg_buy}))
    .sort((a, b) => b.opp_premium - a.opp_premium)
    .slice(0, 25);

  function rowOppClass(rank) {
    if (rank <= 5)  return 'row-green';
    if (rank <= 15) return 'row-yellow';
    return '';
  }

  function renderBuys() {
    if (!buys.length) return '<div style="padding:20px;color:var(--muted)">No buy signals right now — no items with live AH min below Bankarang avg sell.</div>';
    const thead = `<tr>
      <th style="width:48px">Rank</th><th>Item Name</th>
      <th title="Cheapest current AH listing">Live AH Min</th>
      <th title="Bankarang weighted avg sell price">Bankarang's Avg Sell</th>
      <th title="Avg sell minus live min">Profit / Item</th>
      <th title="Number of separate auctions right now">Listings Available</th>
      <th title="Bankarang buy transactions (confidence)">Transactions</th>
    </tr>`;
    const tbody = buys.map((r, i) => {
      const rank = i + 1;
      return `<tr class="${rowOppClass(rank)}">
        <td class="muted" style="text-align:center">${rank}</td>
        <td>${itemCell(r)}</td>
        <td>${fmt_g(r.live_ah_min)}</td>
        <td>${fmt_g(r.bankarang_avg_sell)}</td>
        <td class="pos">+${fmt_g(r.opp_profit)}</td>
        <td>${r.listing_count != null ? r.listing_count.toLocaleString() : '—'}</td>
        <td class="muted">${r.buy_txns || '—'}</td>
      </tr>`;
    }).join('');
    return `<div class="table-wrap"><table><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`;
  }

  function renderSells() {
    if (!sells.length) return '<div style="padding:20px;color:var(--muted)">No sell signals right now — no items with live AH min above Bankarang avg buy.</div>';
    const thead = `<tr>
      <th style="width:48px">Rank</th><th>Item Name</th>
      <th title="Bankarang weighted avg buy price">Bankarang's Avg Buy</th>
      <th title="Cheapest current AH listing — list at or near this">Live AH Min</th>
      <th title="Live min minus avg buy">Premium / Item</th>
      <th title="Number of separate auctions right now (your competition)">Listings Currently Up</th>
      <th title="Bankarang sell transactions (confidence)">Transactions</th>
    </tr>`;
    const tbody = sells.map((r, i) => {
      const rank = i + 1;
      return `<tr class="${rowOppClass(rank)}">
        <td class="muted" style="text-align:center">${rank}</td>
        <td>${itemCell(r)}</td>
        <td>${fmt_g(r.bankarang_avg_buy)}</td>
        <td>${fmt_g(r.live_ah_min)}</td>
        <td class="pos">+${fmt_g(r.opp_premium)}</td>
        <td>${r.listing_count != null ? r.listing_count.toLocaleString() : '—'}</td>
        <td class="muted">${r.sell_txns || '—'}</td>
      </tr>`;
    }).join('');
    return `<div class="table-wrap"><table><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>`;
  }

  const lastUpdated = DATA.summary.live_ah_updated
    ? `Last AH snapshot: ${new Date(DATA.summary.live_ah_updated).toLocaleString()}`
    : 'No live AH data — run refresh_live_ah.sh';

  container.innerHTML = `
    <div style="padding:16px 28px 0;font-size:.82rem;color:var(--muted)">
      📡 ${lastUpdated} &nbsp;·&nbsp; 🟢 Top 5 &nbsp;·&nbsp; 🟡 Ranks 6–15 &nbsp;·&nbsp; Uncolored 16–25
    </div>
    <div style="padding:20px 28px 8px;">
      <div style="font-size:1rem;font-weight:600;color:var(--gold);margin-bottom:4px;">📈 TOP 25 BUYS <span style="font-size:.8rem;font-weight:400;color:var(--muted)">— buy now, sell later (live AH min below Bankarang avg sell)</span></div>
      ${renderBuys()}
    </div>
    <div style="padding:8px 28px 24px;">
      <div style="font-size:1rem;font-weight:600;color:var(--gold);margin-bottom:4px;">📉 TOP 25 SELLS <span style="font-size:.8rem;font-weight:400;color:var(--muted)">— list these now (live AH min above Bankarang avg buy)</span></div>
      ${renderSells()}
    </div>`;
}

// ---- Boot ----
initMeta();
initCards();
initTabs();
initProfit();
initArbitrage();
initRepricing();
initStopBuying();
initReagents();
initLiveAH();
initTopOpportunities();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public-safe sanitization
# ---------------------------------------------------------------------------

# Character names and account names that appear in the personal data.
# These are NOT embedded in the processed dashboard rows, but we sanitize
# the source_file path which contains the Linux username.
_HOME_RE = re.compile(r'/home/[^/"]+/')


def sanitize_html(html: str) -> str:
    """Replace personal path info for the public GitHub Pages build."""
    return _HOME_RE.sub('/home/Character/', html)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading {DATA_FILE}...")
    data = build_data()

    print(f"  Profit rows:      {len(data['profit'])}")
    print(f"  Stop Buying rows: {len(data['stop_buying'])}")
    print(f"  Arbitrage rows:   {len(data['arbitrage'])}")
    print(f"  Repricing rows:   {len(data['repricing'])}")
    print(f"  Reagent rows:     {len(data['reagents'])}")
    live = data.get("live_ah", {})
    totals = live.get("total_counts", {})
    for realm in live.get("realms", []):
        displayed = len(live.get("by_realm", {}).get(realm, []))
        total = totals.get(realm, displayed)
        suffix = f" (showing {displayed} with Bankarang data)" if realm == PRIMARY_REALM else f" (showing top {displayed})"
        print(f"  Live AH {realm:<12}: {total} total{suffix}")

    # Embed data — JSON is valid JS; escape </script> to prevent tag injection
    json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    json_str = json_str.replace('</script>', r'<\/script>')

    html = HTML_TEMPLATE.replace('__DATA_JSON__', json_str)
    DASHBOARD.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {DASHBOARD}")

    # --- Public dashboard (GitHub Pages) ---
    public_html = sanitize_html(html)
    PUBLIC_DASH.write_text(public_html, encoding="utf-8")
    print(f"Public dashboard written to {PUBLIC_DASH}")

    DOCS_DIR.mkdir(exist_ok=True)
    docs_index = DOCS_DIR / "index.html"
    shutil.copy2(PUBLIC_DASH, docs_index)
    print(f"Copied to {docs_index}")


if __name__ == "__main__":
    main()
