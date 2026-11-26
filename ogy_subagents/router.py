"""Resilience router for Gemini API 429 (RPM/TPM vs RPD) & 402 with privacy_mode enforcement.

Enforces two distinct Google Cloud projects:
- Project A (`free`): Free Tier (0 USD), used ONLY when `privacy_mode == "free_first"`.
- Project B (`tier1`): Paid / Google AI Pro Credits (Tier 1), mandatory in `privacy_mode == "tier1_only"`.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ogy_core.pricing import estimate_gemini_api_cost
from ogy_subagents.gemini_client import SubagentLoopResult, create_genai_client
from ogy_subagents.secrets import SecretResolution, redact_secrets, resolve_api_key

QuotaErrorKind = Literal["rpm_tpm", "rpd", "rpd_ambiguous", "billing_402", "other"]

RPM_BACKOFF_BASE_SECONDS: tuple[float, float] = (1.5, 4.0)
AMBIGUOUS_429_COOLDOWN_SECONDS: float = 15.0 * 60.0


class SubagentRouterError(RuntimeError):
    """Base error raised by the resilient subagent router."""


class SubagentPrivacyError(SubagentRouterError):
    """Raised when subagents are disabled or privacy policy blocks execution."""


class SubagentBillingError(SubagentRouterError):
    """Raised on HTTP 402 (prepaid credits/balance exhausted on Tier 1)."""


class SubagentQuotaExhaustedError(SubagentRouterError):
    """Raised when all eligible tiers are exhausted or in cooldown."""


@dataclass(slots=True)
class RoutedExecutionResult:
    """Result returned by `ResilientGeminiRouter.execute()` with billing & fallback metadata."""

    result: SubagentLoopResult
    tier: str  # "free" or "tier1"
    billing_source: str  # "gemini_api_free" or "gemini_api_tier1"
    cost_usd: float
    fallback_notice: str | None = None
    key_source: str = ""


def seconds_until_midnight_pt(now_utc: datetime | None = None) -> float:
    """Compute seconds remaining until next midnight Pacific Time (`America/Los_Angeles`)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        pt_tz: Any = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        # Fallback to UTC-8 when system tzdata is unavailable (e.g., minimal Windows Python)
        pt_tz = timezone(timedelta(hours=-8))
    now_pt = now.astimezone(pt_tz)
    next_midnight = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (next_midnight - now_pt).total_seconds()
    return max(60.0, delta)


def classify_api_error(exc: Exception) -> QuotaErrorKind:
    """Inspect Google API error status code and description to classify 429/402 causes."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    details = getattr(exc, "details", None) or getattr(exc, "message", None) or ""
    text = f"{code or ''} {details} {exc}".lower()

    if code == 402 or "402" in text or "payment required" in text or "insufficient balance" in text or "billing account" in text:
        return "billing_402"

    is_429 = (
        code == 429
        or "429" in text
        or "resource_exhausted" in text
        or "resource exhausted" in text
        or "quota" in text
    )
    if not is_429:
        return "other"

    # RPD (Requests Per Day / daily quota) -> circuit-breaker until midnight PT
    rpd_markers = (
        "perday",
        "per_day",
        "per day",
        "requestsperday",
        "rpd",
        "daily",
        "free_tier_requests",
    )
    if any(m in text for m in rpd_markers):
        return "rpd"

    # RPM / TPM (Per-Minute) -> short exponential backoff + jitter (max 2 retries)
    rpm_markers = (
        "perminute",
        "per_minute",
        "per minute",
        "requestsperminute",
        "tokensperminute",
        "rpm",
        "tpm",
        "rate limit",
    )
    if any(m in text for m in rpm_markers):
        return "rpm_tpm"

    # Ambiguous 429 -> 15 min cooldown
    return "rpd_ambiguous"


class ResilientGeminiRouter:
    """Routes Gemini API calls across Project A (Free Tier) and Project B (Tier 1 Credits)."""

    def __init__(
        self,
        *,
        privacy_mode: str = "tier1_only",
        fallback_to_tier1: bool = True,
        client_factory: Callable[[str], Any] | None = None,
        secret_resolver: Callable[[str], SecretResolution] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        clock_fn: Callable[[], float] | None = None,
        jitter_fn: Callable[[], float] | None = None,
    ) -> None:
        norm_priv = (privacy_mode or "tier1_only").strip().lower()
        if norm_priv not in ("tier1_only", "free_first", "disabled"):
            norm_priv = "tier1_only"
        self.privacy_mode = norm_priv
        self.fallback_to_tier1 = bool(fallback_to_tier1)
        self._client_factory = client_factory or create_genai_client
        self._secret_resolver = secret_resolver or (lambda t: resolve_api_key(t))
        self._sleep = sleep_fn or asyncio.sleep
        self._clock = clock_fn or time.monotonic
        self._jitter = jitter_fn or (lambda: random.uniform(0.0, 0.35))
        self._cooldown_until: dict[str, float] = {}

    def is_in_cooldown(self, tier: str) -> bool:
        until = self._cooldown_until.get(tier, 0.0)
        return self._clock() < until

    def cooldown_remaining_s(self, tier: str) -> float:
        return max(0.0, self._cooldown_until.get(tier, 0.0) - self._clock())

    def _set_cooldown(self, tier: str, duration_s: float) -> None:
        self._cooldown_until[tier] = self._clock() + max(1.0, duration_s)

    def _eligible_tiers(self) -> list[str]:
        if self.privacy_mode == "disabled":
            raise SubagentPrivacyError(
                "MCP subagents are disabled (`subagents.privacy_mode = \"disabled\"`)."
            )
        if self.privacy_mode == "tier1_only":
            # STRICT: never route private/proprietary code to Project A (Free Tier)
            return ["tier1"]
        # free_first
        tiers: list[str] = []
        if not self.is_in_cooldown("free"):
            tiers.append("free")
        if self.fallback_to_tier1 and not self.is_in_cooldown("tier1"):
            tiers.append("tier1")
        return tiers

    async def execute(
        self,
        *,
        model_id: str,
        runner: Callable[[Any, str], Awaitable[SubagentLoopResult]],
    ) -> RoutedExecutionResult:
        """Execute `runner(client, tier)` with RPM retry, RPD circuit-breaker, and Tier 1 fallback."""
        tiers = self._eligible_tiers()
        if not tiers:
            raise SubagentQuotaExhaustedError(
                "All eligible Gemini API tiers are currently in cooldown."
            )

        last_error: Exception | None = None
        fallback_triggered = False

        for idx, tier in enumerate(tiers):
            sec = self._secret_resolver(tier)
            if not sec.key:
                if idx < len(tiers) - 1:
                    continue
                hint = f" ({sec.warning})" if sec.warning else ""
                raise SubagentQuotaExhaustedError(
                    f"No Gemini API key resolved for tier '{tier}'{hint}. "
                    f"Set OGY_GEMINI_API_KEY_{tier.upper()} or GEMINI_API_KEY."
                )

            client = self._client_factory(sec.key)
            max_attempts = 1 + len(RPM_BACKOFF_BASE_SECONDS)

            for attempt in range(max_attempts):
                try:
                    res = await runner(client, tier)
                    billing_source = "gemini_api_free" if tier == "free" else "gemini_api_tier1"
                    cost = estimate_gemini_api_cost(
                        model_id,
                        prompt_token_count=res.usage.prompt_token_count,
                        candidates_token_count=res.usage.candidates_token_count,
                        thoughts_token_count=res.usage.thoughts_token_count,
                        cached_content_token_count=res.usage.cached_content_token_count,
                        billing_source=billing_source,
                    )
                    notice: str | None = None
                    if fallback_triggered and tier == "tier1":
                        notice = (
                            f"[subagent fallback: Free Tier -> Tier 1 credits · "
                            f"est. ${cost:.5f} ({model_id})]"
                        )
                    return RoutedExecutionResult(
                        result=res,
                        tier=tier,
                        billing_source=billing_source,
                        cost_usd=cost,
                        fallback_notice=notice,
                        key_source=sec.source,
                    )
                except Exception as exc:
                    last_error = exc
                    kind = classify_api_error(exc)

                    if kind == "billing_402":
                        self._set_cooldown(tier, 3600.0)
                        raise SubagentBillingError(
                            redact_secrets(
                                f"Gemini API 402 on tier '{tier}': prepaid balance / credits exhausted ({exc})"
                            )
                        ) from exc

                    if kind == "rpm_tpm" and attempt < len(RPM_BACKOFF_BASE_SECONDS):
                        delay = RPM_BACKOFF_BASE_SECONDS[attempt] + float(self._jitter())
                        await self._sleep(delay)
                        continue

                    if kind == "rpd":
                        self._set_cooldown(tier, seconds_until_midnight_pt())
                        if tier == "free" and idx < len(tiers) - 1:
                            fallback_triggered = True
                            break
                        raise SubagentQuotaExhaustedError(
                            redact_secrets(f"Gemini API RPD exhausted on tier '{tier}' (cooldown until midnight PT).")
                        ) from exc

                    if kind == "rpd_ambiguous" or (kind == "rpm_tpm" and attempt >= len(RPM_BACKOFF_BASE_SECONDS)):
                        self._set_cooldown(tier, AMBIGUOUS_429_COOLDOWN_SECONDS)
                        if tier == "free" and idx < len(tiers) - 1:
                            fallback_triggered = True
                            break
                        raise SubagentQuotaExhaustedError(
                            redact_secrets(f"Gemini API 429 on tier '{tier}' (cooldown 15 min).")
                        ) from exc

                    # Non-quota error -> do not retry or silently switch tier
                    raise SubagentRouterError(redact_secrets(str(exc))) from exc

        raise SubagentQuotaExhaustedError(
            redact_secrets(f"Subagent execution failed across tiers: {last_error}")
        )
