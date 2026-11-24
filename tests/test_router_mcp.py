"""Tests for 2-stage router, MCP server tools, and MCP audit."""

from __future__ import annotations

import json
from pathlib import Path

from ogy_cli.init_audit import audit_mcp_config
from ogy_cli.wrapper import build_agy_args
from ogy_core.router import record_routing_decision, route_stage1, route_stage2_bandit
from ogy_core.storage import connect, ensure_session, write_tx
from ogy_mcp.server import ogy_session_budget, ogy_write_handoff


def test_router_stage1_and_quota_downgrade() -> None:
    d1 = route_stage1("Fix typo in README comment")
    assert d1.category == "trivial_edit"
    assert d1.model_id == "gemini-4-hydrogen"
    assert d1.effort == "low"

    d2 = route_stage1("Design OAuth RBAC security architecture", remaining_quota_fraction=0.05)
    assert d2.category == "architecture_security"
    assert d2.effort == "high"
    assert d2.quota_downgraded is False

    d3 = route_stage1("Implement new CSV export endpoint", remaining_quota_fraction=0.05)
    assert d3.category == "routine_code"
    assert d3.effort == "low"
    assert d3.quota_downgraded is True


def test_router_stage2_bandit_and_quality_guardrail(tmp_path: Path) -> None:
    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="s-bandit", workspace_path=str(tmp_path))
        # Seed 16 qualified tasks for routine_code:
        # 8 with hydrogen/medium (100% success, high reward 0.9)
        # 8 with neon/low (50% success -> excluded by <80% guardrail)
        for i in range(16):
            tid = f"t-{i}"
            is_h = i < 8
            outcome = "success" if (is_h or i % 2 == 0) else "failed"
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO tasks (task_id, repo_root, category, outcome) VALUES (?, ?, 'routine_code', ?)",
                    (tid, str(tmp_path), outcome),
                )
            dec = route_stage1(
                "Add helper function",
                override_model="gemini-4-hydrogen" if is_h else "gemini-4-neon",
                override_effort="medium" if is_h else "low",
                override_mode="auto",
            )
            record_routing_decision(conn, session_id="s-bandit", task_id=tid, decision=dec)
            with write_tx(conn):
                conn.execute(
                    "UPDATE routing_decisions SET actual_reward = ? WHERE task_id = ?",
                    (0.9 if is_h else 0.95, tid),
                )

        chosen = route_stage2_bandit(conn, "Add another helper function", min_qualified_tasks=15)
        assert chosen.stage == "stage2_bandit"
        assert chosen.model_id == "gemini-4-hydrogen"
        assert chosen.effort == "medium"
    finally:
        conn.close()


def test_mcp_tools_and_audit(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))
    conn = connect(db_path, init_schema=True)
    conn.close()

    budget_json = json.loads(ogy_session_budget())
    assert budget_json["status"] == "no_sessions"

    handoff_res = json.loads(
        ogy_write_handoff(
            goal="Finish V1 tests",
            modified_files=["ogy_core/storage.py"],
            checks="pytest passed",
            negative_constraints=["Do not compact after 2 errors"],
            next_step="Run final verification",
            workspace_path=str(tmp_path),
        )
    )
    assert Path(handoff_res["path"]).is_file()
    assert handoff_res["lines"] <= 30

    audit = audit_mcp_config(
        {
            "mcpServers": {
                "bloated": {
                    "eager": True,
                    "tools": [
                        {"name": "ui_widget", "_meta": {"ui": {"visibility": ["app"]}}},
                        {"name": "cli_tool", "_meta": {"ui": {"visibility": ["cli"]}}},
                    ],
                }
            }
        }
    )
    assert len(audit["findings"]) == 1
    assert audit["recommended_profile"]["bloated"]["enabledTools"] == ["cli_tool"]

    dec = route_stage1("Fix typo in README")
    assert build_agy_args(dec, prompt="Fix typo")[-2:] == ["-i", "Fix typo"]
    assert build_agy_args(dec, prompt="Fix typo", headless=True)[-2:] == ["-p", "Fix typo"]
