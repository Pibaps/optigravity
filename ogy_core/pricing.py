"""Versioned pricing catalogue and cost calculators (100% stdlib)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone


CARBON_PRICE_SWITCH_DATE = date(2026, 12, 1)
GEMINI_FLASH_PRICE_DOUBLE_DATE = date(2027, 1, 1)

BILLING_SOURCES = ("oauth_quota", "gemini_api_free", "gemini_api_tier1")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per 1M tokens and cache multipliers for a model."""

    model_id: str
    input_per_m: float
    output_per_m: float
    cache_read_mult: float
    cache_write_mult: float
    long_context_threshold: int = 200_000
    long_context_mult: float = 2.0
    long_context_output_mult: float | None = None
    free_tier_eligible: bool = False


def _parse_as_of_date(as_of: date | datetime | str | None) -> date:
    if as_of is None:
        return datetime.now(timezone.utc).date()
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    try:
        return date.fromisoformat(as_of[:10])
    except ValueError:
        return datetime.now(timezone.utc).date()


def normalize_model_id(model_id: str) -> str:
    """Normalize model aliases into canonical slugs."""
    m = (model_id or "").strip().lower()
    if "3.5-flash-lite" in m or "3.5_flash_lite" in m:
        return "gemini-3.5-flash-lite"
    if "3.1-flash-lite" in m or "3.1_flash_lite" in m:
        return "gemini-3.1-flash-lite"
    if "3.8-flash" in m or "3.8_flash" in m:
        return "gemini-3.8-flash"
    if "3.6-flash" in m or "3.6_flash" in m:
        return "gemini-3.6-flash"
    if "3.1-pro" in m or "3.1_pro" in m:
        return "gemini-3.1-pro"
    if "hydrogen" in m:
        return "gemini-4-hydrogen"
    if "neon" in m:
        return "gemini-4-neon"
    if "carbon" in m:
        return "gemini-4-carbon"
    if "argon" in m:
        return "gemini-4-argon"
    if "opus" in m:
        return "claude-opus-5.5"
    if "sonnet" in m:
        return "claude-sonnet-5.5"
    if "haiku" in m:
        return "claude-haiku-5.5"
    return m or "gemini-4-hydrogen"


def get_model_pricing(
    model_id: str,
    *,
    as_of: date | datetime | str | None = None,
    enterprise: bool = False,
    cache_ttl_1h: bool = False,
) -> ModelPricing:
    """Return pricing for a model on a given date."""
    slug = normalize_model_id(model_id)
    d = _parse_as_of_date(as_of)
    gem_cache_read = 0.10 if enterprise else 0.05

    # Gemini API late-2026 subagent catalogue
    if slug == "gemini-3.5-flash-lite":
        return ModelPricing(slug, 0.30, 2.50, gem_cache_read, 1.0, free_tier_eligible=True)
    if slug == "gemini-3.1-flash-lite":
        return ModelPricing(slug, 0.25, 1.50, gem_cache_read, 1.0, free_tier_eligible=True)
    if slug in ("gemini-3.6-flash", "gemini-3.8-flash"):
        if d >= GEMINI_FLASH_PRICE_DOUBLE_DATE:
            return ModelPricing(slug, 1.50, 7.50, gem_cache_read, 1.0)
        return ModelPricing(slug, 0.75, 3.75, gem_cache_read, 1.0)
    if slug == "gemini-3.1-pro":
        return ModelPricing(
            slug,
            2.00,
            12.00,
            gem_cache_read,
            1.0,
            long_context_threshold=200_000,
            long_context_mult=2.0,
            long_context_output_mult=1.5,
        )

    # Antigravity / OAuth primary models
    if slug == "gemini-4-hydrogen":
        return ModelPricing(slug, 1.50, 7.50, gem_cache_read, 1.0)
    if slug == "gemini-4-neon":
        return ModelPricing(slug, 3.00, 15.00, gem_cache_read, 1.0)
    if slug in ("gemini-4-carbon", "gemini-4-argon"):
        if d >= CARBON_PRICE_SWITCH_DATE:
            return ModelPricing(slug, 4.00, 20.00, gem_cache_read, 1.0)
        return ModelPricing(slug, 2.00, 10.00, gem_cache_read, 1.0)
    if slug == "claude-opus-5.5":
        return ModelPricing(
            slug, 4.00, 20.00, 0.05, 2.0 if cache_ttl_1h else 1.25
        )
    if slug == "claude-sonnet-5.5":
        return ModelPricing(
            slug, 3.00, 15.00, 0.05, 2.0 if cache_ttl_1h else 1.25
        )
    if slug == "claude-haiku-5.5":
        return ModelPricing(
            slug,
            1.00,
            5.00,
            0.05,
            2.0 if cache_ttl_1h else 1.25,
            long_context_threshold=100_000,
            long_context_mult=5.0,
        )
    # Safe fallback (Gemini 4 Hydrogen tier)
    return ModelPricing(slug, 1.50, 7.50, gem_cache_read, 1.0)


def estimate_turn_cost(
    model_id: str,
    *,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    thoughts_token_count: int | None = None,
    billing_source: str = "oauth_quota",
    as_of: date | datetime | str | None = None,
    enterprise: bool = False,
    cache_ttl_1h: bool = False,
) -> float:
    """Estimate USD cost for a single turn.

    Bills `(output_tokens + thinking_tokens) * output_rate` (or `thoughts_token_count`
    when passed from Gemini API `usage_metadata`). Returns `$0.0` for `gemini_api_free`.
    """
    if billing_source == "gemini_api_free":
        return 0.0

    p = get_model_pricing(
        model_id, as_of=as_of, enterprise=enterprise, cache_ttl_1h=cache_ttl_1h
    )
    total_ctx = input_tokens + cache_read_tokens + cache_creation_tokens
    is_long = total_ctx > p.long_context_threshold
    in_mult = p.long_context_mult if is_long else 1.0
    out_mult = (
        (p.long_context_output_mult if p.long_context_output_mult is not None else p.long_context_mult)
        if is_long
        else 1.0
    )

    eff_thinking = thoughts_token_count if thoughts_token_count is not None else thinking_tokens
    in_cost = (input_tokens / 1_000_000) * p.input_per_m * in_mult
    read_cost = (cache_read_tokens / 1_000_000) * p.input_per_m * p.cache_read_mult * in_mult
    write_cost = (
        (cache_creation_tokens / 1_000_000) * p.input_per_m * p.cache_write_mult * in_mult
    )
    out_cost = ((output_tokens + eff_thinking) / 1_000_000) * p.output_per_m * out_mult
    return round(in_cost + read_cost + write_cost + out_cost, 6)


def estimate_gemini_api_cost(
    model_id: str,
    *,
    prompt_token_count: int = 0,
    candidates_token_count: int = 0,
    thoughts_token_count: int = 0,
    cached_content_token_count: int = 0,
    billing_source: str = "gemini_api_tier1",
    as_of: date | datetime | str | None = None,
) -> float:
    """Estimate USD cost from Gemini API `usage_metadata` fields.

    Explicitly charges `(candidates_token_count + thoughts_token_count) * output_rate`
    and distinguishes `billing_source` (`oauth_quota`, `gemini_api_free`, `gemini_api_tier1`).
    """
    non_cached_input = max(0, int(prompt_token_count) - int(cached_content_token_count))
    return estimate_turn_cost(
        model_id,
        input_tokens=non_cached_input,
        cache_read_tokens=max(0, int(cached_content_token_count)),
        output_tokens=max(0, int(candidates_token_count)),
        thoughts_token_count=max(0, int(thoughts_token_count)),
        billing_source=billing_source,
        as_of=as_of,
    )


def switch_tax_usd(
    prefix_tokens: int,
    target_model_id: str,
    *,
    as_of: date | datetime | str | None = None,
    enterprise: bool = False,
) -> float:
    """Compute cache-invalidation penalty (H_t * P_write / 1e6) when switching mid-session."""
    if prefix_tokens <= 0:
        return 0.0
    p = get_model_pricing(target_model_id, as_of=as_of, enterprise=enterprise)
    return round((prefix_tokens / 1_000_000) * p.input_per_m * p.cache_write_mult, 6)


def effort_downgrade_breakeven_thinking_tokens(
    prefix_tokens: int,
    model_id: str,
    *,
    as_of: date | datetime | str | None = None,
    enterprise: bool = False,
) -> int:
    """Compute minimum thinking tokens to save to break even on prefix cache busting:
    Delta_think >= (m_w - m_r) * (p_in / p_out) * C_prefix.
    """
    if prefix_tokens <= 0:
        return 0
    p = get_model_pricing(model_id, as_of=as_of, enterprise=enterprise)
    if p.output_per_m <= 0:
        return 0
    delta = (p.cache_write_mult - p.cache_read_mult) * (p.input_per_m / p.output_per_m) * prefix_tokens
    return max(0, int(round(delta)))
