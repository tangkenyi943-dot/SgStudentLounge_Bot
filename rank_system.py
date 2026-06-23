"""
Rank/tier system.

Translates a user's all-time total points (across all games) into a
named rank: Bronze/Silver/Gold, each with 3 sublevels. Purely a display
layer on top of points_store's existing totals — no separate point
tracking of its own, so there's nothing to keep in sync or migrate.

Thresholds are deliberately spaced given the points economy across
Wordle (up to ~50/day), Fishing (variable, luck-based), Daily Trivia
(30/correct), and 1v1 Trivia (2-6/correct, volume-based): Bronze is
reachable almost immediately, Silver takes a real week of engagement,
Gold takes sustained weeks/months of play.
"""

# Ordered from lowest to highest. Each tuple is (tier, sublevel, min_points).
# A user's rank is the highest entry whose min_points they've met or exceeded.
RANK_THRESHOLDS = [
    ("Bronze", 1, 0),
    ("Bronze", 2, 100),
    ("Bronze", 3, 300),
    ("Silver", 1, 750),
    ("Silver", 2, 1500),
    ("Silver", 3, 3000),
    ("Gold", 1, 5000),
    ("Gold", 2, 10000),
    ("Gold", 3, 20000),
]

TIER_EMOJI = {"Bronze": "🥉", "Silver": "🥈", "Gold": "🥇"}


def get_rank(total_points: int) -> dict:
    """
    Returns {"tier": str, "sublevel": int, "emoji": str, "label": str,
    "min_points": int, "next_threshold": int | None, "points_to_next": int | None}.
    next_threshold/points_to_next are None when already at the max rank
    (Gold 3), since there's nothing further to progress toward.
    """
    total_points = max(0, total_points)

    current = RANK_THRESHOLDS[0]
    current_index = 0
    for i, (tier, sublevel, min_points) in enumerate(RANK_THRESHOLDS):
        if total_points >= min_points:
            current = (tier, sublevel, min_points)
            current_index = i
        else:
            break

    tier, sublevel, min_points = current
    emoji = TIER_EMOJI[tier]
    label = f"{emoji} {tier} {sublevel}"

    next_threshold = None
    points_to_next = None
    if current_index + 1 < len(RANK_THRESHOLDS):
        next_threshold = RANK_THRESHOLDS[current_index + 1][2]
        points_to_next = next_threshold - total_points

    return {
        "tier": tier,
        "sublevel": sublevel,
        "emoji": emoji,
        "label": label,
        "min_points": min_points,
        "next_threshold": next_threshold,
        "points_to_next": points_to_next,
    }


def get_rank_image_filename(tier: str) -> str:
    """Returns the filename (not full path) of the pixel-art image for a tier."""
    return f"rank_{tier.lower()}.png"


def get_rank_sticker_filename(tier: str) -> str:
    """Returns the filename of the sticker-format (512px, transparent) version."""
    return f"rank_{tier.lower()}_sticker.png"
