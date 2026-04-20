#!/usr/bin/env python3
"""
Restock suggestion analysis for Bankarang's flip portfolio.

Identifies items Bankarang has successfully sold multiple times but has not
bought recently — indicating she may be out of stock.  Compares the current
Live AH min to her historical buy average to surface restock opportunities
where the price is still favourable.

Logic:
  1. Count Bankarang's SALES per (item_id, quality_tier) on Malfurion.
  2. Keep items with ≥ MIN_SALE_COUNT sales.
  3. Check whether she has bought the item within the last RECENT_BUY_DAYS days.
  4. For items with no recent buys, compare current live AH min to her avg buy:
       - Flag as "Restock" if live_min < avg_buy × RESTOCK_PRICE_RATIO
       - Skip if live_min ≥ avg_buy × RESTOCK_PRICE_RATIO (not worth restocking at this price)
  5. Rank by estimated_profit = (avg_sell − live_min) × (sell_count / observation_days)
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────
# Configurable constants
# ─────────────────────────────────────────────────────────

MIN_SALE_COUNT        = 3      # minimum sales history to consider
RECENT_BUY_DAYS       = 7      # days lookback for "recent buy" check
RESTOCK_PRICE_RATIO   = 0.90   # flag restock only if live_min < avg_buy × this (when buy history exists)
MIN_SELL_MARGIN       = 0.30   # min net margin vs avg_sell required when no buy history (30% headroom)
PRIMARY_REALM         = "Malfurion"
FLIPPER               = "Bankarang"


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp string to UTC datetime."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────

def build_restock_suggestions(
    records: list[dict],
    prices: dict[tuple[int, str], float],
    names: dict[str, str],
) -> list[dict]:
    """
    Build Bankarang restock suggestions.

    Args:
        records:  TSM transaction records (from tsm_data.json)
        prices:   (item_id, quality_tier) → live AH min on Malfurion
        names:    item_id str → item name

    Returns:
        List of restock suggestion dicts, sorted by estimated_profit desc.
    """
    now = datetime.now(tz=timezone.utc)
    recent_cutoff = now - timedelta(days=RECENT_BUY_DAYS)

    # Filter to Bankarang on Malfurion
    ban_records = [
        r for r in records
        if r.get("player") == FLIPPER and r.get("realm") == PRIMARY_REALM
    ]

    # Accumulate sales and buys per (item_id, quality_tier)
    sales:      dict[tuple, list[dict]] = defaultdict(list)
    buys:       dict[tuple, list[dict]] = defaultdict(list)
    recent_buy: dict[tuple, bool]       = defaultdict(bool)

    for r in ban_records:
        key = (r["item_id"], r.get("quality_tier", ""))
        rtype = r.get("type", "")
        ts = _parse_ts(r.get("timestamp_utc", ""))

        if rtype == "Sales" and r.get("source") == "Auction":
            sales[key].append(r)
        elif rtype == "Buys" and r.get("source") == "Auction":
            buys[key].append(r)
            if ts >= recent_cutoff:
                recent_buy[key] = True

    # Observation window: oldest to newest timestamp in records
    all_ts = [_parse_ts(r.get("timestamp_utc", "")) for r in ban_records if r.get("timestamp_utc")]
    if all_ts:
        obs_days = max(1.0, (max(all_ts) - min(all_ts)).total_seconds() / 86400)
    else:
        obs_days = 30.0  # fallback

    results = []

    for key, sale_list in sales.items():
        iid, qt = key
        if len(sale_list) < MIN_SALE_COUNT:
            continue

        # Skip if Bankarang has bought this recently (probably already restocked)
        if recent_buy.get(key):
            continue

        # Skip if no live AH price available
        live_min = prices.get(key)
        if live_min is None:
            continue

        # Compute historical sell average
        total_sell_gold = sum(r["price_gold"] for r in sale_list)
        total_sell_qty  = sum(r["quantity"]    for r in sale_list)
        avg_sell = total_sell_gold / total_sell_qty if total_sell_qty else 0

        if avg_sell <= 0:
            continue

        # Compute historical buy average if available
        buy_list = buys.get(key, [])
        if buy_list:
            total_buy_gold = sum(r["price_gold"] for r in buy_list)
            total_buy_qty  = sum(r["quantity"]    for r in buy_list)
            avg_buy = total_buy_gold / total_buy_qty if total_buy_qty else None
        else:
            avg_buy = None

        # Price check: if buy history exists, use it as the price anchor.
        # Otherwise fall back to requiring live_min to be well below avg_sell
        # (MIN_SELL_MARGIN headroom after 5% AH cut to ensure a profitable flip).
        if avg_buy is not None:
            if avg_buy <= 0 or live_min >= avg_buy * RESTOCK_PRICE_RATIO:
                continue
        else:
            # No buy history: use sell price as anchor — need at least MIN_SELL_MARGIN profit
            net_if_bought_now = avg_sell * (1.0 - 0.05) - live_min
            if net_if_bought_now / avg_sell < MIN_SELL_MARGIN:
                continue

        sell_count     = len(sale_list)
        sell_frequency = sell_count / obs_days  # sales per day

        # Estimated profit: per-unit margin × daily sell rate
        net_margin          = avg_sell * (1.0 - 0.05) - live_min  # 5% cut on sell side
        estimated_profit    = net_margin * sell_frequency

        name = names.get(str(iid), f"Item {iid}")

        # Priority label
        ref_price = avg_buy if avg_buy is not None else live_min
        if net_margin > ref_price * 0.5:
            priority = "High"
        elif net_margin > 0:
            priority = "Medium"
        else:
            priority = "Low"

        results.append({
            "item_id":           iid,
            "quality_tier":      qt,
            "item_name":         name,
            "live_ah_min":       round(live_min, 4),
            "avg_buy":           round(avg_buy, 4) if avg_buy is not None else None,
            "avg_sell":          round(avg_sell, 4),
            "sell_count":        sell_count,
            "sell_frequency":    round(sell_frequency, 3),
            "net_margin":        round(net_margin, 4),
            "estimated_profit":  round(estimated_profit, 4),
            # % above/below avg buy (or avg sell if no buy history)
            "price_vs_avg":      round((live_min / (avg_buy or avg_sell) - 1) * 100, 1),
            "priority":          priority,
        })

    results.sort(key=lambda r: r["estimated_profit"], reverse=True)
    return results
