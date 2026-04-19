#!/usr/bin/env python3
"""
Live Auction House database — stores and queries AH snapshots from the Blizzard AH API.

Database: ~/tsm-agent/live_ah.db (separate from tsm_history.db)

Schema:
  ah_snapshots: one row per (item, realm, snapshot timestamp)
    - min_price: lowest current buyout per unit (gold)
    - avg_price: average buyout per unit across all listings (gold)
    - total_quantity: sum of all listed quantities
    - listing_count: number of separate auction entries
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).parent / "live_ah.db"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ah_snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc  TEXT    NOT NULL,
    realm          TEXT    NOT NULL,
    item_id        INTEGER NOT NULL,
    quality_tier   TEXT    NOT NULL DEFAULT '',
    min_price      REAL    NOT NULL,
    avg_price      REAL    NOT NULL,
    total_quantity INTEGER NOT NULL,
    listing_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ah_realm_item ON ah_snapshots (realm, item_id, quality_tier);
CREATE INDEX IF NOT EXISTS idx_ah_timestamp  ON ah_snapshots (timestamp_utc);
"""


def init_db() -> None:
    """
    Create the database and schema.  If an outdated schema is detected
    (missing quality_tier column) the table is dropped and recreated —
    live AH data is refreshed every 5 minutes so the loss is trivial.
    """
    with sqlite3.connect(DB_FILE) as conn:
        try:
            conn.execute("SELECT quality_tier FROM ah_snapshots LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("live_ah.db: schema outdated — dropping and recreating (quality_tier added)")
            conn.executescript(
                "DROP TABLE IF EXISTS ah_snapshots;"
                "DROP INDEX IF EXISTS idx_ah_realm_item;"
                "DROP INDEX IF EXISTS idx_ah_timestamp;"
            )
        conn.executescript(_SCHEMA)
    logger.debug(f"live_ah.db ready at {DB_FILE}")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_snapshot(realm: str, filtered_auctions: list[dict],
                  timestamp: datetime | None = None) -> int:
    """
    Aggregate filtered_auctions by item_id and insert one summary row per item.
    Runs prune_old_snapshots(28) first to keep the database lean.
    Returns number of items saved.
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)
    ts_str = timestamp.isoformat()

    # Aggregate per (item_id, quality_tier)
    aggregated: dict[tuple, dict] = {}
    for a in filtered_auctions:
        iid   = a["item_id"]
        qt    = a.get("quality_tier", "")
        price = a["buyout_per_unit"]
        qty   = a.get("quantity", 1)
        key   = (iid, qt)

        if key not in aggregated:
            aggregated[key] = {"prices": [], "total_qty": 0, "listing_count": 0}

        aggregated[key]["prices"].append(price)
        aggregated[key]["total_qty"] += qty
        aggregated[key]["listing_count"] += 1

    rows = []
    for (iid, qt), d in aggregated.items():
        min_p = min(d["prices"])
        avg_p = sum(d["prices"]) / len(d["prices"])
        rows.append((ts_str, realm, iid, qt, min_p, avg_p, d["total_qty"], d["listing_count"]))

    prune_old_snapshots(28)

    with sqlite3.connect(DB_FILE) as conn:
        conn.executemany(
            "INSERT INTO ah_snapshots "
            "(timestamp_utc, realm, item_id, quality_tier, min_price, avg_price, total_quantity, listing_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    logger.info(f"Saved {len(rows)} item snapshots for {realm} at {ts_str}")
    return len(rows)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_latest_snapshot(realm: str, item_id: int,
                        quality_tier: str = "") -> dict | None:
    """
    Return the most recent snapshot row for (realm, item_id, quality_tier).
    Pass quality_tier='' for items with no quality tier.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT * FROM ah_snapshots
            WHERE realm = ? AND item_id = ? AND quality_tier = ?
            ORDER BY timestamp_utc DESC LIMIT 1
            """,
            (realm, item_id, quality_tier),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_price_history(item_id: int, realm: str, days: int = 7,
                      quality_tier: str = "") -> list[dict]:
    """
    Return all snapshot rows for (item_id, quality_tier, realm) within the
    last `days` days, ordered oldest → newest.
    """
    from datetime import timedelta
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT * FROM ah_snapshots
            WHERE realm = ? AND item_id = ? AND quality_tier = ? AND timestamp_utc >= ?
            ORDER BY timestamp_utc ASC
            """,
            (realm, item_id, quality_tier, since),
        )
        return [dict(r) for r in cur.fetchall()]


def get_all_latest_snapshots(realm: str) -> list[dict]:
    """
    Return the most recent snapshot row for every (item_id, quality_tier)
    combination in the given realm.  Used by generate_dashboard.py.
    """
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT s.*
            FROM ah_snapshots s
            INNER JOIN (
                SELECT realm, item_id, quality_tier, MAX(timestamp_utc) AS max_ts
                FROM ah_snapshots
                WHERE realm = ?
                GROUP BY realm, item_id, quality_tier
            ) latest
              ON s.realm        = latest.realm
             AND s.item_id      = latest.item_id
             AND s.quality_tier = latest.quality_tier
             AND s.timestamp_utc = latest.max_ts
            WHERE s.realm = ?
            ORDER BY s.min_price ASC
            """,
            (realm, realm),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def prune_old_snapshots(days_to_keep: int = 28) -> int:
    """
    Delete snapshot rows older than `days_to_keep` days.
    Returns number of rows deleted.
    """
    from datetime import timedelta
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days_to_keep)).isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "DELETE FROM ah_snapshots WHERE timestamp_utc < ?", (cutoff,)
        )
        deleted = cur.rowcount
    if deleted:
        logger.info(f"Pruned {deleted} snapshot rows older than {days_to_keep} days")
    return deleted


def snapshot_stats() -> dict:
    """Return basic stats about the database (row count, oldest/newest timestamp, realm list)."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) as total, MIN(timestamp_utc) as oldest, "
            "MAX(timestamp_utc) as newest FROM ah_snapshots"
        ).fetchone()
        realms = [r[0] for r in conn.execute(
            "SELECT DISTINCT realm FROM ah_snapshots ORDER BY realm"
        ).fetchall()]
    return {
        "total_rows": row["total"],
        "oldest": row["oldest"],
        "newest": row["newest"],
        "realms": realms,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    init_db()
    stats = snapshot_stats()
    print(f"live_ah.db stats: {stats}")
