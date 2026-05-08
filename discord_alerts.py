#!/usr/bin/env python3
"""
TSM Discord Reagent Alerts

Posts a TOP 5 BUYS / TOP 5 SELLS reagent summary to a Discord channel
every 15 minutes via webhook.

Signal logic uses a layered pricing reference:
  PRIMARY:   Live AH minimum price from live_ah.db (most recent snapshot)
  SECONDARY: TSM market value (14-day weighted average, if populated)
  TERTIARY:  Bankarang's personal buy/sell averages from tsm_data.json

  BUYS:  live AH min < Bankarang avg sell * 0.80  → worth buying now (≥20% below avg sell)
  SELLS: live AH min > Bankarang avg buy  * 1.20  → list now while price is high (≥20% above avg buy)

Flipping analysis is restricted to Bankarang on Malfurion — the designated flipper.
Other characters buy reagents for crafting (not resale) and are excluded so their
crafting costs don't pollute the flip signals.

Run via systemd timer or manually:
  python3 discord_alerts.py
"""

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import bankarang_pricing
import blizzard_api
import live_ah_db
import quality_tiers

SCRIPT_DIR  = Path(__file__).parent
DATA_FILE   = Path.home() / "tsm_data.json"
NAMES_FILE  = Path(__file__).parent / "item_names.json"
LOG_FILE    = SCRIPT_DIR / "logs" / "agent.log"
ENV_FILE    = SCRIPT_DIR / ".env"

load_dotenv(ENV_FILE)

PRIMARY_REALM   = "Malfurion"
MIDNIGHT_MIN_ID = 236000
MIN_PROFIT_G    = 2.0   # minimum gold profit to appear in lists

# ---------------------------------------------------------------------------
# Bankarang filter — flipping analysis is Bankarang/Malfurion only.
# Other characters buy reagents for crafting, not resale.
# See agent.py for the full explanation.
# Future: extend to a set if more designated flippers are added.
# ---------------------------------------------------------------------------
FLIPPER = "Bankarang"

# Minimum price spread to generate a signal (20% above/below personal average)
SIGNAL_THRESHOLD_PCT = 20.0

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s UTC] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    "glamour", "illusory adornment", "treatise",
}

_PROFESSION_TOOL_KEYWORDS = {
    "knife", "needle", "hammer", "chisel", "awl", "apron", "goggles", "wrench",
    "focuser", "backpack", "chapeau", "bifocals", "rolling pin", "satchel",
    "cover", "visor", "multitool", "snippers", "clampers", "cutters", "toolset",
    "screwdriver", "tongs", "cap", "loupes", "quill", "rod", "shears", "pick",
    "pickaxe", "sickle",
}


def _is_profession_item(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in _PROFESSION_TOOL_KEYWORDS)


def is_midnight_reagent(name: str, item_id: int) -> bool:
    if item_id < MIDNIGHT_MIN_ID:
        return False
    lower = name.lower()
    if _is_profession_item(name):
        return False
    return not any(kw in lower for kw in _GEAR_CONSUMABLE_EXCL)


def fmt_g(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) < 10:
        return f"{v:.2f}g"
    return f"{round(v):,}g"


def load_names() -> dict[str, str]:
    if NAMES_FILE.exists():
        try:
            return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_mv_status(has_mv_data: bool) -> str:
    if not has_mv_data:
        lua_path = os.getenv("TSM_LUA_PATH", "").strip()
        if not lua_path or not Path(lua_path).exists():
            return "unmounted"
        return "no_sync"
    return "ok"


# ---------------------------------------------------------------------------
# Build buy/sell signals — layered pricing reference
# ---------------------------------------------------------------------------

def _build_bankarang_prices(records: list[dict]) -> dict[tuple, dict]:
    """
    Recency-weighted Bankarang buy/sell prices keyed by (item_id, quality_tier).
    See bankarang_pricing.bankarang_prices_weighted for the weighting scheme.
    """
    return bankarang_pricing.bankarang_prices_weighted(
        records, flipper=FLIPPER, realm=PRIMARY_REALM
    )


def _get_live_ah_price(item_id: int, realm: str = PRIMARY_REALM,
                       qt: str = "") -> dict | None:
    """
    Return the most recent live AH snapshot for (item_id, quality_tier, realm).
    Queries live_ah.db (populated by refresh_live_ah.sh every 5 minutes).
    """
    try:
        live_ah_db.init_db()
        return live_ah_db.get_latest_snapshot(realm, item_id, qt)
    except Exception:
        return None


def build_reagent_signals(records: list[dict], names: dict,
                          market_values: dict[str, float]) -> tuple[list[dict], list[dict]]:
    """
    Returns (top_buys, top_sells) — each a list of reagent signal dicts.

    Flipping analysis: Bankarang on Malfurion only.

    Pricing hierarchy:
      1. Live AH minimum price (live_ah.db) — PRIMARY
      2. TSM market value — SECONDARY (if populated)
      3. Bankarang's personal avg sell/buy — TERTIARY (current fallback)

    TOP BUYS:  live AH min ≥20% below Bankarang's recent (last-28d, weighted) avg sell
    TOP SELLS: live AH min ≥20% above Bankarang's recent (last-28d, weighted) avg buy

    Items with no Bankarang sales in the last 28 days are excluded from SELL
    opportunities — without recent activity there's no proof the price holds.
    """
    ban_prices = _build_bankarang_prices(records)

    buy_signals:  list[dict] = []
    sell_signals: list[dict] = []

    live_ah_checked = 0
    live_ah_found   = 0

    for key in ban_prices:
        iid, qt = key
        name = names.get(str(iid), f"Unknown Item ({iid})")
        if not is_midnight_reagent(name, iid):
            continue
        if blizzard_api.is_excluded_item(iid):
            continue

        bp = ban_prices[key]
        # Use recency-weighted averages as the primary signal anchor.
        avg_buy  = bp["buy_recent_avg"]
        avg_sell = bp["sell_recent_avg"]
        tsm_mv   = market_values.get(str(iid)) or None
        buy_txns  = bp["buy_recent_txns"]
        sell_txns = bp["sell_recent_txns"]
        total_txns = buy_txns + sell_txns

        # --- Primary: live AH ---
        live_ah_checked += 1
        snap = _get_live_ah_price(iid, PRIMARY_REALM, qt)
        live_min = snap["min_price"] if snap else None
        listing_count = snap["listing_count"] if snap else None
        if live_min is not None:
            live_ah_found += 1

        # --- Build signals ---
        ref_price    = live_min or tsm_mv  # PRIMARY or SECONDARY
        price_source = "live_ah" if live_min is not None else ("tsm_mv" if tsm_mv else "personal")
        display_name = f"{name} {quality_tiers.fmt_quality(qt)}" if qt else name

        alltime_sell = bp["sell_alltime_avg"]
        alltime_buy  = bp["buy_alltime_avg"]
        sell_days_since = bp["sell_days_since"]

        if ref_price is not None:
            # BUY signal: live AH min is well below recent avg sell → buy to resell.
            # If avg_sell is None, Bankarang has no recent (≤28d) sales — skip.
            if avg_sell is not None:
                profit = avg_sell - ref_price
                spread_pct = (profit / avg_sell * 100) if avg_sell else 0
                if profit >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    buy_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": ref_price,
                        "avg_sell": avg_sell,
                        "alltime_sell": alltime_sell,
                        "profit": profit,
                        "spread_pct": spread_pct,
                        "txns": sell_txns,
                        "listing_count": listing_count,
                        "price_source": price_source,
                        "fallback": price_source == "personal",
                        "sell_days_since": sell_days_since,
                    })

            # SELL signal: live AH min is well above recent avg buy → list now.
            # If avg_buy is None, no recent buy data — skip.
            if avg_buy is not None:
                profit = ref_price - avg_buy
                spread_pct = (profit / avg_buy * 100) if avg_buy else 0
                if profit >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    sell_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": ref_price,
                        "avg_buy": avg_buy,
                        "alltime_buy": alltime_buy,
                        "profit": profit,
                        "spread_pct": spread_pct,
                        "txns": buy_txns,
                        "listing_count": listing_count,
                        "price_source": price_source,
                        "fallback": price_source == "personal",
                    })

        else:
            # TERTIARY: no external reference — use personal recent buy vs sell spread
            if avg_buy is not None and avg_sell is not None:
                spread = avg_sell - avg_buy
                spread_pct = (spread / avg_buy * 100) if avg_buy else 0
                if spread >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    buy_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": avg_sell,
                        "avg_sell": avg_sell,
                        "alltime_sell": alltime_sell,
                        "profit": spread,
                        "spread_pct": spread_pct,
                        "txns": sell_txns,
                        "listing_count": None,
                        "price_source": "personal",
                        "fallback": True,
                        "sell_days_since": sell_days_since,
                    })
                    sell_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": avg_buy,
                        "avg_buy": avg_buy,
                        "alltime_buy": alltime_buy,
                        "profit": spread,
                        "spread_pct": spread_pct,
                        "txns": buy_txns,
                        "listing_count": None,
                        "price_source": "personal",
                        "fallback": True,
                    })

    logger.info(
        f"Live AH checked: {live_ah_checked} items, found prices: {live_ah_found} "
        f"| Buy signals: {len(buy_signals)}  Sell signals: {len(sell_signals)}"
    )

    buy_signals.sort(key=lambda x: x["profit"], reverse=True)
    sell_signals.sort(key=lambda x: x["profit"], reverse=True)
    return buy_signals[:5], sell_signals[:5]


# ---------------------------------------------------------------------------
# Format Discord message
# ---------------------------------------------------------------------------

_SOURCE_LABEL = {
    "live_ah":  "Live AH",
    "tsm_mv":   "TSM MV",
    "personal": "Est.",
}


def format_message(top_buys: list[dict], top_sells: list[dict],
                   mv_status: str) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"⚔️ **TSM Reagent Report — {PRIMARY_REALM}**",
        f"🕐 Updated: {now}",
        f"📊 *Flipping analysis: Bankarang only*",
        "",
    ]

    if mv_status == "unmounted":
        lines += [
            "⚠️ *Games drive unmounted — falling back to personal price history.*",
            "",
        ]
    # No message for no_sync: live AH is the intended primary source now

    if top_buys:
        lines.append("📈 **TOP 5 BUYS** *(buy now — AH price below recent sell avg, last 28d weighted)*")
        for i, item in enumerate(top_buys, 1):
            listings_str = f"{item['listing_count']} listings up" if item.get("listing_count") else "listings unknown"
            lines.append(f"{i}. **{item['name']}**")
            lines.append(f"   Buy at: {fmt_g(item['ref_price'])}  →  Recent sold avg: {fmt_g(item.get('avg_sell'))} ({item['txns']} txns ≤28d)")
            if item.get("alltime_sell") is not None:
                lines.append(f"   All-time sold avg: {fmt_g(item['alltime_sell'])}")
            lines.append(f"   Potential profit: **+{fmt_g(item['profit'])} per unit**")
            lines.append(f"   {listings_str}")
    else:
        lines.append("📈 **TOP 5 BUYS** — *No strong buy signals right now*")

    lines.append("")

    if top_sells:
        lines.append("📉 **TOP 5 SELLS** *(list now — AH price above recent buy avg, last 28d weighted)*")
        for i, item in enumerate(top_sells, 1):
            listings_str = f"{item['listing_count']} listings up" if item.get("listing_count") else "listings unknown"
            lines.append(f"{i}. **{item['name']}**")
            lines.append(f"   Recent buy avg: {fmt_g(item.get('avg_buy'))} ({item['txns']} txns ≤28d)  →  Live AH min: {fmt_g(item['ref_price'])}")
            if item.get("alltime_buy") is not None:
                lines.append(f"   All-time buy avg: {fmt_g(item['alltime_buy'])}")
            lines.append(f"   Potential premium: **+{fmt_g(item['profit'])} per unit**")
            lines.append(f"   {listings_str}")
    else:
        lines.append("📉 **TOP 5 SELLS** — *No items currently above market*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Send alert
# ---------------------------------------------------------------------------

def send_reagent_alert(webhook_url: str) -> bool:
    """Post reagent alert to Discord. Returns True on success."""
    logger.info("Loading TSM data...")
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    records       = raw["records"]
    market_values = raw.get("market_values", {})
    names         = load_names()

    has_mv_data = any(v for v in market_values.values()) if market_values else False
    mv_status   = get_mv_status(has_mv_data)
    top_buys, top_sells = build_reagent_signals(records, names, market_values)

    logger.info(f"MV status: {mv_status}  Buy signals: {len(top_buys)}  Sell signals: {len(top_sells)}")

    message = format_message(top_buys, top_sells, mv_status)

    resp = requests.post(
        webhook_url,
        json={"content": message},
        timeout=15,
    )

    if resp.status_code in (200, 204):
        logger.info("Discord alert posted successfully")
        return True
    else:
        logger.error(f"Discord webhook error {resp.status_code}: {resp.text}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(ENV_FILE)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL not set in .env — aborting")
        sys.exit(1)

    success = send_reagent_alert(webhook_url)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
