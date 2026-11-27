"""Comprehensive V3.7 UX test suite: agy feature parity + ogy superpowers (Tier 1 & Tier 2)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Button, Label, OptionList, Static

from ogy_cli.config_cmd import set_config_value
from ogy_cli.main import build_parser
from ogy_core.config import load_config
from ogy_frontends.acp_tui.client import (
    AcpClient,
    AcpClientConfig,
    AgentMessageEvent,
    AgentThoughtEvent,
    AvailableCommandsEvent,
    CurrentModeUpdateEvent,
    PlanUpdateEvent,
    UsageEvent,
)
from ogy_frontends.acp_tui.orchestrator import SessionTelemetryState
from ogy_frontends.acp_tui.screens.modals import (
    ContextInspectorModal,
    DiffPreviewModal,
    HandoffReviewModal,
    QuickSelectModal,
    SessionPickerModal,
)
from ogy_frontends.acp_tui.widgets.composer import PromptTextArea
from ogy_frontends.acp_tui.widgets.telemetry import (
    MetricRow,
    TelemetryPanel,
    estimate_cache_savings_usd,
)
from ogy_frontends.acp_tui.widgets.transcript import (
    AssistantMessage,
    PlanCard,
    ShellCard,
    ThoughtBlock,
)
from tests.test_acp_client import FakeAgentConn


# ---------------------------------------------------------------------------
# Tier 1: CLI flags, ACP event parsing & cache savings math
# ---------------------------------------------------------------------------


def test_cli_tui_resume_continue_and_lang_default() -> None:
    parser = build_parser()
    args_default = parser.parse_args(["tui"])
    assert args_default.resume is None
    assert args_default.continue_last is False
    assert args_default.lang is None

    args_resume_id = parser.parse_args(["tui", "--resume", "sess-42"])
    assert args_resume_id.resume == "sess-42"

    args_resume_latest = parser.parse_args(["tui", "-r"])
    assert args_resume_latest.resume == "__LATEST__"

    args_continue = parser.parse_args(["chat", "-c"])
    assert args_continue.continue_last is True


def test_estimate_cache_savings_usd() -> None:
    state = SessionTelemetryState(
        session_id="s1",
        model_id="gemini-4-hydrogen",
        cached_read_tokens=1_000_000,
    )
    saved = estimate_cache_savings_usd(state)
    assert saved > 0.0

    empty = SessionTelemetryState(session_id="s2", cached_read_tokens=0)
    assert estimate_cache_savings_usd(empty) == 0.0


@pytest.mark.asyncio
async def test_acp_client_handles_plan_available_commands_and_mode_updates(tmp_path: Path) -> None:
    events: list[Any] = []
    client = AcpClient(AcpClientConfig(cwd=str(tmp_path)), on_event=events.append)

    await client._handle_session_update(
        "s1",
        SimpleNamespace(
            session_update="plan",
            entries=[
                {"content": "Audit codebase", "status": "completed", "priority": "high"},
                {"content": "Write tests", "status": "in_progress", "priority": "medium"},
            ],
        ),
    )
    await client._handle_session_update(
        "s1",
        SimpleNamespace(
            session_update="available_commands_update",
            available_commands=[
                {"name": "review", "description": "Run code review", "input": {"hint": "path"}},
            ],
        ),
    )
    await client._handle_session_update(
        "s1",
        SimpleNamespace(session_update="current_mode_update", current_mode_id="plan"),
    )
    # Banned mode must be ignored
    await client._handle_session_update(
        "s1",
        SimpleNamespace(session_update="current_mode_update", current_mode_id="yolo"),
    )

    assert any(isinstance(e, PlanUpdateEvent) and len(e.entries) == 2 for e in events)
    assert any(isinstance(e, AvailableCommandsEvent) and e.commands[0]["name"] == "review" for e in events)
    assert client.get_available_commands("s1")[0]["name"] == "review"
    mode_events = [e for e in events if isinstance(e, CurrentModeUpdateEvent)]
    assert len(mode_events) == 1
    assert mode_events[0].mode_id == "plan"


# ---------------------------------------------------------------------------
# Tier 2: Headless TUI Pilot Tests (V3.7 agy parity + ogy superpowers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_plan_card_and_dynamic_agy_commands(make_tui_app: Any) -> None:
    fake = FakeAgentConn()
    app = make_tui_app(injected_agent_conn=fake, lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # 1. PlanCard mounts and updates in place (never duplicates)
        await app._on_acp_event(
            PlanUpdateEvent(
                session_id="acp-sess-1",
                entries=[
                    {"content": "Inspect API", "status": "in_progress", "priority": "high"},
                    {"content": "Patch handler", "status": "pending", "priority": "medium"},
                ],
            )
        )
        await pilot.pause()
        tr = app.get_active_transcript()
        assert tr is not None
        cards = list(tr.query(PlanCard))
        assert len(cards) == 1
        assert "0/2 (0%)" in cards[0].title

        await app._on_acp_event(
            PlanUpdateEvent(
                session_id="acp-sess-1",
                entries=[
                    {"content": "Inspect API", "status": "completed", "priority": "high"},
                    {"content": "Patch handler", "status": "completed", "priority": "medium"},
                ],
            )
        )
        await pilot.pause()
        assert len(list(tr.query(PlanCard))) == 1
        assert "2/2 (100%)" in cards[0].title

        # 2. Dynamic ACP commands appear in `/` completions and forward to agy
        app.client.session_commands["acp-sess-1"] = [
            {"name": "review", "description": "Deep code review", "input_hint": "file"},
        ]
        completions = app.get_slash_completions("/rev")
        assert any(ins == "/review " and "[agy]" in lbl for ins, lbl in completions)

        await app._handle_slash_command("/review app.py")
        await pilot.pause(0.15)
        assert any(name == "prompt" for name, _ in fake.calls)


@pytest.mark.asyncio
async def test_tui_inline_shell_and_attach_to_prompt(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        ta = app.query_one("#prompt-input", PromptTextArea)
        ta.value = "!echo hello_v37"
        await ta.action_submit()

        # Wait briefly for async subprocess to finish
        for _ in range(30):
            await pilot.pause(0.05)
            tr = app.get_active_transcript()
            cards = list(tr.query(ShellCard)) if tr else []
            if cards and cards[0].exit_code is not None:
                break

        tr = app.get_active_transcript()
        assert tr is not None
        cards = list(tr.query(ShellCard))
        assert len(cards) == 1
        assert cards[0].exit_code == 0
        assert "hello_v37" in cards[0].output_text

        # Trigger Attach to prompt button -> output injected into composer
        cards[0].post_message(ShellCard.AttachRequested(cards[0].command, cards[0].output_text, 0))
        await pilot.pause()
        assert "hello_v37" in ta.text
        assert "[Shell output:" in ta.text


@pytest.mark.asyncio
async def test_tui_git_diff_modal(tmp_path: Path, make_tui_app: Any) -> None:
    # Create a tiny git repo with a modified file
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@ogy.dev"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Ogy"], cwd=tmp_path, check=True, capture_output=True)
    f = tmp_path / "hello.py"
    f.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    f.write_text("x = 42\n", encoding="utf-8")

    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    app.client_config.cwd = str(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await app._handle_slash_command("/diff")
        await pilot.pause(0.15)
        assert isinstance(app.screen, DiffPreviewModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DiffPreviewModal)


@pytest.mark.asyncio
async def test_tui_attach_image_and_compact_alias(tmp_path: Path, make_tui_app: Any) -> None:
    img = tmp_path / "screen.png"
    # Minimal valid PNG header bytes
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    app.client_config.cwd = str(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await app._handle_slash_command("/attach screen.png")
        await pilot.pause()
        ta = app.query_one("#prompt-input", PromptTextArea)
        assert "[Image #1]" in ta.text
        _, extra = ta.expand_payload(ta.text)
        assert len(extra) == 1
        assert extra[0].mime_type == "image/png"

        # /compact opens HandoffReviewModal (agy alias for /rotate)
        await app._handle_slash_command("/compact")
        await pilot.pause()
        assert isinstance(app.screen, HandoffReviewModal)
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_fork_session_and_picker_close(make_tui_app: Any) -> None:
    fake = FakeAgentConn()
    app = make_tui_app(injected_agent_conn=fake, lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        orig_sid = app.registry.active_session_id
        assert orig_sid == "acp-sess-1"

        await app._handle_slash_command("/fork")
        await pilot.pause()
        assert app.registry.active_session_id == "acp-sess-2"
        assert any(name == "fork_session" for name, _ in fake.calls)
        assert orig_sid in app.registry.sessions

        # Close inactive parent session via helper (wired from SessionPickerModal Ctrl+X)
        await app._close_session_by_id(orig_sid)
        await pilot.pause()
        assert orig_sid not in app.registry.sessions
        assert any(name == "close_session" for name, _ in fake.calls)


@pytest.mark.asyncio
async def test_tui_shift_tab_mode_cycle_quick_select_and_ctrl_t(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.current_state is not None
        assert app.current_state.mode == "auto"

        # Shift+Tab cycles auto -> plan -> default -> accept-edits (never yolo)
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.current_state.mode == "plan"

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.current_state.mode == "default"
        assert app.current_state.mode != "yolo"

        # Alt+M opens QuickSelectModal with Switch Tax when context > 0
        app.current_state.context_tokens = 50_000
        await pilot.press("alt+m")
        await pilot.pause()
        assert isinstance(app.screen, QuickSelectModal)
        opt_list = app.screen.query_one("#quick-select-list", OptionList)
        assert opt_list.option_count >= 3
        await pilot.press("escape")
        await pilot.pause()

        # Ctrl+T toggles ThoughtBlock expansion
        await app._on_acp_event(AgentThoughtEvent("acp-sess-1", "Deep reasoning step..."))
        tr = app.get_active_transcript()
        assert tr is not None
        tr.close_active_thought()
        tb = tr.query_one(ThoughtBlock)
        assert tb.collapsed is True

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert tb.collapsed is False

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert tb.collapsed is True


@pytest.mark.asyncio
async def test_tui_context_inspector_mcp_config_and_live_savings_budget_cap(
    tmp_path: Path, make_tui_app: Any
) -> None:
    (tmp_path / "GEMINI.md").write_text("# Project rules\n", encoding="utf-8")
    mcp_dir = tmp_path / ".agents"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"ogy-mcp": {"command": "ogy-mcp", "eager": False}}}),
        encoding="utf-8",
    )

    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    app.client_config.cwd = str(tmp_path)
    app.ogy_config.budget_hard_cap_usd = 0.05
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Simulate usage with large cache read & cost exceeding hard cap
        await app._on_acp_event(
            UsageEvent(
                session_id="acp-sess-1",
                input_tokens=1_000,
                cached_read_tokens=500_000,
                output_tokens=200,
                context_used=501_000,
                cost_usd=0.08,
                is_turn_final=True,
            )
        )
        await pilot.pause()

        tel = app.query_one("#telemetry-panel", TelemetryPanel)
        saved_lbl = str(tel.query_one("#tel-cache-saved", Label).render())
        assert saved_lbl.startswith("$") and saved_lbl != "$0.0000"
        assert tel.query_one("#row-cost", MetricRow).has_class("val-danger")

        # /context opens ContextInspectorModal
        await app._handle_slash_command("/context")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContextInspectorModal)
        await pilot.press("escape")
        await pilot.pause()

        # /mcp lists configured servers
        await app._handle_slash_command("/mcp")
        await pilot.pause()
        tr = app.get_active_transcript()
        assert tr is not None
        assert any("ogy-mcp" in str(w.render()) for w in tr.query(".notice-line"))

        # /config updates config.toml & applies theme live
        await app._handle_slash_command("/config tui.theme light")
        await pilot.pause(0.1)
        assert app.tui_theme == "light"
        assert load_config(app.ogy_config.config_path).tui_theme == "light"
