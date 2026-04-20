#!/usr/bin/env python3
"""
Milling margin analysis for Midnight Inscription.

Milling converts herbs into pigments, which are used to craft inks and
ultimately glyphs, vantus runes, and other inscription items.

Midnight milling mechanics (documented assumptions):
  - Each herb mills in batches of 5, yielding approximately PIGMENT_YIELD
    pigments per herb on average (before profession skill/specialisation modifiers).
  - Each herb type mills into its corresponding named pigment of the same quality
    tier.  Tranquility Bloom and Azeroot have no named pigment — they yield
    Powder Pigment (the base/generic pigment used for lower-value items).
  - Nocturnal Lotus is excluded: it is a rare special herb not sold in normal
    AH quantities and likely yields a unique craftable not modelled here.

To update yield when character specialisation is added, change PIGMENT_YIELD.
"""

from pathlib import Path

# ─────────────────────────────────────────────────────────
# Configurable constants
# ─────────────────────────────────────────────────────────

PIGMENT_YIELD   = 1.0   # pigments produced per herb on average (no spec bonus)
MILL_BATCH_SIZE = 5     # herbs consumed per single mill cast
AH_CUT          = 0.05  # Auction House cut applied when selling pigments

# ─────────────────────────────────────────────────────────
# Herb → Pigment mappings
# Each entry: herb_item_id → (herb_name, quality_tier, pigment_item_id, pigment_quality_tier)
#
# Sources / evidence:
#   - Argentleaf, Sanguithorn, Mana Lily: named pigments match herb names exactly.
#   - Tranquility Bloom, Azeroot: no named pigment exists; assumed → Powder Pigment
#     (the generic base pigment at same tier). Confirm with wowhead/Midnight PTR data.
# ─────────────────────────────────────────────────────────

HERB_PIGMENT_MAP: dict[int, tuple[str, str, int, str]] = {
    # Tranquility Bloom (T1/T2) → Powder Pigment [ASSUMED — no named pigment]
    236761: ("Tranquility Bloom", "T1", 245807, "T1"),
    236767: ("Tranquility Bloom", "T2", 245808, "T2"),
    # Sanguithorn (T1/T2) → Sanguithorn Pigment
    236770: ("Sanguithorn",       "T1", 245864, "T1"),
    236771: ("Sanguithorn",       "T2", 245865, "T2"),
    # Azeroot (T1/T2) → Powder Pigment [ASSUMED — no named pigment]
    236774: ("Azeroot",           "T1", 245807, "T1"),
    236775: ("Azeroot",           "T2", 245808, "T2"),
    # Argentleaf (T1/T2) → Argentleaf Pigment
    236776: ("Argentleaf",        "T1", 245803, "T1"),
    236777: ("Argentleaf",        "T2", 245804, "T2"),
    # Mana Lily (T1/T2) → Mana Lily Pigment
    236778: ("Mana Lily",         "T1", 245866, "T1"),
    236779: ("Mana Lily",         "T2", 245867, "T2"),
}

# ─────────────────────────────────────────────────────────
# Pigment display names (for dashboard rendering)
# ─────────────────────────────────────────────────────────

PIGMENT_NAMES: dict[int, str] = {
    245807: "Powder Pigment",
    245808: "Powder Pigment",
    245803: "Argentleaf Pigment",
    245804: "Argentleaf Pigment",
    245864: "Sanguithorn Pigment",
    245865: "Sanguithorn Pigment",
    245866: "Mana Lily Pigment",
    245867: "Mana Lily Pigment",
}


# ─────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────

def build_milling_analysis(
    prices: dict[tuple[int, str], float],
) -> list[dict]:
    """
    Calculate milling margins for all mapped herbs.

    Args:
        prices: dict keyed by (item_id, quality_tier) → min_buyout on Malfurion

    Returns:
        List of row dicts sorted by profit_per_herb descending.
    """
    results = []

    for herb_id, (herb_name, herb_tier, pigment_id, pig_tier) in HERB_PIGMENT_MAP.items():
        herb_price  = prices.get((herb_id,  herb_tier))
        pig_price   = prices.get((pigment_id, pig_tier))

        if herb_price is None or pig_price is None:
            continue  # skip if either side not on AH

        pigment_name = PIGMENT_NAMES.get(pigment_id, f"Pigment {pigment_id}")

        # Expected revenue per herb: yield × pigment AH price × (1 - cut)
        revenue_per_herb = PIGMENT_YIELD * pig_price * (1.0 - AH_CUT)
        profit_per_herb  = revenue_per_herb - herb_price

        # Per-batch figures (5 herbs)
        cost_per_batch     = herb_price * MILL_BATCH_SIZE
        revenue_per_batch  = revenue_per_herb * MILL_BATCH_SIZE
        profit_per_batch   = profit_per_herb * MILL_BATCH_SIZE

        margin_pct = (profit_per_herb / herb_price * 100) if herb_price else 0.0

        results.append({
            "herb_id":           herb_id,
            "herb_name":         herb_name,
            "quality_tier":      herb_tier,
            "pigment_id":        pigment_id,
            "pigment_name":      pigment_name,
            "pigment_tier":      pig_tier,
            "herb_price":        round(herb_price, 4),
            "pigment_price":     round(pig_price, 4),
            "yield_per_herb":    PIGMENT_YIELD,
            "revenue_per_herb":  round(revenue_per_herb, 4),
            "profit_per_herb":   round(profit_per_herb, 4),
            "cost_per_batch":    round(cost_per_batch, 4),
            "revenue_per_batch": round(revenue_per_batch, 4),
            "profit_per_batch":  round(profit_per_batch, 4),
            "margin_pct":        round(margin_pct, 2),
        })

    results.sort(key=lambda r: r["profit_per_herb"], reverse=True)
    return results
