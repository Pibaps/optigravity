"""Headless Textual TUI tests for Optigravity V2 (`ogy tui`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from ogy_core.config import load_config
from ogy_frontends.acp_tui.app import OptigravityApp, TelemetryPanel
from ogy_frontends.acp_tui.client import (
    AcpClientConfig,
    AgentMessageEvent,
    AgentThoughtEvent,
    ToolCallEvent,
    UsageEvent,
)
from tests.test_acp_client import FakeAgentConn


def test_tui_app_streams_and_handles_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    fake = FakeAgentConn()
    app = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake,
    )

    async def _run() -> None:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert app.client.active_session_id == "acp-sess-1"

            # Simulate streaming thought, message, tool call, and final usage
            await app._on_acp_event(AgentThoughtEvent("acp-sess-1", "Analyzing codebase..."))
            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Here is the fix."))
            await app._on_acp_event(
                ToolCallEvent("acp-sess-1", "tc1", "run_command", "pytest -q", "completed")
            )
            await app._on_acp_event(
                UsageEvent(
                    session_id="acp-sess-1",
                    input_tokens=500,
                    cached_read_tokens=4500,
                    output_tokens=120,
                    thought_tokens=310,
                    context_used=5000,
                    cost_usd=0.0042,
                    is_turn_final=True,
                )
            )
            await pilot.pause(0.05)

            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            thought_lbl = str(tel.query_one("#tel-thought").render())
            assert "310" in thought_lbl

            # Slash command /mode yolo must be rejected, /mode plan accepted
            inp = app.query_one("#prompt-input", Input)
            inp.value = "/mode yolo"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state is not None
            assert app.current_state.mode != "yolo"

            inp.value = "/mode plan"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.mode == "plan"

            # Ctrl+C sends session/cancel without killing app
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            assert any(name == "cancel" for name, _ in fake.calls)

    asyncio.run(_run())
