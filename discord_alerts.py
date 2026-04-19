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

import blizzard_api
import live_ah_db
import quality_tiers

SCRIPT_DIR  = Path(__file__).parent
DATA_FILE   = Path.home() / "tsm_data.json"
NAMES_FILE  = Path(__file__).parent / "item_names.json"
LOG_FILE    = SCRIPT_DIR / "logs" / "agent.log"
ENV_FILE    = SCRIPT_DIR / ".env"

PRIMARY_REALM   = "Malfurion"
MIDNIGHT_MIN_ID = 236000
MIN_PROFIT_G    = 2.0   # minimum gold profit to appear in lists
GAMES_MOUNT     = "/mnt/Games"

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
        if not os.path.ismount(GAMES_MOUNT):
            return "unmounted"
        return "no_sync"
    return "ok"


# ---------------------------------------------------------------------------
# Build buy/sell signals — layered pricing reference
# ---------------------------------------------------------------------------

def _build_bankarang_prices(records: list[dict]) -> tuple[dict[int, dict], dict[int, dict]]:
    """
    Compute Bankarang's weighted average buy and sell prices per item_id.
    Returns (buy_acc, sell_acc) where each is {item_id: {gold, qty, txns}}.
    Only considers Bankarang on Malfurion, Auction source — the designated flipper.
    """
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
        buy_acc[key]["gold"] += r["price_gold"]
        buy_acc[key]["qty"]  += r["quantity"]
        buy_acc[key]["txns"] += 1

    for r in sales:
        key = (r["item_id"], r.get("quality_tier", ""))
        sell_acc[key]["gold"] += r["price_gold"]
        sell_acc[key]["qty"]  += r["quantity"]
        sell_acc[key]["txns"] += 1

    return dict(buy_acc), dict(sell_acc)


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

    TOP BUYS:  live AH min ≥20% below Bankarang's historical avg sell price
    TOP SELLS: live AH min ≥20% above Bankarang's historical avg buy price
    """
    buy_acc, sell_acc = _build_bankarang_prices(records)
    all_keys: set[tuple] = set(buy_acc) | set(sell_acc)

    buy_signals:  list[dict] = []
    sell_signals: list[dict] = []

    live_ah_checked = 0
    live_ah_found   = 0

    for key in all_keys:
        iid, qt = key
        name = names.get(str(iid), f"Unknown Item ({iid})")
        if not is_midnight_reagent(name, iid):
            continue
        if blizzard_api.is_excluded_item(iid):
            continue

        b = buy_acc.get(key)
        s = sell_acc.get(key)
        avg_buy  = (b["gold"] / b["qty"]) if b and b["qty"] else None
        avg_sell = (s["gold"] / s["qty"]) if s and s["qty"] else None
        tsm_mv   = market_values.get(str(iid)) or None
        buy_txns  = b["txns"] if b else 0
        sell_txns = s["txns"] if s else 0
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

        if ref_price is not None:
            # BUY signal: live AH min is well below avg sell → buy to resell
            if avg_sell is not None:
                profit = avg_sell - ref_price
                spread_pct = (profit / avg_sell * 100) if avg_sell else 0
                if profit >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    buy_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": ref_price,
                        "avg_sell": avg_sell,
                        "profit": profit,
                        "spread_pct": spread_pct,
                        "txns": total_txns,
                        "listing_count": listing_count,
                        "price_source": price_source,
                        "fallback": price_source == "personal",
                    })

            # SELL signal: live AH min is well above avg buy → list now
            if avg_buy is not None:
                profit = ref_price - avg_buy
                spread_pct = (profit / avg_buy * 100) if avg_buy else 0
                if profit >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    sell_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": ref_price,
                        "avg_buy": avg_buy,
                        "profit": profit,
                        "spread_pct": spread_pct,
                        "txns": total_txns,
                        "listing_count": listing_count,
                        "price_source": price_source,
                        "fallback": price_source == "personal",
                    })

        else:
            # TERTIARY: no external reference — use personal buy vs sell spread
            if avg_buy is not None and avg_sell is not None:
                spread = avg_sell - avg_buy
                spread_pct = (spread / avg_buy * 100) if avg_buy else 0
                if spread >= MIN_PROFIT_G and spread_pct >= SIGNAL_THRESHOLD_PCT:
                    buy_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": avg_sell,
                        "avg_sell": avg_sell,
                        "profit": spread,
                        "spread_pct": spread_pct,
                        "txns": total_txns,
                        "listing_count": None,
                        "price_source": "personal",
                        "fallback": True,
                    })
                    sell_signals.append({
                        "name": display_name,
                        "quality_tier": qt,
                        "ref_price": avg_buy,
                        "avg_buy": avg_buy,
                        "profit": spread,
                        "spread_pct": spread_pct,
                        "txns": total_txns,
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
        lines.append("📈 **TOP 5 BUYS** *(buy now — AH price below your avg sell)*")
        for i, item in enumerate(top_buys, 1):
            listings_str = f"{item['listing_count']} listings up" if item.get("listing_count") else "listings unknown"
            lines.append(f"{i}. **{item['name']}**")
            lines.append(f"   Buy at: {fmt_g(item['ref_price'])}  →  Bankarang sold avg: {fmt_g(item.get('avg_sell'))}")
            lines.append(f"   Potential profit: **+{fmt_g(item['profit'])} per unit**")
            lines.append(f"   {listings_str} | {item['txns']} txn history")
    else:
        lines.append("📈 **TOP 5 BUYS** — *No strong buy signals right now*")

    lines.append("")

    if top_sells:
        lines.append("📉 **TOP 5 SELLS** *(list these now — AH price above your avg buy)*")
        for i, item in enumerate(top_sells, 1):
            listings_str = f"{item['listing_count']} listings up" if item.get("listing_count") else "listings unknown"
            lines.append(f"{i}. **{item['name']}**")
            lines.append(f"   Bankarang bought avg: {fmt_g(item.get('avg_buy'))}  →  Live AH min: {fmt_g(item['ref_price'])}")
            lines.append(f"   Potential premium: **+{fmt_g(item['profit'])} per unit**")
            lines.append(f"   {listings_str} | {item['txns']} txn history")
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
