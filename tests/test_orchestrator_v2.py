"""Tests for V2 SessionOrchestrator (Switch Tax, >60K delegation, rotation, ToS guardrails)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ogy_core.config import load_config
from ogy_core.pricing import switch_tax_usd
from ogy_core.router import RouteDecision, evaluate_bandit_offline, record_routing_decision
from ogy_core.storage import connect, ensure_session, write_tx
from ogy_frontends.acp_tui.orchestrator import SessionOrchestrator, SessionTelemetryState


def test_tos_claude_guardrail_under_oauth_personal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    orch = SessionOrchestrator(ogy_config=load_config(), auth_method="oauth-personal")

    prop = orch.check_model_compatibility("claude-opus-5.5")
    assert prop is not None
    assert prop.kind == "switch_to_v1_run"
    assert "ogy run" in prop.title

    assert orch.check_model_compatibility("gemini-4-neon") is None


def test_switch_tax_and_60k_delegation_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    orch = SessionOrchestrator(ogy_config=load_config())

    # <= 60K tokens with 2 consecutive failures -> propose upgrade_config [U]
    low_ctx = SessionTelemetryState(
        session_id="s1",
        model_id="gemini-4-hydrogen",
        effort="low",
        context_tokens=30_000,
        turn_count=5,
        consecutive_failures=2,
    )
    props_low = orch.evaluate_proposals(low_ctx)
    kinds_low = [p.kind for p in props_low]
    assert "upgrade_config" in kinds_low
    up = next(p for p in props_low if p.kind == "upgrade_config")
    assert up.switch_tax_usd == switch_tax_usd(30_000, "gemini-4-neon")

    # > 60K tokens with 2 consecutive failures -> propose delegate_subagent [D] to preserve parent cache
    high_ctx = SessionTelemetryState(
        session_id="s2",
        model_id="gemini-4-hydrogen",
        effort="medium",
        context_tokens=85_000,
        turn_count=12,
        consecutive_failures=2,
    )
    props_high = orch.evaluate_proposals(high_ctx)
    kinds_high = [p.kind for p in props_high]
    assert "delegate_subagent" in kinds_high
    assert "upgrade_config" not in kinds_high


def test_red_zone_rotation_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    orch = SessionOrchestrator(ogy_config=load_config())

    state = SessionTelemetryState(
        session_id="old-sess",
        context_tokens=125_000,
        turn_count=20,
        modified_files=["ogy_core/router.py"],
        active_goal="Implement V2 TUI",
    )
    props = orch.evaluate_proposals(state)
    assert any(p.kind == "rotate_session" for p in props)

    class _FakeClient:
        def __init__(self) -> None:
            self.closed: list[str] = []

        async def close_session(self, sid: str) -> None:
            self.closed.append(sid)

        async def new_session(self, cwd: str = ".") -> str:
            return "new-sess-2"

    fake = _FakeClient()
    new_sid, handoff_path, content = asyncio.run(orch.execute_rotation(fake, state, cwd=str(tmp_path)))
    assert fake.closed == ["old-sess"]
    assert new_sid == "new-sess-2"
    assert Path(handoff_path).is_file()
    assert "Implement V2 TUI" in content


def test_evaluate_bandit_offline(tmp_path: Path) -> None:
    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="s1", workspace_path=str(tmp_path))
        for i in range(6):
            dec = RouteDecision(
                stage="stage1_heuristic",
                category="bugfix_local",
                model_id="gemini-4-neon" if i % 2 == 0 else "gemini-4-hydrogen",
                effort="medium",
                mode="auto",
                features={},
            )
            record_routing_decision(conn, session_id="s1", decision=dec)
        with write_tx(conn):
            conn.execute(
                "UPDATE routing_decisions SET actual_reward = CASE WHEN chosen_model = 'gemini-4-neon' THEN 0.9 ELSE 0.5 END"
            )
        report = evaluate_bandit_offline(conn, min_samples=5)
        assert report["total_decisions"] == 6
        assert report["categories"]["bugfix_local"]["best_arm"] == "gemini-4-neon:medium"
    finally:
        conn.close()
