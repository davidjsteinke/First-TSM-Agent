#!/usr/bin/env python3
"""
Prospecting margin analysis for Midnight Jewelcrafting.

Prospecting converts 5 ore into a random mix of gems.  The expected value of
a prospect is the probability-weighted average gem output value minus the cost
of 5 ores, accounting for the 5% AH cut when selling gems.

Drop rate assumptions (Midnight — estimated from TWW/Dragonflight patterns;
update when wowhead/PTR data is available for Midnight):
  - Lower-tier ores produce mostly vendor-trash gems (Duskshrouded Stone,
    Radiant Shard) with small chances of valuable garnets.
  - Higher-tier ores (Silver, Thorium) have better odds of rare/epic gems and
    fewer junk outputs.
  - Gem quantities per prospect are fractional averages (e.g. 0.4 = 40% chance
    of receiving 1 of that gem from a single prospect of 5 ore).

All drop rates are APPROXIMATE and documented so they can be revised.
"""

# ─────────────────────────────────────────────────────────
# Configurable constants
# ─────────────────────────────────────────────────────────

PROSPECT_BATCH_SIZE = 5   # ore consumed per prospect
AH_CUT              = 0.05

# ─────────────────────────────────────────────────────────
# Expected gem outputs per 5-ore prospect
# Format: ore_item_id → list of (gem_item_id, quality_tier, avg_qty_per_prospect)
#
# Gem item IDs:
#   Duskshrouded Stone    T1=242788  T2=242789   (junk, ~3g)
#   Radiant Shard         T1=243602  T2=243603   (junk, ~4-5g)
#   Crystalline Glass     T1=242786  T2=242787   (T1=235g, T2=2.5g — T2 is worthless)
#   Glimmering Gemdust    T1=242620  T2=242621   (~211g / 631g)
#   Deadly Garnet         T1=240871  T2=240872
#   Quick Garnet          T1=240873  T2=240874
#   Masterful Garnet      T1=240875  T2=240876
#   Versatile Garnet      T1=240877  T2=240878
#   Sanguine Garnet       T1=242553  T2=242723
#   Eversong Diamond      T1=242608  T2=242712
#   Flawless Deadly Garnet   T1=240903 T2=240904
#   Flawless Quick Garnet    T1=240905 T2=240906
#   Flawless Masterful Garnet T1=240907 T2=240908
#   Flawless Versatile Garnet T1=240909 T2=240910
#   Dawn Crystal          T1=243605  T2=243606
#   Powerful Eversong Diamond T1=240966 T2=240967
#   Telluric Eversong Diamond T1=240968 T2=240969
#   Stoic Eversong Diamond    T1=240970 T2=240971
#   Indecipherable Eversong Diamond T1=240982 T2=240983
# ─────────────────────────────────────────────────────────

# Helper type alias
_GemOutput = list[tuple[int, str, float]]

ORE_PROSPECT_MAP: dict[int, tuple[str, str, _GemOutput]] = {
    # ── Refulgent Copper Ore T1 (item 237359) ──────────────────────────────
    # Basic ore; mostly junk gems, small chance of T1 garnets.
    # ASSUMED rates based on TWW T1-equivalent ore.
    237359: ("Refulgent Copper Ore", "T1", [
        (242788, "T1", 1.2),   # Duskshrouded Stone T1 (~3g) — most common output
        (243602, "T1", 0.8),   # Radiant Shard T1 (~4g)
        (240877, "T1", 0.25),  # Versatile Garnet T1 (~5g — low value)
        (240871, "T1", 0.15),  # Deadly Garnet T1 (~51g)
        (240875, "T1", 0.10),  # Masterful Garnet T1 (~47g)
        (242620, "T1", 0.05),  # Glimmering Gemdust T1 (~211g)
    ]),

    # ── Refulgent Copper Ore T2 (item 237361) ──────────────────────────────
    # T2 copper; fewer junk, better garnet yields.
    # ASSUMED rates based on TWW T1-equivalent T2 quality.
    237361: ("Refulgent Copper Ore", "T2", [
        (242788, "T1", 0.6),   # Duskshrouded Stone T1 (~3g)
        (243602, "T2", 0.4),   # Radiant Shard T2 (~5g)
        (240872, "T2", 0.5),   # Deadly Garnet T2 (~428g)
        (240876, "T2", 0.3),   # Masterful Garnet T2 (~388g)
        (240878, "T2", 0.2),   # Versatile Garnet T2 (~364g)
        (242621, "T2", 0.15),  # Glimmering Gemdust T2 (~631g)
        (242723, "T2", 0.05),  # Sanguine Garnet T2 (~611g)
    ]),

    # ── Umbral Tin Ore T1 (item 237362) ────────────────────────────────────
    # Mid-tier ore; mix of junk and useful gems.
    # ASSUMED rates.
    237362: ("Umbral Tin Ore", "T1", [
        (242788, "T1", 0.8),   # Duskshrouded Stone T1 (~3g)
        (243602, "T1", 0.5),   # Radiant Shard T1 (~4g)
        (240871, "T1", 0.3),   # Deadly Garnet T1 (~51g)
        (240875, "T1", 0.25),  # Masterful Garnet T1 (~47g)
        (242786, "T1", 0.2),   # Crystalline Glass T1 (~236g)
        (242620, "T1", 0.15),  # Glimmering Gemdust T1 (~211g)
        (242553, "T1", 0.05),  # Sanguine Garnet T1 (~161g)
        (242608, "T1", 0.03),  # Eversong Diamond T1 (~198g)
    ]),

    # ── Umbral Tin Ore T2 (item 237363) ────────────────────────────────────
    # ASSUMED rates.
    237363: ("Umbral Tin Ore", "T2", [
        (242789, "T2", 0.5),   # Duskshrouded Stone T2 (~3g)
        (240872, "T2", 0.4),   # Deadly Garnet T2 (~428g)
        (240876, "T2", 0.35),  # Masterful Garnet T2 (~388g)
        (242621, "T2", 0.25),  # Glimmering Gemdust T2 (~631g)
        (242723, "T2", 0.12),  # Sanguine Garnet T2 (~611g)
        (242712, "T2", 0.08),  # Eversong Diamond T2 (~580g)
        (240904, "T2", 0.04),  # Flawless Deadly Garnet T2 (~2606g)
    ]),

    # ── Brilliant Silver Ore T1 (item 237364) ──────────────────────────────
    # Higher-tier ore; fewer junk, more valuable gems.
    # ASSUMED rates.
    237364: ("Brilliant Silver Ore", "T1", [
        (243602, "T1", 0.4),   # Radiant Shard T1 (~4g)
        (240871, "T1", 0.3),   # Deadly Garnet T1 (~51g)
        (242786, "T1", 0.25),  # Crystalline Glass T1 (~236g)
        (242620, "T1", 0.2),   # Glimmering Gemdust T1 (~211g)
        (242553, "T1", 0.15),  # Sanguine Garnet T1 (~161g)
        (242608, "T1", 0.10),  # Eversong Diamond T1 (~198g)
        (240903, "T1", 0.05),  # Flawless Deadly Garnet T1 (~545g)
        (243605, "T1", 0.02),  # Dawn Crystal T1 (~900g)
    ]),

    # ── Brilliant Silver Ore T2 (item 237365) ──────────────────────────────
    # ASSUMED rates.
    237365: ("Brilliant Silver Ore", "T2", [
        (242723, "T2", 0.35),  # Sanguine Garnet T2 (~611g)
        (242712, "T2", 0.25),  # Eversong Diamond T2 (~580g)
        (242621, "T2", 0.20),  # Glimmering Gemdust T2 (~631g)
        (240904, "T2", 0.12),  # Flawless Deadly Garnet T2 (~2606g)
        (240908, "T2", 0.08),  # Flawless Masterful Garnet T2 (~2721g)
        (243606, "T2", 0.04),  # Dawn Crystal T2 (~1501g)
        (240967, "T2", 0.02),  # Powerful Eversong Diamond T2 (~5000g)
    ]),

    # ── Dazzling Thorium (item 237366, no tier) ────────────────────────────
    # Rare/endgame ore; premium gem outputs.
    # ASSUMED rates — Thorium is the top mining node.
    237366: ("Dazzling Thorium", "", [
        (242723, "T2", 0.4),   # Sanguine Garnet T2 (~611g)
        (240908, "T2", 0.3),   # Flawless Masterful Garnet T2 (~2721g)
        (240904, "T2", 0.25),  # Flawless Deadly Garnet T2 (~2606g)
        (243606, "T2", 0.15),  # Dawn Crystal T2 (~1501g)
        (240967, "T2", 0.08),  # Powerful Eversong Diamond T2 (~5000g)
        (240969, "T2", 0.06),  # Telluric Eversong Diamond T2 (~3000g)
        (240983, "T2", 0.04),  # Indecipherable Eversong Diamond T2 (~4000g)
    ]),
}


# ─────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────

def build_prospecting_analysis(
    prices: dict[tuple[int, str], float],
) -> list[dict]:
    """
    Calculate prospecting margins for all mapped ores.

    Args:
        prices: dict keyed by (item_id, quality_tier) → min_buyout on Malfurion

    Returns:
        List of row dicts sorted by profit_per_batch descending.
    """
    results = []

    for ore_id, (ore_name, ore_tier, gem_outputs) in ORE_PROSPECT_MAP.items():
        ore_price = prices.get((ore_id, ore_tier))
        if ore_price is None:
            continue

        cost_per_batch = ore_price * PROSPECT_BATCH_SIZE

        # Expected gem revenue: sum of (gem_price × avg_qty × (1-cut)) for each output
        expected_revenue = 0.0
        gem_detail: list[dict] = []
        any_gem_priced = False

        for gem_id, gem_tier, avg_qty in gem_outputs:
            gem_price = prices.get((gem_id, gem_tier))
            if gem_price is None:
                continue
            any_gem_priced = True
            contrib = gem_price * avg_qty * (1.0 - AH_CUT)
            expected_revenue += contrib
            gem_detail.append({
                "gem_id":    gem_id,
                "gem_tier":  gem_tier,
                "avg_qty":   avg_qty,
                "gem_price": round(gem_price, 4),
                "contrib":   round(contrib, 4),
            })

        if not any_gem_priced:
            continue

        profit_per_batch = expected_revenue - cost_per_batch
        margin_pct = (profit_per_batch / cost_per_batch * 100) if cost_per_batch else 0.0

        results.append({
            "ore_id":            ore_id,
            "ore_name":          ore_name,
            "quality_tier":      ore_tier,
            "ore_price":         round(ore_price, 4),
            "batch_size":        PROSPECT_BATCH_SIZE,
            "cost_per_batch":    round(cost_per_batch, 4),
            "expected_revenue":  round(expected_revenue, 4),
            "profit_per_batch":  round(profit_per_batch, 4),
            "margin_pct":        round(margin_pct, 2),
            "gem_detail":        gem_detail,
            # Convenience fields for dashboard table
            "profit_per_ore":    round(profit_per_batch / PROSPECT_BATCH_SIZE, 4),
        })

    results.sort(key=lambda r: r["profit_per_batch"], reverse=True)
    return results
