"""Assisted dynamic orchestration, Switch Tax calculator, and multi-session rotation (V2)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ogy_core.config import OgyConfig, load_config
from ogy_core.context_health import ContextHealth, assess_context_health, write_handoff
from ogy_core.ingest import get_latest_quota_snapshots
from ogy_core.pricing import normalize_model_id, switch_tax_usd
from ogy_core.storage import connect_readonly


@dataclass(slots=True)
class SessionTelemetryState:
    """In-memory live telemetry for an active ACP session."""

    session_id: str
    model_id: str = "gemini-4-hydrogen"
    effort: str = "medium"
    mode: str = "auto"
    turn_count: int = 0
    input_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    context_tokens: int = 0
    context_max: int = 1_000_000
    total_cost_usd: float = 0.0
    consecutive_failures: int = 0
    last_error_summary: str = ""
    modified_files: list[str] = field(default_factory=list)
    last_turn_monotonic: float = field(default_factory=time.monotonic)
    active_goal: str = "Continue current task"

    @property
    def cache_hit_ratio(self) -> float | None:
        denom = self.input_tokens + self.cached_read_tokens + self.cached_write_tokens
        if denom <= 0:
            return None
        return self.cached_read_tokens / denom

    @property
    def idle_gap_s(self) -> float:
        if self.turn_count == 0:
            return 0.0
        return max(0.0, time.monotonic() - self.last_turn_monotonic)

    def assess_health(self) -> ContextHealth:
        return assess_context_health(
            context_tokens=self.context_tokens,
            turn_count=self.turn_count,
            cache_hit_ratio=self.cache_hit_ratio,
            idle_gap_s=self.idle_gap_s,
            exceeds_200k=self.context_tokens > 200_000,
            consecutive_failures=self.consecutive_failures,
            model_id=self.model_id,
        )


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """One-key user-confirmed action banner in the TUI (ToS Section 6 compliant)."""

    kind: str  # "rotate_session", "upgrade_config", "delegate_subagent", "switch_to_v1_run"
    hotkey: str  # "r", "u", "d", "v"
    title: str
    detail: str
    target_model: str | None = None
    target_effort: str | None = None
    switch_tax_usd: float = 0.0


class SessionOrchestrator:
    """Coordinates Switch Tax calculation, >60K delegation, Red-Zone rotation, and ToS guardrails."""

    CACHE_PRESERVE_THRESHOLD = 60_000

    def __init__(self, *, ogy_config: OgyConfig | None = None, auth_method: str = "oauth-personal") -> None:
        self.ogy_config = ogy_config or load_config()
        self.auth_method = auth_method

    def check_model_compatibility(self, requested_model: str) -> ActionProposal | None:
        """ToS Section 6 & ACP scope: Claude 5.5 under oauth-personal routes to V1 `ogy run`."""
        norm = normalize_model_id(requested_model)
        if self.auth_method == "oauth-personal" and norm.startswith("claude-"):
            return ActionProposal(
                kind="switch_to_v1_run",
                hotkey="v",
                title=f"Claude ({norm}) -> basculer vers `ogy run` (V1)",
                detail=(
                    "Sous OAuth personnel, `ogy tui` (ACP) opère sur Gemini 4. "
                    f"Utilisez `ogy run --model {norm}` pour conserver 100% du quota Claude 5.5."
                ),
                target_model=norm,
            )
        return None

    def compute_switch_tax(self, state: SessionTelemetryState, target_model: str) -> float:
        """Compute exact prefix cache invalidation tax (H_t * P_write / 1e6)."""
        return switch_tax_usd(
            state.context_tokens,
            target_model,
            enterprise=self.ogy_config.enterprise_cache_pricing,
        )

    def evaluate_proposals(self, state: SessionTelemetryState) -> list[ActionProposal]:
        """Return prioritized 1-key proposals based on context health and consecutive failures."""
        proposals: list[ActionProposal] = []
        health = state.assess_health()

        if health.zone == "red":
            proposals.append(
                ActionProposal(
                    kind="rotate_session",
                    hotkey="r",
                    title="[R] Rotation propre (handoff -> close -> new)",
                    detail=f"Zone rouge: {health.reason}. Écrit .ogy/handoff.md et ouvre une session fraîche.",
                )
            )

        if state.consecutive_failures >= 2:
            target_model = "gemini-4-neon"
            target_effort = "high" if state.effort in ("low", "medium") else state.effort
            tax = self.compute_switch_tax(state, target_model)

            if state.context_tokens > self.CACHE_PRESERVE_THRESHOLD:
                proposals.append(
                    ActionProposal(
                        kind="delegate_subagent",
                        hotkey="d",
                        title=f"[D] Déléguer à un sous-agent ({target_model}/{target_effort})",
                        detail=(
                            f"Contexte {state.context_tokens:,} > 60K : déléguer préserve le cache parent "
                            f"(évite Switch Tax ${tax:.4f})."
                        ),
                        target_model=target_model,
                        target_effort=target_effort,
                        switch_tax_usd=tax,
                    )
                )
            else:
                proposals.append(
                    ActionProposal(
                        kind="upgrade_config",
                        hotkey="u",
                        title=f"[U] Escalader vers {target_model} ({target_effort})",
                        detail=f"{state.consecutive_failures} échecs consécutifs. Switch Tax estimée : ${tax:.4f}.",
                        target_model=target_model,
                        target_effort=target_effort,
                        switch_tax_usd=tax,
                    )
                )

        return proposals

    async def execute_rotation(
        self,
        client: Any,
        state: SessionTelemetryState,
        *,
        cwd: str = ".",
        next_step: str = "Resume task from handoff",
    ) -> tuple[str, str, str]:
        """Perform clean multi-session rotation: handoff -> close_session -> new_session."""
        checks = state.last_error_summary or "See recent tool outputs"
        neg = []
        if state.consecutive_failures >= 2:
            neg.append("Do not repeat failing approach without changing strategy")
        res = write_handoff(
            goal=state.active_goal,
            modified_files=state.modified_files,
            checks=checks,
            negative_constraints=neg,
            next_step=next_step,
            workspace_path=cwd,
        )
        old_sid = state.session_id
        await client.close_session(old_sid)
        new_sid = await client.new_session(cwd=cwd)
        return new_sid, str(res["path"]), str(res["content"])

    def read_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        """Read latest quota_snapshots from ogy.db read-only (never polls agy in a loop)."""
        if not self.ogy_config.db_path.is_file():
            return {}
        try:
            conn = connect_readonly(self.ogy_config.db_path)
            try:
                return get_latest_quota_snapshots(conn)
            finally:
                conn.close()
        except Exception:
            return {}
