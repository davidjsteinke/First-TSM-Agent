#!/usr/bin/env python3
"""
TSM Cross-Realm Arbitrage Analyser

For each item appearing in auction data on 2+ realms, compares the
weighted-average market price between realms and flags opportunities
where the spread (after the 5% AH cut) exceeds MIN_SPREAD_PCT.

Price source priority per realm:
  1. Auction Sales records  — direct evidence of what the item sells for
  2. Auction Buys records   — fallback for realms that have purchase but no
                              sale history (e.g. Maelstrom, Mal'Ganis)

Appends an ARBITRAGE section to report.txt (creates the file if missing).
Also prints to stdout so run_agent.sh captures it in the log.
"""

import json
from collections import defaultdict
from pathlib import Path

import blizzard_api

DATA_FILE   = Path("/home/davidjsteinke/tsm_data.json")
REPORT_FILE = Path("/home/davidjsteinke/report.txt")

AH_CUT          = 0.05    # Blizzard takes 5% on successful sales
MIN_SPREAD_PCT  = 20.0    # minimum profit % to report
MIN_TXNS        = 1       # minimum transactions on each side to trust the price


# ---------------------------------------------------------------------------
# Helpers (duplicated from agent.py to keep this module self-contained)
# ---------------------------------------------------------------------------

def divider(char: str = "─", width: int = 100) -> str:
    return char * width


def header(title: str, width: int = 100) -> str:
    pad = (width - len(title) - 2) // 2
    return f"{'═' * pad} {title} {'═' * (width - pad - len(title) - 2)}"


def item_label(item_id: int, width: int = 38) -> str:
    name = blizzard_api.get_item_name(item_id)
    full = f"{name} ({item_id})"
    return full[:width].ljust(width) if len(full) > width else full.ljust(width)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["records"]


# ---------------------------------------------------------------------------
# Price aggregation
# ---------------------------------------------------------------------------

def build_realm_prices(records: list[dict]) -> dict[int, dict[str, dict]]:
    """
    Returns:
        {
            item_id: {
                realm: {
                    "avg_price":  float,   # weighted avg unit price in gold
                    "txns":       int,     # number of transactions
                    "total_qty":  int,
                    "source":     "Sales" | "Buys",
                }
            }
        }

    Sales records take priority over Buys records for the same item+realm.
    """
    # Accumulate raw data: item -> realm -> source -> [(unit_price, qty)]
    raw: dict[int, dict[str, dict[str, list]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for r in records:
        if r.get("source") != "Auction":
            continue
        dtype = r.get("type")
        if dtype not in ("Sales", "Buys"):
            continue
        iid  = r.get("item_id")
        qty  = r.get("quantity") or 1
        gold = r.get("price_gold")
        if not iid or not gold:
            continue
        realm = r["realm"]
        unit  = gold / qty
        raw[iid][realm][dtype].append((unit, qty))

    # Resolve: prefer Sales, fall back to Buys
    result: dict[int, dict[str, dict]] = {}
    for iid, realms in raw.items():
        result[iid] = {}
        for realm, sources in realms.items():
            if "Sales" in sources:
                chosen, source_label = sources["Sales"], "Sales"
            else:
                chosen, source_label = sources["Buys"], "Buys"

            total_qty  = sum(q for _, q in chosen)
            avg_price  = sum(p * q for p, q in chosen) / total_qty
            result[iid][realm] = {
                "avg_price":  avg_price,
                "txns":       len(chosen),
                "total_qty":  total_qty,
                "source":     source_label,
            }

    return result


# ---------------------------------------------------------------------------
# Arbitrage detection
# ---------------------------------------------------------------------------

def find_opportunities(realm_prices: dict[int, dict[str, dict]]) -> list[dict]:
    """
    For every item on 2+ realms, test all ordered (buy_realm, sell_realm) pairs.
    Keep the single best pair per item (highest net profit).
    """
    opportunities = []

    for iid, realms in realm_prices.items():
        if len(realms) < 2:
            continue

        realm_list = list(realms.items())
        best = None

        for i, (buy_realm, buy_data) in enumerate(realm_list):
            for j, (sell_realm, sell_data) in enumerate(realm_list):
                if i == j:
                    continue
                if buy_data["txns"] < MIN_TXNS or sell_data["txns"] < MIN_TXNS:
                    continue

                buy_price  = buy_data["avg_price"]
                sell_gross = sell_data["avg_price"]
                sell_net   = sell_gross * (1 - AH_CUT)
                profit     = sell_net - buy_price
                spread_pct = (profit / buy_price * 100) if buy_price else 0.0

                if spread_pct < MIN_SPREAD_PCT:
                    continue

                if best is None or profit > best["profit_per_item"]:
                    best = {
                        "item_id":       iid,
                        "buy_realm":     buy_realm,
                        "buy_price":     buy_price,
                        "buy_txns":      buy_data["txns"],
                        "buy_source":    buy_data["source"],
                        "sell_realm":    sell_realm,
                        "sell_gross":    sell_gross,
                        "sell_net":      sell_net,
                        "sell_txns":     sell_data["txns"],
                        "sell_source":   sell_data["source"],
                        "profit_per_item": profit,
                        "spread_pct":    spread_pct,
                        "total_txns":    buy_data["txns"] + sell_data["txns"],
                    }

        if best:
            opportunities.append(best)

    opportunities.sort(key=lambda x: x["profit_per_item"], reverse=True)
    return opportunities


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_arbitrage(opportunities: list[dict]) -> list[str]:
    lines = []
    lines.append(header("CROSS-REALM ARBITRAGE OPPORTUNITIES"))
    lines.append(f"  Price spread > {MIN_SPREAD_PCT:.0f}% after {AH_CUT*100:.0f}% AH cut  |  "
                 f"ranked by net profit per item")
    lines.append(f"  Source key: S=Sales history  B=Buys history")
    lines.append(divider())

    col = (
        f"  {'Item':<38}  "
        f"{'Buy Realm':<14} {'Buy Price':>10} {'Src':>3}  "
        f"{'Sell Realm':<14} {'Sell Net':>10} {'Src':>3}  "
        f"{'Profit':>10}  {'Spread':>8}  {'Txns':>5}"
    )
    lines.append(col)
    lines.append(divider("─"))

    if not opportunities:
        lines.append(f"  No cross-realm arbitrage opportunities found above {MIN_SPREAD_PCT:.0f}% threshold.")
    else:
        for opp in opportunities:
            lines.append(
                f"  {item_label(opp['item_id'], 38)}  "
                f"{opp['buy_realm']:<14} {opp['buy_price']:>9.2f}g "
                f"{opp['buy_source'][0]:>3}  "
                f"{opp['sell_realm']:<14} {opp['sell_net']:>9.2f}g "
                f"{opp['sell_source'][0]:>3}  "
                f"{opp['profit_per_item']:>9.2f}g  "
                f"{opp['spread_pct']:>+7.1f}%  "
                f"{opp['total_txns']:>5}"
            )

    lines.append(divider("─"))
    if opportunities:
        total_profit = sum(o["profit_per_item"] for o in opportunities)
        lines.append(
            f"  {len(opportunities)} opportunities found  |  "
            f"avg profit {total_profit / len(opportunities):.2f}g/item  |  "
            f"best: {item_label(opportunities[0]['item_id'], 30).strip()} "
            f"@ +{opportunities[0]['profit_per_item']:.2f}g ({opportunities[0]['spread_pct']:+.1f}%)"
        )
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    records      = load_records(DATA_FILE)
    realm_prices = build_realm_prices(records)

    # Prefetch names for all items in multi-realm set
    multi_realm_ids = [iid for iid, realms in realm_prices.items() if len(realms) >= 2]
    blizzard_api.prefetch_item_names(multi_realm_ids)

    opportunities = find_opportunities(realm_prices)
    section_lines = render_arbitrage(opportunities)

    output = "\n".join(["", *section_lines, ""])
    print(output)

    # Append to report.txt (create if missing)
    with open(REPORT_FILE, "a", encoding="utf-8") as f:
        f.write(output)
    print(f"[Arbitrage section appended to {REPORT_FILE}]")


if __name__ == "__main__":
    main()
