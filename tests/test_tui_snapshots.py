"""Visual SVG snapshot tests for `ogy tui` via `pytest-textual-snapshot` (Lot 3.5.2).

Authoritative baseline runs on `ubuntu-latest` in CI; locally update with:
    uv run -m pytest -m snapshot --snapshot-update
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
import time_machine

from ogy_frontends.acp_tui.client import (
    AgentMessageEvent,
    AgentThoughtEvent,
    ElicitationRequestEvent,
    PermissionRequestEvent,
    ToolCallEvent,
    UsageEvent,
)
from ogy_frontends.acp_tui.screens.modals import (
    ElicitationModal,
    HandoffReviewModal,
    PermissionModal,
    SessionPickerModal,
)
from tests.conftest import disable_blink_for_active_cursors
from tests.test_acp_client import FakeAgentConn

FROZEN_TIME = "2026-11-15T10:05:00Z"


async def _settle(pilot: Any) -> None:
    """Deterministically wait for workers, scheduled animations, and neutralize cursor blink."""
    await pilot.app.workers.wait_for_complete()
    await pilot.wait_for_scheduled_animations()
    disable_blink_for_active_cursors(pilot)
    await pilot.pause()


@pytest.mark.snapshot
@pytest.mark.parametrize("terminal_size", [(80, 24), (140, 40)])
def test_snapshot_home_screen(snap_compare: Any, make_seeded_tui_app: Any, terminal_size: tuple[int, int]) -> None:
    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=terminal_size, run_before=_settle)


@pytest.mark.snapshot
def test_snapshot_streaming_and_telemetry_sidebar(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        app = pilot.app
        await _settle(pilot)
        await app._on_acp_event(AgentThoughtEvent("acp-sess-1", "Checking token budget and cache ratio..."))
        await app._on_acp_event(ToolCallEvent("acp-sess-1", "r1", "read_file", "ogy_core/config.py", "completed", kind="read"))
        await app._on_acp_event(ToolCallEvent("acp-sess-1", "s1", "grep_search", "profile_mode", "completed", kind="search"))
        await app._on_acp_event(
            AgentMessageEvent(
                "acp-sess-1",
                "### Audit Summary\n\n- Profile mode: **isolated**\n- Warm cache hit: **92%**\n",
            )
        )
        await app._on_acp_event(
            UsageEvent(
                session_id="acp-sess-1",
                input_tokens=1200,
                cached_read_tokens=8800,
                output_tokens=350,
                thought_tokens=420,
                context_used=10000,
                cost_usd=0.0042,
                is_turn_final=True,
            )
        )
        tr = app.get_active_transcript()
        if tr is not None:
            await tr.close_active_message()
            tr.close_active_thought()
        if app.current_state is not None:
            app.current_state.last_turn_monotonic = time.monotonic()
        await pilot.pause(0.1)
        await _settle(pilot)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)


@pytest.mark.snapshot
def test_snapshot_permission_modal_with_diff(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        app = pilot.app
        await _settle(pilot)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        await app._on_acp_event(
            PermissionRequestEvent(
                session_id="acp-sess-1",
                tool_call_id="perm-snap-1",
                tool_name="edit_file",
                title="Edit app.py",
                options=[
                    ("allow_once", "Allow once", "allow_once"),
                    ("allow_always", "Allow always", "allow_always"),
                    ("reject_once", "Reject once", "reject_once"),
                    ("reject_always", "Never", "reject_always"),
                ],
                future=fut,
                kind="edit",
                raw_input={"path": "app.py"},
                content=[
                    {
                        "type": "diff",
                        "path": "app.py",
                        "oldText": "def answer():\n    return 41\n",
                        "newText": "def answer():\n    return 42\n",
                    }
                ],
            )
        )
        await _settle(pilot)
        assert isinstance(app.screen, PermissionModal)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)


@pytest.mark.snapshot
def test_snapshot_elicitation_modal(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        app = pilot.app
        await _settle(pilot)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        await app._on_acp_event(
            ElicitationRequestEvent(
                session_id="acp-sess-1",
                elicitation_id="el-snap-1",
                message="Configure deployment target",
                future=fut,
                mode="form",
                requested_schema={
                    "type": "object",
                    "properties": {
                        "env": {"type": "string", "title": "Environment", "default": "staging"},
                        "dry_run": {"type": "boolean", "title": "Dry run", "default": True},
                    },
                    "required": ["env"],
                },
            )
        )
        await _settle(pilot)
        assert isinstance(app.screen, ElicitationModal)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)


@pytest.mark.snapshot
def test_snapshot_session_picker_modal(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        app = pilot.app
        await _settle(pilot)
        await pilot.press("ctrl+s")
        await _settle(pilot)
        assert isinstance(app.screen, SessionPickerModal)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)


@pytest.mark.snapshot
def test_snapshot_handoff_review_modal(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        app = pilot.app
        await _settle(pilot)
        await app.action_open_rotation_review()
        await _settle(pilot)
        assert isinstance(app.screen, HandoffReviewModal)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)


@pytest.mark.snapshot
def test_snapshot_light_theme_welcome(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en", tui_theme="light")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_settle)


@pytest.mark.snapshot
def test_snapshot_search_and_queue_bar(snap_compare: Any, make_seeded_tui_app: Any) -> None:
    async def _run_before(pilot: Any) -> None:
        from textual.widgets import Input

        app = pilot.app
        await _settle(pilot)
        await app._on_acp_event(
            AgentMessageEvent(
                "acp-sess-1",
                "Here is the fix:\n```python\ndef optimize(x):\n    return x * 10\n```\n",
            )
        )
        tr = app.get_active_transcript()
        if tr is not None:
            await tr.close_active_message()
        app._set_working(False)
        # Show queued prompt bar and active Ctrl+F transcript search bar
        app.registry.enqueue_prompt("acp-sess-1", "Now run the full test suite")
        app._refresh_queue_bar()
        app.action_open_transcript_search()
        app.query_one("#transcript-search-input", Input).value = "optimize"
        await pilot.pause(0.1)
        await _settle(pilot)

    with time_machine.travel(FROZEN_TIME, tick=False):
        app = make_seeded_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
        assert snap_compare(app, terminal_size=(120, 36), run_before=_run_before)
