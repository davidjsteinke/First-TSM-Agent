#!/usr/bin/env python3
"""
Orchestrator for the live AH refresh pipeline.
Called by refresh_live_ah.sh every 5 minutes.

Steps:
  1. Fetch AH data for all 5 realms via Blizzard API
  2. Filter to Reagents + Consumables (classes 0, 5, 7)
  3. Save snapshots to live_ah.db (prune > 28 days old)
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import blizzard_ah
import live_ah_db

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    logger.info("Starting live AH refresh pipeline")
    ts = datetime.now(tz=timezone.utc)

    live_ah_db.init_db()

    # Fetch all realms (commodity + per-realm regular auctions)
    try:
        realm_data = blizzard_ah.fetch_all_realms()
    except Exception as exc:
        logger.error(f"fetch_all_realms failed: {exc}")
        sys.exit(1)

    # Save snapshots to DB
    total_saved = 0
    for realm, auctions in realm_data.items():
        if not auctions:
            logger.warning(f"{realm}: no auctions returned — skipping")
            continue
        n = live_ah_db.save_snapshot(realm, auctions, ts)
        logger.info(f"{realm}: saved {n} item snapshots")
        total_saved += n

    logger.info(
        f"Refresh complete — {total_saved} total snapshots saved  "
        f"|  {blizzard_ah._request_count} API requests  "
        f"|  DB: {live_ah_db.DB_FILE}"
    )

    stats = live_ah_db.snapshot_stats()
    logger.info(f"DB stats: {stats}")


if __name__ == "__main__":
    main()
