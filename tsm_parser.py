#!/usr/bin/env python3
"""
TSM (TradeSkillMaster) SavedVariables parser.
Extracts auction/transaction data from the Lua file and outputs clean JSON.
"""

import re
import json
import csv
import io
from datetime import datetime, timezone
from pathlib import Path

LUA_PATH = Path(
    "/run/media/davidjsteinke/Games/Blizzard/World of Warcraft"
    "/_retail_/WTF/Account/QUESOMAN/SavedVariables/TradeSkillMaster.lua"
)
OUTPUT_PATH = Path("tsm_data.json")

# Keys that carry price + counterparty data
PRICED_KEYS = re.compile(
    r'\["r@([^@"]+)@internalData@(csvBuys|csvSales)"\]\s*=\s*"((?:[^"\\]|\\.)*)\"'
)
# Keys that only carry item + player (no price)
SLIM_KEYS = re.compile(
    r'\["r@([^@"]+)@internalData@(csvCancelled|csvExpired)"\]\s*=\s*"((?:[^"\\]|\\.)*)"'
)
# TSM 14-day region market value average (stored as "i:ID:copper,..." string)
MARKET_VALUE_KEYS = re.compile(
    r'\["r@([^@"]+)@internalData@(?:dbRegionMarketValueAvg|dbMarketValue|marketValue)"\]'
    r'\s*=\s*"((?:[^"\\]|\\.)*)"'
)


def copper_to_gold(copper: int) -> float:
    """Convert copper amount to gold (1 gold = 10000 copper)."""
    return round(copper / 10_000, 4)


def parse_item_string(item_string: str) -> dict:
    """
    Parse a TSM item string into its components.

    Simple:   i:237366
    Bonused:  i:246647::4:3210:10255:12239:12290
    """
    parts = item_string.split("::", 1)
    base = parts[0]  # e.g. "i:237366"
    bonus_ids = parts[1] if len(parts) > 1 else None

    item_id = int(base.split(":")[1]) if ":" in base else None
    return {
        "item_id": item_id,
        "item_string": item_string,
        "bonus_ids": bonus_ids,
    }


def parse_csv_block(realm: str, entry_type: str, raw_csv: str) -> list[dict]:
    """Parse a single CSV block into a list of record dicts."""
    # TSM stores newlines as literal \n inside the Lua string
    text = raw_csv.replace("\\n", "\n")
    reader = csv.DictReader(io.StringIO(text))

    records = []
    for row in reader:
        try:
            item_info = parse_item_string(row["itemString"].strip())
            record = {
                "realm": realm,
                "type": entry_type,
                "item_id": item_info["item_id"],
                "item_string": item_info["item_string"],
                "bonus_ids": item_info["bonus_ids"],
                "stack_size": int(row["stackSize"]),
                "quantity": int(row["quantity"]),
                "player": row["player"].strip(),
                "timestamp": int(row["time"]),
                "timestamp_utc": datetime.fromtimestamp(
                    int(row["time"]), tz=timezone.utc
                ).isoformat(),
            }

            if "price" in row:
                copper = int(row["price"])
                record["price_copper"] = copper
                record["price_gold"] = copper_to_gold(copper)
                record["price_per_item_gold"] = (
                    copper_to_gold(copper / int(row["quantity"]))
                    if int(row["quantity"]) > 0
                    else None
                )

            if "otherPlayer" in row:
                record["other_player"] = row["otherPlayer"].strip()

            if "source" in row:
                record["source"] = row["source"].strip()

            records.append(record)
        except (KeyError, ValueError) as exc:
            print(f"  [warn] skipping malformed row in {realm}/{entry_type}: {exc} — {row}")

    return records


def parse_market_values(lua_text: str) -> dict[int, float]:
    """
    Parse TSM 14-day market value averages from the Lua file.
    Handles the "i:ID:copper_value,..." string format TSM uses.
    Returns {item_id: gold_value}.
    """
    values: dict[int, float] = {}
    for match in MARKET_VALUE_KEYS.finditer(lua_text):
        data = match.group(2).replace("\\n", "")
        for entry in re.split(r"[,^]", data):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) >= 3 and parts[0] == "i":
                try:
                    item_id = int(parts[1])
                    copper = int(parts[-1])
                    if copper > 0 and item_id not in values:
                        values[item_id] = round(copper / 10_000, 4)
                except (ValueError, IndexError):
                    pass
    return values


def extract_all_records(lua_text: str) -> list[dict]:
    """Find every CSV block in the Lua file and parse it."""
    all_records: list[dict] = []

    for match in PRICED_KEYS.finditer(lua_text):
        realm, entry_type, raw_csv = match.group(1), match.group(2), match.group(3)
        # Skip header-only entries (no actual data rows)
        if "\n" not in raw_csv.replace("\\n", "\n"):
            continue
        recs = parse_csv_block(realm, entry_type, raw_csv)
        print(f"  {realm} / {entry_type}: {len(recs)} records")
        all_records.extend(recs)

    for match in SLIM_KEYS.finditer(lua_text):
        realm, entry_type, raw_csv = match.group(1), match.group(2), match.group(3)
        if "\n" not in raw_csv.replace("\\n", "\n"):
            continue
        recs = parse_csv_block(realm, entry_type, raw_csv)
        print(f"  {realm} / {entry_type}: {len(recs)} records")
        all_records.extend(recs)

    return all_records


def build_summary(records: list[dict]) -> dict:
    """Aggregate high-level stats for the output."""
    buys = [r for r in records if r["type"] == "csvBuys"]
    sales = [r for r in records if r["type"] == "csvSales"]

    def total_gold(recs):
        return round(sum(r.get("price_gold", 0) for r in recs), 4)

    return {
        "total_records": len(records),
        "buys": len(buys),
        "sales": len(sales),
        "cancelled": sum(1 for r in records if r["type"] == "csvCancelled"),
        "expired": sum(1 for r in records if r["type"] == "csvExpired"),
        "total_spent_gold": total_gold(buys),
        "total_earned_gold": total_gold(sales),
    }


def main():
    print(f"Reading {LUA_PATH} ...")
    lua_text = LUA_PATH.read_text(encoding="utf-8", errors="replace")
    print(f"File size: {len(lua_text):,} bytes\n")

    print("Extracting records...")
    records = extract_all_records(lua_text)

    # Sort by timestamp for readability
    records.sort(key=lambda r: r["timestamp"])

    summary = build_summary(records)
    print(f"\nSummary: {summary}")

    print("Extracting TSM market values...")
    try:
        market_values = parse_market_values(lua_text)
        print(f"  Found market values for {len(market_values)} items")
    except Exception as exc:
        print(f"  [warn] Market value parsing failed: {exc}")
        market_values = {}

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_file": str(LUA_PATH),
        "summary": summary,
        "records": records,
        "market_values": {str(k): v for k, v in market_values.items()},
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(records)} records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
