"""Global pytest guardrails & Textual Pilot factories for Optigravity V3.5.

Invariants enforced here:
1. Host ~/.gemini/ and ~/.local/share/optigravity/ are NEVER touched by any test
   (including pytest-xdist workers and mutmut runs): HOME, USERPROFILE, OGY_DB_PATH,
   OGY_CONFIG_PATH, and OGY_GEMINI_HOME are systematically isolated in tmp_path.
2. `make_tui_app` / `make_seeded_tui_app` are factories returning an unstarted
   `OptigravityApp` instance — `App.run_test()` is NEVER opened inside a pytest-asyncio
   fixture (avoids Textual #4998 ContextVar / NoActiveAppError issues).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ogy_core.config import OgyConfig, load_config
from ogy_core.storage import connect, ensure_session, upsert_turn
from ogy_frontends.acp_tui.app import OptigravityApp
from ogy_frontends.acp_tui.client import AcpClientConfig


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Global V3.1/V3.5 guardrail: isolate HOME, USERPROFILE, OGY_DB_PATH, OGY_GEMINI_HOME."""
    fake_home = tmp_path / "_ogy_test_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    db_path = fake_home / ".local" / "share" / "optigravity" / "ogy.db"

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))
    monkeypatch.delenv("OGY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OGY_REAL_HOME", raising=False)
    monkeypatch.delenv("OGY_GEMINI_HOME", raising=False)
    monkeypatch.delenv("OGY_ACTIVE", raising=False)
    monkeypatch.delenv("OGY_TASK_ID", raising=False)
    monkeypatch.delenv("OGY_SESSION_META", raising=False)
    monkeypatch.delenv("OGY_ACP_COMMAND_OVERRIDE", raising=False)
    return fake_home


def fake_acp_command(scenario: str) -> list[str]:
    """Return command_override list launching the deterministic Fake ACP Agent over stdio."""
    return [sys.executable, "-m", "tests.fixtures.fake_acp_agent", "--scenario", scenario]


def disable_blink_for_active_cursors(pilot: Any) -> None:
    """Neutralize cursor blinking on TextArea/Input widgets for deterministic SVG snapshots."""
    for widget in pilot.app.screen.query("*"):
        if hasattr(widget, "cursor_blink"):
            try:
                widget.cursor_blink = False
            except Exception:
                pass


@pytest.fixture
def make_tui_app(tmp_path: Path) -> Callable[..., OptigravityApp]:
    """Factory returning an unstarted OptigravityApp isolated in tmp_path (Textual #4998 safe)."""

    def _factory(
        *,
        scenario: str | None = "nominal_stream",
        injected_agent_conn: Any = None,
        profile_mode: str = "shared",
        ogy_config: OgyConfig | None = None,
        cwd: Path | None = None,
        lang: str = "fr",
        auto_connect: bool = True,
        initial_model: str | None = None,
        initial_effort: str | None = None,
        initial_prompt: str | None = None,
        subagents_service: Any = None,
    ) -> OptigravityApp:
        work_dir = cwd or tmp_path
        cfg = ogy_config or load_config()
        if profile_mode == "isolated":
            cfg.profile_mode = "isolated"
            iso_root = cfg.isolated_profile_root or (tmp_path / "_ogy_test_home" / ".local" / "share" / "optigravity" / "gemini-profile")
            cfg.isolated_profile_root = iso_root
            cfg.gemini_home = iso_root / ".gemini"
            cfg.agy_cli_dir = cfg.gemini_home / "antigravity-cli"
            cfg.agy_config_dir = cfg.gemini_home / "config"

        cmd_override = None if injected_agent_conn is not None else (fake_acp_command(scenario) if scenario else None)
        client_cfg = AcpClientConfig(
            cwd=str(work_dir),
            command_override=cmd_override,
        )
        return OptigravityApp(
            client_config=client_cfg,
            ogy_config=cfg,
            auto_connect=auto_connect,
            injected_agent_conn=injected_agent_conn,
            lang=lang,
            initial_model=initial_model,
            initial_effort=initial_effort,
            initial_prompt=initial_prompt,
            subagents_service=subagents_service,
        )

    return _factory


@pytest.fixture
def make_seeded_tui_app(tmp_path: Path, make_tui_app: Callable[..., OptigravityApp]) -> Callable[..., OptigravityApp]:
    """Factory returning an unstarted OptigravityApp backed by a deterministic pre-seeded SQLite DB."""

    def _factory(
        *,
        scenario: str | None = "nominal_stream",
        injected_agent_conn: Any = None,
        lang: str = "en",
        auto_connect: bool = True,
    ) -> OptigravityApp:
        cfg = load_config()
        conn = connect(cfg.db_path, init_schema=True)
        try:
            ensure_session(
                conn,
                session_id="sess-snap-001",
                surface="cli_acp",
                workspace_path="/workspace/optigravity",
                initial_model="gemini-4-hydrogen",
            )
            upsert_turn(
                conn,
                session_id="sess-snap-001",
                step_index=1,
                model_id="gemini-4-hydrogen",
                input_tokens=1200,
                cache_read_tokens=8800,
                output_tokens=350,
                thinking_tokens=420,
                thinking_is_estimated=0,
                context_window_used=10000,
                estimated_cost_usd=0.0042,
                data_source="acp",
            )
            conn.execute(
                """
                INSERT INTO quota_snapshots (captured_at, bucket_id, remaining_fraction, plan_tier)
                VALUES (?, ?, ?, ?)
                """,
                ("2026-11-15T10:04:00Z", "gemini-5h", 0.87, "pro"),
            )
            conn.execute(
                """
                INSERT INTO quota_snapshots (captured_at, bucket_id, remaining_fraction, plan_tier)
                VALUES (?, ?, ?, ?)
                """,
                ("2026-11-15T10:04:00Z", "3p-weekly", 0.42, "pro"),
            )
            conn.commit()
        finally:
            conn.close()

        return make_tui_app(
            scenario=scenario,
            injected_agent_conn=injected_agent_conn,
            ogy_config=cfg,
            cwd=tmp_path,
            lang=lang,
            auto_connect=auto_connect,
        )

    return _factory
