#!/usr/bin/env python3
"""
TSM Price History — SQLite-backed snapshot tracker.

Each call to save_snapshot() writes one row per item for the current run.
get_trends() compares the current computed stats against the most recent
previous snapshot to produce RISING/FALLING/STABLE labels and margin warnings.

Usage from agent.py:
    import price_history
    ts = price_history.current_run_ts()
    trends = price_history.get_trends(stats, realm, ts)   # read before saving
    price_history.save_snapshot(stats, realm, ts)         # then persist
"""

import sqlite3
import time
from pathlib import Path

DB_FILE = Path("/home/davidjsteinke/tsm_history.db")

# Sell price must move by this fraction to be called RISING/FALLING
TREND_THRESHOLD = 0.02       # 2%
# Margin drop (in percentage points) that triggers a WARNING
MARGIN_DROP_WARNING = 10.0


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_ts  INTEGER NOT NULL,
                realm        TEXT    NOT NULL,
                item_id      INTEGER NOT NULL,
                avg_buy      REAL    NOT NULL,
                avg_sell     REAL    NOT NULL,
                margin_pct   REAL    NOT NULL,
                buy_txns     INTEGER NOT NULL,
                sell_txns    INTEGER NOT NULL,
                total_volume INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snap_realm_ts
            ON snapshots (realm, snapshot_ts)
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_snapshot(stats: list[dict], realm: str, snapshot_ts: int) -> int:
    """
    Persist all items from a computed stats list as one snapshot batch.
    Returns the number of rows written.
    """
    if not stats:
        return 0

    rows = [
        (
            snapshot_ts,
            realm,
            s["item_id"],
            s["avg_buy"],
            s["avg_sell"],
            s["margin_pct"],
            s["buy_txns"],
            s["sell_txns"],
            s["total_volume"],
        )
        for s in stats
    ]

    with _connect() as conn:
        conn.executemany("""
            INSERT INTO snapshots
                (snapshot_ts, realm, item_id, avg_buy, avg_sell,
                 margin_pct, buy_txns, sell_txns, total_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

    return len(rows)


# ---------------------------------------------------------------------------
# Read — previous snapshot
# ---------------------------------------------------------------------------

def _get_prev_snapshot_ts(realm: str, before_ts: int) -> int | None:
    """Return the most recent snapshot_ts for realm that is strictly before before_ts."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT MAX(snapshot_ts) AS ts
            FROM snapshots
            WHERE realm = ? AND snapshot_ts < ?
        """, (realm, before_ts)).fetchone()
    ts = row["ts"] if row else None
    return ts


def _load_snapshot(realm: str, snapshot_ts: int) -> dict[int, dict]:
    """Load all items from a specific snapshot as {item_id: row_dict}."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT item_id, avg_buy, avg_sell, margin_pct, buy_txns, sell_txns, total_volume
            FROM snapshots
            WHERE realm = ? AND snapshot_ts = ?
        """, (realm, snapshot_ts)).fetchall()
    return {r["item_id"]: dict(r) for r in rows}


def snapshot_count(realm: str) -> int:
    """How many distinct snapshots exist for this realm."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT COUNT(DISTINCT snapshot_ts) AS n
            FROM snapshots WHERE realm = ?
        """, (realm,)).fetchone()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Trend computation
# ---------------------------------------------------------------------------

def get_trends(current_stats: list[dict], realm: str, current_ts: int) -> dict[int, dict]:
    """
    Compare current_stats against the most recent snapshot that predates current_ts.

    Returns a dict keyed by item_id:
        {
            "trend":          "RISING" | "FALLING" | "STABLE" | "NEW",
            "sell_delta":     float,   # avg_sell change in gold
            "margin_delta":   float,   # margin_pct change in pct points
            "margin_warning": bool,    # True if margin dropped > MARGIN_DROP_WARNING
            "prev_sell":      float,
            "prev_margin":    float,
            "prev_ts":        int,
        }

    Items with no previous data get trend="NEW".
    Returns {} if fewer than 1 prior snapshot exists (i.e. this is the first run).
    """
    prev_ts = _get_prev_snapshot_ts(realm, current_ts)
    if prev_ts is None:
        return {}

    prev = _load_snapshot(realm, prev_ts)

    trends: dict[int, dict] = {}
    for s in current_stats:
        iid = s["item_id"]
        if iid not in prev:
            trends[iid] = {
                "trend": "NEW", "sell_delta": 0.0, "margin_delta": 0.0,
                "margin_warning": False, "prev_sell": 0.0, "prev_margin": 0.0,
                "prev_ts": prev_ts,
            }
            continue

        p = prev[iid]
        sell_delta   = s["avg_sell"] - p["avg_sell"]
        margin_delta = s["margin_pct"] - p["margin_pct"]

        if p["avg_sell"] > 0:
            sell_ratio = sell_delta / p["avg_sell"]
        else:
            sell_ratio = 0.0

        if sell_ratio > TREND_THRESHOLD:
            trend = "RISING"
        elif sell_ratio < -TREND_THRESHOLD:
            trend = "FALLING"
        else:
            trend = "STABLE"

        trends[iid] = {
            "trend":          trend,
            "sell_delta":     sell_delta,
            "margin_delta":   margin_delta,
            "margin_warning": margin_delta < -MARGIN_DROP_WARNING,
            "prev_sell":      p["avg_sell"],
            "prev_margin":    p["margin_pct"],
            "prev_ts":        prev_ts,
        }

    return trends


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def current_run_ts() -> int:
    """Single timestamp for the entire current run."""
    return int(time.time())


def snapshot_summary(realm: str) -> list[str]:
    """Return human-readable lines summarising snapshot history for a realm."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT snapshot_ts, COUNT(*) AS item_count
            FROM snapshots
            WHERE realm = ?
            GROUP BY snapshot_ts
            ORDER BY snapshot_ts DESC
            LIMIT 10
        """, (realm,)).fetchall()

    if not rows:
        return [f"  No snapshots yet for {realm}."]

    import datetime
    lines = [f"  {'Snapshot Time':<26}  {'Items':>6}"]
    lines.append("  " + "─" * 36)
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["snapshot_ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"  {dt:<26}  {r['item_count']:>6}")
    return lines
