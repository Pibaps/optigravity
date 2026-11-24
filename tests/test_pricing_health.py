"""Unit tests for pricing (including 2026-12-01 switch) and context health."""

from __future__ import annotations

from datetime import date

from ogy_core.context_health import assess_context_health, k_star_threshold
from ogy_core.pricing import (
    effort_downgrade_breakeven_thinking_tokens,
    estimate_turn_cost,
    get_model_pricing,
    switch_tax_usd,
)


def test_carbon_price_switch_dec_2026() -> None:
    before = get_model_pricing("gemini-4-carbon", as_of=date(2026, 11, 30))
    after = get_model_pricing("gemini-4-carbon", as_of=date(2026, 12, 1))
    assert before.input_per_m == 2.00
    assert before.output_per_m == 10.00
    assert after.input_per_m == 4.00
    assert after.output_per_m == 20.00


def test_opus_switch_tax_and_breakeven() -> None:
    # 100K prefix on Opus 5.5: write mult 1.25x * $4.00 = $0.50 (or diff 1.20x * $4/M * 100k = $0.48)
    tax = switch_tax_usd(100_000, "claude-opus-5.5")
    assert tax == 0.50
    delta_think = effort_downgrade_breakeven_thinking_tokens(100_000, "claude-opus-5.5")
    assert delta_think == 24_000


def test_haiku_long_context_cliff() -> None:
    cost_normal = estimate_turn_cost("claude-haiku-5.5", input_tokens=50_000, output_tokens=1_000)
    cost_cliff = estimate_turn_cost("claude-haiku-5.5", input_tokens=110_000, output_tokens=1_000)
    assert cost_cliff > cost_normal * 5


def test_context_health_zones_and_k_star() -> None:
    assert k_star_threshold("gemini-4-neon") == 5
    assert k_star_threshold("claude-opus-5.5") == 9

    green = assess_context_health(context_tokens=30_000, turn_count=5, cache_hit_ratio=0.90)
    assert green.zone == "green"
    assert green.action == "keep_warm"

    amber = assess_context_health(context_tokens=75_000, turn_count=10, cache_hit_ratio=0.85)
    assert amber.zone == "amber"
    assert amber.action == "frugal_btw"

    red_handoff = assess_context_health(
        context_tokens=115_000, turn_count=20, remaining_turns_est=2, model_id="gemini-4-neon"
    )
    assert red_handoff.zone == "red"
    assert red_handoff.action == "clear_handoff"

    red_compact = assess_context_health(
        context_tokens=115_000, turn_count=20, remaining_turns_est=6, model_id="gemini-4-neon"
    )
    assert red_compact.zone == "red"
    assert red_compact.action == "compact"

    red_rewind = assess_context_health(
        context_tokens=40_000, turn_count=6, consecutive_failures=2
    )
    assert red_rewind.zone == "red"
    assert red_rewind.action == "rewind"
