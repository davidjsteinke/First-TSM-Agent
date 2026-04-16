#!/usr/bin/env python3
"""
TSM Discord Reagent Alerts

Posts a TOP 5 BUYS / TOP 5 SELLS reagent summary to a Discord channel
every 15 minutes via webhook.

Signal logic uses TSM market value (14-day weighted average) as reference:
  BUYS:  TSM MV significantly > recent buy price  → worth buying now
  SELLS: Recent sell price significantly > TSM MV → list now while high

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

SCRIPT_DIR  = Path(__file__).parent
DATA_FILE   = Path.home() / "tsm_data.json"
NAMES_FILE  = Path.home() / "item_names.json"
LOG_FILE    = SCRIPT_DIR / "logs" / "agent.log"
ENV_FILE    = SCRIPT_DIR / ".env"

PRIMARY_REALM   = "Malfurion"
MIDNIGHT_MIN_ID = 236000
MIN_PROFIT_G    = 2.0   # minimum gold profit to appear in lists

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


# ---------------------------------------------------------------------------
# Build buy/sell lists
# ---------------------------------------------------------------------------

def build_reagent_signals(records: list[dict], names: dict,
                          market_values: dict[str, float]) -> tuple[list[dict], list[dict]]:
    """
    Returns (top_buys, top_sells) — each a list of reagent dicts.

    BUYS:  TSM MV > avg recent buy price  → ranked by (TSM MV - avg buy)
    SELLS: avg recent sell price > TSM MV → ranked by (avg sell - TSM MV)
    """
    buys  = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Buys"  and r.get("source") == "Auction"]
    sales = [r for r in records if r.get("realm") == PRIMARY_REALM
             and r.get("type") == "Sales" and r.get("source") == "Auction"]

    buy_acc  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_acc = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    all_ids: set[int] = set()
    for r in buys:
        iid = r["item_id"]
        buy_acc[iid]["gold"] += r["price_gold"]
        buy_acc[iid]["qty"]  += r["quantity"]
        buy_acc[iid]["txns"] += 1
        all_ids.add(iid)
    for r in sales:
        iid = r["item_id"]
        sell_acc[iid]["gold"] += r["price_gold"]
        sell_acc[iid]["qty"]  += r["quantity"]
        sell_acc[iid]["txns"] += 1
        all_ids.add(iid)

    buy_signals  = []
    sell_signals = []

    for iid in all_ids:
        name   = names.get(str(iid), f"Item {iid}")
        if not is_midnight_reagent(name, iid):
            continue

        b = buy_acc.get(iid)
        s = sell_acc.get(iid)
        avg_buy  = (b["gold"] / b["qty"]) if b and b["qty"] else None
        avg_sell = (s["gold"] / s["qty"]) if s and s["qty"] else None
        tsm_mv   = market_values.get(str(iid))
        txns     = (b["txns"] if b else 0) + (s["txns"] if s else 0)

        if tsm_mv is not None and avg_buy is not None:
            profit = tsm_mv - avg_buy
            if profit >= MIN_PROFIT_G:
                buy_signals.append({
                    "name": name, "tsm_mv": tsm_mv,
                    "recent_price": avg_buy, "profit": profit, "txns": txns,
                })

        if tsm_mv is not None and avg_sell is not None:
            profit = avg_sell - tsm_mv
            if profit >= MIN_PROFIT_G:
                sell_signals.append({
                    "name": name, "tsm_mv": tsm_mv,
                    "recent_price": avg_sell, "profit": profit, "txns": txns,
                })

    buy_signals.sort(key=lambda x: x["profit"], reverse=True)
    sell_signals.sort(key=lambda x: x["profit"], reverse=True)
    return buy_signals[:5], sell_signals[:5]


# ---------------------------------------------------------------------------
# Format Discord message
# ---------------------------------------------------------------------------

def format_message(top_buys: list[dict], top_sells: list[dict],
                   has_mv_data: bool) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"⚔️ **TSM Reagent Report — {PRIMARY_REALM}**",
        f"🕐 Updated: {now}",
        "",
    ]

    if not has_mv_data:
        lines += [
            "⚠️ *TSM market values not available — drive may be unmounted.*",
            "*Showing personal transaction history only.*",
            "",
        ]

    if top_buys:
        lines.append("📈 **TOP 5 BUYS** *(buy now, sell at market value)*")
        for i, item in enumerate(top_buys, 1):
            lines.append(
                f"{i}. **{item['name']}** — "
                f"Buy @ {fmt_g(item['recent_price'])} | "
                f"Market: {fmt_g(item['tsm_mv'])} | "
                f"**+{fmt_g(item['profit'])} profit** "
                f"({item['txns']} txns)"
            )
    else:
        lines.append("📈 **TOP 5 BUYS** — *No strong buy signals right now*")

    lines.append("")

    if top_sells:
        lines.append("📉 **TOP 5 SELLS** *(list these now — above market)*")
        for i, item in enumerate(top_sells, 1):
            lines.append(
                f"{i}. **{item['name']}** — "
                f"Sell @ {fmt_g(item['recent_price'])} | "
                f"Market: {fmt_g(item['tsm_mv'])} | "
                f"**+{fmt_g(item['profit'])} premium** "
                f"({item['txns']} txns)"
            )
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

    has_mv_data = len(market_values) > 0
    top_buys, top_sells = build_reagent_signals(records, names, market_values)

    logger.info(f"Buy signals: {len(top_buys)}  Sell signals: {len(top_sells)}")

    message = format_message(top_buys, top_sells, has_mv_data)

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
