#!/usr/bin/env python3
"""
TSM Auction Profit Analyst
Loads tsm_data.json and produces a ranked profit-opportunity report.
"""

import json
from collections import defaultdict
from pathlib import Path

import price_history
import blizzard_api

DATA_FILE = Path("/home/davidjsteinke/tsm_data.json")
REPORT_FILE = Path("/home/davidjsteinke/report.txt")

HIGH_MARGIN_THRESHOLD = 20.0  # percent
PRIMARY_REALM = "Malfurion"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def unit_price(rec: dict) -> float:
    """Price per individual item in gold."""
    qty = rec["quantity"]
    return rec["price_gold"] / qty if qty else rec["price_gold"]


def item_label(item_id: int, width: int = 36) -> str:
    """Return 'Item Name (123456)' truncated/padded to width."""
    name = blizzard_api.get_item_name(item_id)
    full = f"{name} ({item_id})"
    return full[:width].ljust(width) if len(full) > width else full.ljust(width)


def fmt_gold(g: float) -> str:
    return f"{g:>10.2f}g"


def fmt_pct(p: float) -> str:
    return f"{p:>+8.1f}%"


def divider(char: str = "─", width: int = 100) -> str:
    return char * width


def header(title: str, width: int = 100) -> str:
    pad = (width - len(title) - 2) // 2
    return f"{'═' * pad} {title} {'═' * (width - pad - len(title) - 2)}"


# ---------------------------------------------------------------------------
# Load & partition
# ---------------------------------------------------------------------------

def load_data(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["records"]


def partition(records: list[dict]) -> dict:
    """Split records into logical buckets keyed by (realm, type)."""
    buckets: dict[tuple, list] = defaultdict(list)
    for r in records:
        buckets[(r["realm"], r["type"])].append(r)
    return buckets


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def build_item_stats(buys: list[dict], sales: list[dict]) -> dict:
    """
    For each item_id appearing in BOTH buys and sales (Auction source only),
    compute aggregate pricing and profit metrics.

    price_gold in the data is the TOTAL for the transaction.
    Unit price = price_gold / quantity.
    We weight averages by quantity so bulk transactions count fairly.
    """
    # Filter to Auction only (exclude Vendor, etc.)
    auction_buys  = [r for r in buys  if r.get("source") == "Auction"]
    auction_sales = [r for r in sales if r.get("source") == "Auction"]

    # Accumulate weighted sums per item_id
    buy_data: dict[int, dict]  = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})
    sell_data: dict[int, dict] = defaultdict(lambda: {"gold": 0.0, "qty": 0, "txns": 0})

    for r in auction_buys:
        iid = r["item_id"]
        buy_data[iid]["gold"] += r["price_gold"]
        buy_data[iid]["qty"]  += r["quantity"]
        buy_data[iid]["txns"] += 1

    for r in auction_sales:
        iid = r["item_id"]
        sell_data[iid]["gold"] += r["price_gold"]
        sell_data[iid]["qty"]  += r["quantity"]
        sell_data[iid]["txns"] += 1

    # Only items present in BOTH
    common_ids = set(buy_data) & set(sell_data)

    stats = []
    for iid in common_ids:
        b = buy_data[iid]
        s = sell_data[iid]

        avg_buy  = b["gold"] / b["qty"]   # weighted avg per-item buy price
        avg_sell = s["gold"] / s["qty"]   # weighted avg per-item sell price

        profit_per_item = avg_sell - avg_buy
        margin_pct = (profit_per_item / avg_buy * 100) if avg_buy else 0.0
        total_volume = b["txns"] + s["txns"]

        stats.append({
            "item_id":        iid,
            "avg_buy":        avg_buy,
            "avg_sell":       avg_sell,
            "profit_per_item": profit_per_item,
            "margin_pct":     margin_pct,
            "buy_txns":       b["txns"],
            "sell_txns":      s["txns"],
            "buy_qty":        b["qty"],
            "sell_qty":       s["qty"],
            "total_volume":   total_volume,
        })

    # Rank by absolute gold profit per item (descending)
    stats.sort(key=lambda x: x["profit_per_item"], reverse=True)
    return stats


def build_cancel_expired_stats(cancelled: list[dict], expired: list[dict],
                                sales: list[dict], buys: list[dict]) -> list[dict]:
    """
    For items that were cancelled or expired, summarise listing activity
    and flag repricing concerns.
    """
    # Count cancels + expirations per item
    ce_data: dict[int, dict] = defaultdict(lambda: {
        "cancels": 0, "expirations": 0, "cancel_qty": 0, "expired_qty": 0
    })
    for r in cancelled:
        iid = r["item_id"]
        ce_data[iid]["cancels"]    += 1
        ce_data[iid]["cancel_qty"] += r["quantity"]
    for r in expired:
        iid = r["item_id"]
        ce_data[iid]["expirations"]  += 1
        ce_data[iid]["expired_qty"]  += r["quantity"]

    # Enrich with sale/buy context
    sell_txns: dict[int, int] = defaultdict(int)
    buy_prices: dict[int, list] = defaultdict(list)
    for r in sales:
        if r.get("source") == "Auction":
            sell_txns[r["item_id"]] += 1
    for r in buys:
        if r.get("source") == "Auction":
            buy_prices[r["item_id"]].append(unit_price(r))

    result = []
    for iid, d in ce_data.items():
        failures = d["cancels"] + d["expirations"]
        successes = sell_txns.get(iid, 0)
        total_listings = failures + successes
        failure_rate = (failures / total_listings * 100) if total_listings else 100.0
        avg_buy_price = (sum(buy_prices[iid]) / len(buy_prices[iid])) if buy_prices.get(iid) else None

        result.append({
            "item_id":       iid,
            "cancels":       d["cancels"],
            "expirations":   d["expirations"],
            "failed_qty":    d["cancel_qty"] + d["expired_qty"],
            "sell_successes": successes,
            "total_listings": total_listings,
            "failure_rate":  failure_rate,
            "avg_buy_price": avg_buy_price,
        })

    # Sort by number of failures desc, then failure_rate desc
    result.sort(key=lambda x: (x["cancels"] + x["expirations"], x["failure_rate"]), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

TREND_ICON = {"RISING": "↑", "FALLING": "↓", "STABLE": "→", "NEW": "✦"}


def render_profit_table(
    stats: list[dict],
    realm_label: str,
    trends: dict | None = None,
    show_limit: int = 40,
) -> list[str]:
    has_trends = bool(trends)
    lines = []
    lines.append(header(f"PROFIT OPPORTUNITIES — {realm_label}"))
    lines.append(f"  Auction-only | items appearing in BOTH buys & sales | ranked by gold profit/item")
    hint = f"  ★ = margin > {HIGH_MARGIN_THRESHOLD:.0f}%  (strong opportunity)"
    if has_trends:
        hint += "  |  ↑ RISING  ↓ FALLING  → STABLE  ✦ NEW  |  ⚠ = margin dropped >10pp"
    lines.append(hint)
    lines.append(divider())
    col = (
        f"  {'Item':<36}  {'Avg Buy':>10}  {'Avg Sell':>10}  "
        f"{'Profit/Item':>12}  {'Margin':>9}  {'Buy Txns':>8}  {'Sell Txns':>9}  {'Flag':<16}"
    )
    if has_trends:
        col += f"  {'Trend':<8}  ΔSell       ΔMargin"
    lines.append(col)
    lines.append(divider("─"))

    shown = 0
    margin_warnings = []

    for s in stats:
        if shown >= show_limit:
            break
        flag = "★  HIGH MARGIN" if s["margin_pct"] >= HIGH_MARGIN_THRESHOLD else ""
        line = (
            f"  {item_label(s['item_id'])}  "
            f"{fmt_gold(s['avg_buy'])}  "
            f"{fmt_gold(s['avg_sell'])}  "
            f"{fmt_gold(s['profit_per_item'])}  "
            f"{fmt_pct(s['margin_pct'])}  "
            f"{s['buy_txns']:>8}  "
            f"{s['sell_txns']:>9}  "
            f"{flag:<16}"
        )
        if has_trends:
            t = (trends or {}).get(s["item_id"])
            if t:
                icon  = TREND_ICON.get(t["trend"], "?")
                label = f"{icon} {t['trend']:<7}"
                d_sell   = f"{t['sell_delta']:>+8.2f}g"
                d_margin = f"{t['margin_delta']:>+7.1f}pp"
                warn = "  ⚠ MARGIN DROP" if t["margin_warning"] else ""
                line += f"  {label}  {d_sell}  {d_margin}{warn}"
                if t["margin_warning"]:
                    margin_warnings.append(s)
            else:
                line += f"  {'—':<8}  {'—':>9}  {'—':>8}"
        lines.append(line)
        shown += 1

    if not stats:
        lines.append("  (no items found in both buys and sales for this realm set)")

    # Summary counts
    high_margin = [s for s in stats if s["margin_pct"] >= HIGH_MARGIN_THRESHOLD]
    profitable   = [s for s in stats if s["profit_per_item"] > 0]
    lines.append(divider("─"))
    lines.append(
        f"  Showing {min(shown, len(stats))} of {len(stats)} items  |  "
        f"{len(profitable)} profitable  |  "
        f"{len(high_margin)} with >{HIGH_MARGIN_THRESHOLD:.0f}% margin"
        + (f"  |  {len(margin_warnings)} margin-drop warnings" if margin_warnings else "")
    )
    return lines


def render_cancel_expired(ce_stats: list[dict], realm_label: str) -> list[str]:
    lines = []
    lines.append(header(f"CANCELLED & EXPIRED LISTINGS — {realm_label}"))
    lines.append(f"  Items that failed to sell — candidates for repricing strategy review")
    lines.append(f"  Avg Buy Price shown where purchase history exists")
    lines.append(divider())
    col = (
        f"  {'Item':<36}  {'Cancels':>7}  {'Expired':>7}  {'Failed Qty':>10}  "
        f"{'Sold OK':>7}  {'Fail Rate':>9}  {'Avg Buy':>10}  Advice"
    )
    lines.append(col)
    lines.append(divider("─"))

    for c in ce_stats[:40]:
        if c["avg_buy_price"] is not None:
            buy_str = fmt_gold(c["avg_buy_price"])
        else:
            buy_str = f"{'—':>10}"

        if c["failure_rate"] >= 80:
            advice = "⚠  CONSISTENTLY FAILING — reprice or stop listing"
        elif c["failure_rate"] >= 50:
            advice = "△  High fail rate — review price vs market"
        elif c["cancels"] > 0 and c["expirations"] == 0:
            advice = "ℹ  Cancelled only — manual undercut response"
        else:
            advice = ""

        lines.append(
            f"  {item_label(c['item_id'])}  "
            f"{c['cancels']:>7}  "
            f"{c['expirations']:>7}  "
            f"{c['failed_qty']:>10}  "
            f"{c['sell_successes']:>7}  "
            f"{c['failure_rate']:>8.1f}%  "
            f"{buy_str}  "
            f"{advice}"
        )

    if not ce_stats:
        lines.append("  (no cancelled or expired listings found)")

    lines.append(divider("─"))
    total_cancels    = sum(c["cancels"]     for c in ce_stats)
    total_expirations = sum(c["expirations"] for c in ce_stats)
    consistently_bad = sum(1 for c in ce_stats if c["failure_rate"] >= 80)
    lines.append(
        f"  {len(ce_stats)} unique items  |  "
        f"{total_cancels} total cancels  |  "
        f"{total_expirations} total expirations  |  "
        f"{consistently_bad} items consistently failing (≥80%)"
    )
    return lines


def render_realm_summary(buckets: dict) -> list[str]:
    lines = []
    lines.append(header("DATA SUMMARY"))
    lines.append(f"  {'Realm':<22}  {'Type':<12}  {'Records':>8}  {'Auction Only':>13}")
    lines.append(divider("─"))

    realm_order = [PRIMARY_REALM] + sorted(
        {realm for realm, _ in buckets if realm != PRIMARY_REALM}
    )

    for realm in realm_order:
        for dtype in ("Buys", "Sales", "Cancelled", "Expired"):
            recs = buckets.get((realm, dtype), [])
            if not recs:
                continue
            auction_count = sum(1 for r in recs if r.get("source") == "Auction")
            marker = " ◀ primary" if realm == PRIMARY_REALM else ""
            lines.append(
                f"  {realm + marker:<30}  {dtype:<12}  {len(recs):>8}  {auction_count:>13}"
            )

    lines.append(divider("─"))
    total = sum(len(v) for v in buckets.values())
    lines.append(f"  Total records: {total}")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # --- Init price history DB ---
    price_history.init_db()
    run_ts = price_history.current_run_ts()

    records = load_data(DATA_FILE)
    buckets = partition(records)

    # Prefetch all item names up front (cached in item_names.json after first run)
    all_item_ids = list({r["item_id"] for r in records if r.get("item_id")})
    blizzard_api.prefetch_item_names(all_item_ids)

    all_lines: list[str] = []

    def section(*render_lines):
        all_lines.extend(render_lines)
        all_lines.append("")

    # --- Header ---
    all_lines.append(divider("═"))
    all_lines.append(f"{'TSM AUCTION PROFIT REPORT':^100}")
    all_lines.append(f"{'Primary Realm: ' + PRIMARY_REALM:^100}")
    all_lines.append(divider("═"))
    all_lines.append("")

    # --- Summary ---
    section(*render_realm_summary(buckets))

    # --- Malfurion profit analysis (primary) ---
    malf_buys  = buckets.get((PRIMARY_REALM, "Buys"),  [])
    malf_sales = buckets.get((PRIMARY_REALM, "Sales"), [])
    malf_stats = build_item_stats(malf_buys, malf_sales)

    # Trends: read BEFORE saving so we compare against the previous snapshot
    malf_trends = price_history.get_trends(malf_stats, PRIMARY_REALM, run_ts)
    price_history.save_snapshot(malf_stats, PRIMARY_REALM, run_ts)
    snap_n = price_history.snapshot_count(PRIMARY_REALM)

    section(*render_profit_table(malf_stats, PRIMARY_REALM, trends=malf_trends))

    # --- All-realm combined profit analysis ---
    all_buys  = [r for recs in [v for k, v in buckets.items() if k[1] == "Buys"]  for r in recs]
    all_sales = [r for recs in [v for k, v in buckets.items() if k[1] == "Sales"] for r in recs]
    all_stats = build_item_stats(all_buys, all_sales)
    section(*render_profit_table(all_stats, "ALL REALMS COMBINED"))

    # --- Per-realm profit tables for non-primary realms ---
    for realm in sorted({realm for realm, _ in buckets if realm != PRIMARY_REALM}):
        r_buys  = buckets.get((realm, "Buys"),  [])
        r_sales = buckets.get((realm, "Sales"), [])
        if r_buys and r_sales:
            r_stats = build_item_stats(r_buys, r_sales)
            section(*render_profit_table(r_stats, realm))

    # --- Malfurion cancelled/expired ---
    malf_cancelled = buckets.get((PRIMARY_REALM, "Cancelled"), [])
    malf_expired   = buckets.get((PRIMARY_REALM, "Expired"),   [])
    ce_stats = build_cancel_expired_stats(malf_cancelled, malf_expired, malf_sales, malf_buys)
    section(*render_cancel_expired(ce_stats, PRIMARY_REALM))

    # --- High-margin highlight block ---
    all_lines.append(header(f"HIGH-MARGIN ITEMS (>{HIGH_MARGIN_THRESHOLD:.0f}%) — MALFURION"))
    all_lines.append(divider("─"))
    hm = [s for s in malf_stats if s["margin_pct"] >= HIGH_MARGIN_THRESHOLD]
    if hm:
        for s in hm:
            t = malf_trends.get(s["item_id"]) if malf_trends else None
            trend_str = ""
            if t:
                icon = TREND_ICON.get(t["trend"], "?")
                trend_str = f"  {icon} {t['trend']}"
                if t["margin_warning"]:
                    trend_str += "  ⚠ MARGIN DROP"
            all_lines.append(
                f"  ★  {item_label(s['item_id'], width=38)}  |  "
                f"Buy {fmt_gold(s['avg_buy']).strip()}  →  "
                f"Sell {fmt_gold(s['avg_sell']).strip()}  |  "
                f"Profit {fmt_gold(s['profit_per_item']).strip()}/item  |  "
                f"Margin {s['margin_pct']:+.1f}%  |  "
                f"{s['buy_txns']} buys / {s['sell_txns']} sells"
                f"{trend_str}"
            )
    else:
        all_lines.append(f"  No Malfurion items exceeded {HIGH_MARGIN_THRESHOLD:.0f}% margin in this dataset.")
    all_lines.append(divider("─"))
    all_lines.append("")

    # --- Snapshot history ---
    all_lines.append(header(f"PRICE HISTORY — {PRIMARY_REALM} (snapshot {snap_n})"))
    if not malf_trends:
        all_lines.append(f"  Snapshot #{snap_n} saved. Trend data will appear on next run.")
    else:
        rising  = sum(1 for t in malf_trends.values() if t["trend"] == "RISING")
        falling = sum(1 for t in malf_trends.values() if t["trend"] == "FALLING")
        stable  = sum(1 for t in malf_trends.values() if t["trend"] == "STABLE")
        new     = sum(1 for t in malf_trends.values() if t["trend"] == "NEW")
        warns   = sum(1 for t in malf_trends.values() if t["margin_warning"])
        all_lines.append(
            f"  vs snapshot #{snap_n - 1}  |  "
            f"↑ {rising} rising  ↓ {falling} falling  → {stable} stable  ✦ {new} new  "
            f"{'|  ⚠ ' + str(warns) + ' margin-drop warnings' if warns else ''}"
        )
    all_lines.append(divider("─"))
    all_lines.extend(price_history.snapshot_summary(PRIMARY_REALM))
    all_lines.append(divider("─"))
    all_lines.append("")

    all_lines.append(divider("═"))
    all_lines.append(f"  Report generated from: {DATA_FILE}")
    all_lines.append(f"  Records analysed: {len(records)}")
    all_lines.append(divider("═"))

    output = "\n".join(all_lines)

    # Print to terminal
    print(output)

    # Save to file
    REPORT_FILE.write_text(output + "\n", encoding="utf-8")
    print(f"\n[Saved to {REPORT_FILE}]")


if __name__ == "__main__":
    main()
