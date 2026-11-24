"""Context health zones, K* breakeven, and cold-cache detection (100% stdlib)."""

from __future__ import annotations

from dataclasses import dataclass

from ogy_core.pricing import normalize_model_id


@dataclass(frozen=True, slots=True)
class ContextHealth:
    """Assessed context health state and recommended action."""

    zone: str  # "green", "amber", "red"
    action: str  # "keep_warm", "frugal_btw", "clear_handoff", "compact", "rewind"
    reason: str
    cache_hit_pct: float | None
    idle_warning: str | None
    pricing_cliff_alert: str | None
    k_star_min_turns: int


def k_star_threshold(model_id: str) -> int:
    """Minimum remaining turns K* where /compact breaks even vs /clear + handoff."""
    slug = normalize_model_id(model_id)
    if slug.startswith("claude-"):
        return 9
    return 5


def assess_context_health(
    *,
    context_tokens: int,
    turn_count: int,
    cache_hit_ratio: float | None = None,
    idle_gap_s: float = 0.0,
    exceeds_200k: bool = False,
    consecutive_failures: int = 0,
    remaining_turns_est: int = 0,
    model_id: str = "gemini-4-hydrogen",
) -> ContextHealth:
    """Evaluate green/amber/red zone and recommend the cheapest safe action."""
    slug = normalize_model_id(model_id)
    k_star = k_star_threshold(slug)
    cache_pct = round(cache_hit_ratio * 100.0, 1) if cache_hit_ratio is not None else None

    idle_warning: str | None = None
    if idle_gap_s >= 300:
        idle_warning = "CACHE FROID -> /ogy-handoff"
    elif idle_gap_s >= 150 and not slug.startswith("claude-"):
        idle_warning = "pre-eviction (>150s)"

    pricing_cliff_alert: str | None = None
    if slug == "claude-haiku-5.5" and context_tokens > 90_000:
        pricing_cliff_alert = "Haiku >90K (5x cliff at 100K)"
    elif not slug.startswith("claude-") and context_tokens > 180_000:
        pricing_cliff_alert = "Approaching 200K long-context cliff"

    # Red zone conditions
    if consecutive_failures >= 2:
        return ContextHealth(
            zone="red",
            action="rewind",
            reason=f"{consecutive_failures} consecutive failures (never compact errors)",
            cache_hit_pct=cache_pct,
            idle_warning=idle_warning,
            pricing_cliff_alert=pricing_cliff_alert,
            k_star_min_turns=k_star,
        )

    if (
        context_tokens >= 100_000
        or exceeds_200k
        or (idle_gap_s > 180 and context_tokens > 60_000)
    ):
        if remaining_turns_est >= k_star and not exceeds_200k and idle_gap_s <= 300:
            return ContextHealth(
                zone="red",
                action="compact",
                reason=f"Context {context_tokens} >= 100K with >= {k_star} turns left",
                cache_hit_pct=cache_pct,
                idle_warning=idle_warning,
                pricing_cliff_alert=pricing_cliff_alert,
                k_star_min_turns=k_star,
            )
        return ContextHealth(
            zone="red",
            action="clear_handoff",
            reason="Context saturated or cache cold; /clear + handoff >50% cheaper",
            cache_hit_pct=cache_pct,
            idle_warning=idle_warning,
            pricing_cliff_alert=pricing_cliff_alert,
            k_star_min_turns=k_star,
        )

    # Amber zone conditions
    if (
        60_000 <= context_tokens < 100_000
        or 15 <= turn_count < 25
        or (cache_hit_ratio is not None and cache_hit_ratio < 0.60 and turn_count >= 3)
    ):
        return ContextHealth(
            zone="amber",
            action="frugal_btw",
            reason="Amber zone: stay frugal, use /btw for side questions",
            cache_hit_pct=cache_pct,
            idle_warning=idle_warning,
            pricing_cliff_alert=pricing_cliff_alert,
            k_star_min_turns=k_star,
        )

    return ContextHealth(
        zone="green",
        action="keep_warm",
        reason="Healthy warm context",
        cache_hit_pct=cache_pct,
        idle_warning=idle_warning,
        pricing_cliff_alert=pricing_cliff_alert,
        k_star_min_turns=k_star,
    )
