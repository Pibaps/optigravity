"""Headless Textual TUI tests for Optigravity V2 (`ogy tui`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Markdown, OptionList, Static

from ogy_core.config import load_config
from ogy_core.storage import connect
from ogy_frontends.acp_tui.app import (
    GlossaryModal,
    MetricGauge,
    OptigravityApp,
    OptigravityCommandProvider,
    TelemetryPanel,
    _clean_file_links,
    _human_quota_label,
)
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

            # Session bar must have >=1 visible content line (>=2 outer height with border-bottom)
            session_bar = app.query_one("#session-bar", Static)
            assert session_bar.size.height >= 1
            assert session_bar.outer_size.height >= 2
            bar_text = str(session_bar.render())
            assert "gemini-4-hydrogen" in bar_text
            assert "Effort" in bar_text

            # Chronological stream order: msg1 -> tool -> msg2 must mount msg2 AFTER tool
            await app._on_acp_event(AgentThoughtEvent("acp-sess-1", "Analyzing codebase..."))
            activity = app.query_one("#activity-bar", Static)
            assert activity.has_class("visible")

            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "First message before tool."))
            await app._on_acp_event(
                ToolCallEvent("acp-sess-1", "tc1", "run_command", "pytest -q", "running")
            )
            await app._on_acp_event(
                ToolCallEvent("acp-sess-1", "tc1", "run_command", "pytest -q", "completed")
            )
            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Final answer after tool."))

            stream = app.query_one("#stream-scroll", VerticalScroll)
            children = list(stream.children)
            md_indices = [i for i, w in enumerate(children) if isinstance(w, Markdown)]
            tool_indices = [i for i, w in enumerate(children) if isinstance(w, Static) and w.has_class("tool-line")]
            assert len(md_indices) == 2
            assert len(tool_indices) == 1
            assert md_indices[0] < tool_indices[0] < md_indices[1]

            assert app.current_state is not None
            app.current_state.last_turn_monotonic -= 120.0  # simulate old timestamp before final turn
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

            # Spinner hides when turn completes, and idle_gap_s resets
            assert not activity.has_class("visible")
            assert app.current_state.idle_gap_s < 5.0

            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            thought_lbl = str(tel.query_one("#tel-thought").render())
            assert "310" in thought_lbl

            # Slash command /mode yolo must be rejected, /mode plan accepted
            inp = app.query_one("#prompt-input", Input)
            inp.value = "/mode yolo"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.mode != "yolo"

            inp.value = "/mode plan"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.mode == "plan"

            # /model without arg should NOT error; it lists current & available models
            inp.value = "/model"
            await inp.action_submit()
            await pilot.pause(0.05)

            # /btw command sends a concise side question prompt
            inp.value = "/btw what is 2+2?"
            await inp.action_submit()
            await pilot.pause(0.1)
            assert any(name == "prompt" for name, _ in fake.calls)

            # /lang en switches UI labels to English and /lang fr switches back
            inp.value = "/lang en"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.lang == "en"
            assert "Model:" in str(app.query_one("#session-bar", Static).render())

            inp.value = "/lang fr"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.lang == "fr"
            assert "Modèle :" in str(app.query_one("#session-bar", Static).render())

            # Ctrl+C sends session/cancel without killing app
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            assert any(name == "cancel" for name, _ in fake.calls)

    asyncio.run(_run())


def test_tui_slash_autocomplete_palette_glossary_and_quota_gauges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

    # Seed quota_snapshots in SQLite so TelemetryPanel renders per-bucket MetricGauge widgets
    conn = connect(db_path, init_schema=True)
    try:
        conn.execute(
            "INSERT INTO quota_snapshots (bucket_id, remaining_fraction, plan_tier) VALUES (?, ?, ?)",
            ("gemini-5h", 0.87, "pro"),
        )
        conn.execute(
            "INSERT INTO quota_snapshots (bucket_id, remaining_fraction, plan_tier) VALUES (?, ?, ?)",
            ("3p-weekly", 0.36, "pro"),
        )
        conn.commit()
    finally:
        conn.close()

    fake = FakeAgentConn()
    app = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake,
    )

    async def _run() -> None:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            # Quota gauges rendered with human labels (FR & EN)
            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            assert tel.query_one("#quota-gemini-5h", MetricGauge) is not None
            assert tel.query_one("#quota-3p-weekly", MetricGauge) is not None
            assert _human_quota_label("3p-5h", "fr") == "Tiers / Claude (5h)"
            assert _human_quota_label("3p-weekly", "en") == "3rd-Party / Claude (7d)"

            # Slash autocomplete popup includes /btw and /lang, and Tab completes `/mod` -> `/model `
            assert any(ins == "/btw " for ins, _ in app.get_slash_completions("/b"))
            assert any(ins == "/lang en" for ins, _ in app.get_slash_completions("/lang "))

            inp = app.query_one("#prompt-input", Input)
            inp.focus()
            inp.value = "/mod"
            await pilot.pause(0.05)
            popup = app.query_one("#slash-popup", OptionList)
            assert popup.has_class("visible")
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert inp.value == "/model "
            arg_completions = app.get_slash_completions("/model ")
            assert any("gemini-4-hydrogen" in insert for insert, _ in arg_completions)

            # Command palette provider exposes Optigravity commands via discover()
            provider = OptigravityCommandProvider(app.screen, match_style=None)
            discovered = [hit async for hit in provider.discover()]
            titles = [str(h.display) for h in discovered]
            assert any("Glossaire" in t for t in titles)
            assert any("Rotation" in t for t in titles)
            assert any("Modèle →" in t for t in titles)
            assert any("Langue / Language" in t for t in titles)

            # F1 opens GlossaryModal, Escape closes it
            await pilot.press("f1")
            await pilot.pause(0.05)
            assert isinstance(app.screen, GlossaryModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, GlossaryModal)

            # Proposal upgrade action applies config change after >=2 failures
            await app._on_acp_event(ToolCallEvent("acp-sess-1", "e1", "run_command", "fail 1", "failed", is_error=True))
            await app._on_acp_event(ToolCallEvent("acp-sess-1", "e2", "run_command", "fail 2", "failed", is_error=True))
            await pilot.pause(0.05)
            assert app.active_proposals
            await app.action_accept_proposal()
            await pilot.pause(0.05)

    asyncio.run(_run())


def test_clean_file_links_shortens_markdown_file_urls_and_keeps_code_blocks() -> None:
    sample = (
        "Voir [install.ps1](file:///C:/Users/pbapi/Projets/install.ps1) et "
        "[README.md](file:///C:/Users/pbapi/README.md#L97-L109).\n"
        "```md\n[keep.py](file:///C:/Users/pbapi/keep.py)\n```\n"
    )
    cleaned = _clean_file_links(sample)
    assert "`install.ps1`" in cleaned
    assert "`README.md:97-109`" in cleaned
    assert "file:///C:/Users/pbapi/Projets/install.ps1" not in cleaned
    assert "[keep.py](file:///C:/Users/pbapi/keep.py)" in cleaned


def test_tui_frugal_startup_and_first_prompt_stage1_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            assert app.current_state is not None
            # Starts on frugal default (gemini-4-hydrogen / medium)
            assert app.current_state.model_id == "gemini-4-hydrogen"
            assert app.current_state.effort == "medium"

            # 1st prompt classified as bugfix_local -> auto-routes to gemini-4-neon / low (0 Switch Tax)
            inp = app.query_one("#prompt-input", Input)
            inp.value = "Fix bug in parser.py"
            await inp.action_submit()
            await pilot.pause(0.1)
            assert app.current_state.model_id == "gemini-4-neon"
            assert app.current_state.effort == "low"

            # Stickiness: 2nd prompt must NOT re-route away from active session profile
            inp.value = "Fix typo in README"
            await inp.action_submit()
            await pilot.pause(0.1)
            assert app.current_state.model_id == "gemini-4-neon"

            # Manual override via /model on a fresh session skips 1st-prompt auto-routing
            await app.action_new_session()
            await pilot.pause(0.1)
            assert app.current_state.model_id == "gemini-4-hydrogen"
            inp.value = "/model gemini-4-carbon"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.model_id == "gemini-4-carbon"

            inp.value = "Fix bug in parser.py"
            await inp.action_submit()
            await pilot.pause(0.1)
            assert app.current_state.model_id == "gemini-4-carbon"

    asyncio.run(_run())
