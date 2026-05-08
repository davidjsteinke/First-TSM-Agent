#!/usr/bin/env python3
"""
Bankarang transaction pricing with recency weighting.

Old transactions don't reflect current market conditions. A 6,449g historical
avg-sell from sales 60+ days ago is misleading when the current AH is 1,800g.
This module weights every transaction by age:

    Last 7 days:   weight = 1.0
    8–14 days:     weight = 0.5
    15–28 days:    weight = 0.25
    > 28 days:     weight = 0.0  (excluded entirely)

A transaction with no recent activity (no sales in 28 days) is treated as
"no longer a proven price" — callers should skip it as a sell opportunity.
"""

from collections import defaultdict
from datetime import datetime, timezone


# Recency tiers — (max_age_days, weight). Order matters (must be ascending).
RECENCY_TIERS: tuple[tuple[int, float], ...] = (
    (7,  1.00),
    (14, 0.50),
    (28, 0.25),
)
EXCLUSION_DAYS = 28  # transactions older than this are dropped entirely


def _parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _weight_for(age_days: float) -> float:
    """Return the recency weight for a transaction `age_days` old."""
    for max_age, w in RECENCY_TIERS:
        if age_days <= max_age:
            return w
    return 0.0


def weighted_avg(records: list[dict], now: datetime | None = None) -> dict:
    """
    Compute both recency-weighted and all-time averages from a list of TSM
    transaction records (each must have price_gold, quantity, timestamp_utc).

    Returns:
        {
            "recent_avg":   weighted gold-per-unit (last 28 days, weighted)
                            or None if no recent records
            "recent_txns":  count of records contributing weight > 0
            "alltime_avg":  unweighted gold-per-unit across all records
                            or None if no records
            "alltime_txns": total record count
            "last_txn_ts":  ISO-8601 timestamp of newest record (or None)
            "days_since":   days since most recent transaction (None if empty)
        }
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    weighted_gold  = 0.0
    weighted_qty   = 0.0
    recent_txns    = 0

    alltime_gold = 0.0
    alltime_qty  = 0
    alltime_txns = 0
    newest_ts: datetime | None = None

    for r in records:
        qty   = r.get("quantity") or 0
        price = r.get("price_gold")
        ts    = _parse_ts(r.get("timestamp_utc", ""))
        if qty <= 0 or price is None:
            continue

        alltime_gold += price
        alltime_qty  += qty
        alltime_txns += 1
        if ts and (newest_ts is None or ts > newest_ts):
            newest_ts = ts

        if ts is None:
            continue
        age_days = (now - ts).total_seconds() / 86_400
        if age_days < 0:
            age_days = 0
        if age_days > EXCLUSION_DAYS:
            continue

        w = _weight_for(age_days)
        if w <= 0:
            continue
        weighted_gold += price * w
        weighted_qty  += qty   * w
        recent_txns   += 1

    recent_avg  = (weighted_gold / weighted_qty) if weighted_qty else None
    alltime_avg = (alltime_gold  / alltime_qty)  if alltime_qty  else None
    days_since  = (now - newest_ts).total_seconds() / 86_400 if newest_ts else None

    return {
        "recent_avg":   recent_avg,
        "recent_txns":  recent_txns,
        "alltime_avg":  alltime_avg,
        "alltime_txns": alltime_txns,
        "last_txn_ts":  newest_ts.isoformat() if newest_ts else None,
        "days_since":   round(days_since, 2) if days_since is not None else None,
    }


def bankarang_prices_weighted(
    records: list[dict],
    flipper: str,
    realm: str,
    now: datetime | None = None,
) -> dict[tuple, dict]:
    """
    Compute per-(item_id, quality_tier) Bankarang buy/sell prices with both
    recency-weighted and all-time averages.

    Returns {(item_id, qt): {
        "buy_recent_avg", "buy_recent_txns", "buy_alltime_avg", "buy_alltime_txns",
        "buy_days_since",
        "sell_recent_avg", "sell_recent_txns", "sell_alltime_avg", "sell_alltime_txns",
        "sell_days_since",
    }}
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    buys_by_key:  dict[tuple, list[dict]] = defaultdict(list)
    sales_by_key: dict[tuple, list[dict]] = defaultdict(list)

    for r in records:
        if r.get("realm")  != realm:    continue
        if r.get("source") != "Auction": continue
        if r.get("player") != flipper:   continue
        key = (r["item_id"], r.get("quality_tier", ""))
        if r.get("type") == "Buys":
            buys_by_key[key].append(r)
        elif r.get("type") == "Sales":
            sales_by_key[key].append(r)

    out: dict[tuple, dict] = {}
    for key in set(buys_by_key) | set(sales_by_key):
        b = weighted_avg(buys_by_key.get(key,  []), now=now)
        s = weighted_avg(sales_by_key.get(key, []), now=now)
        out[key] = {
            "buy_recent_avg":    b["recent_avg"],
            "buy_recent_txns":   b["recent_txns"],
            "buy_alltime_avg":   b["alltime_avg"],
            "buy_alltime_txns":  b["alltime_txns"],
            "buy_days_since":    b["days_since"],
            "sell_recent_avg":   s["recent_avg"],
            "sell_recent_txns":  s["recent_txns"],
            "sell_alltime_avg":  s["alltime_avg"],
            "sell_alltime_txns": s["alltime_txns"],
            "sell_days_since":   s["days_since"],
        }
    return out


def has_recent_sales(price_entry: dict | None) -> bool:
    """Returns True if Bankarang has at least one weighted sale (≤28d)."""
    if not price_entry:
        return False
    return (price_entry.get("sell_recent_txns") or 0) > 0
