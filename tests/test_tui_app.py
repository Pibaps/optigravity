"""Headless Textual Pilot tests for Optigravity Phase 2 (`ogy tui`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual import events
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Static, TextArea

from ogy_core.config import load_config
from ogy_core.storage import connect
from ogy_frontends.acp_tui.app import (
    ElicitationModal,
    GlossaryModal,
    HandoffReviewModal,
    MetricGauge,
    OptigravityApp,
    OptigravityCommandProvider,
    PermissionModal,
    SessionPickerModal,
    TelemetryPanel,
    _clean_file_links,
    _human_quota_label,
)
from ogy_frontends.acp_tui.client import (
    AcpClientConfig,
    AgentMessageEvent,
    AgentThoughtEvent,
    ElicitationCompleteEvent,
    ElicitationRequestEvent,
    PermissionRequestEvent,
    ToolCallEvent,
    UsageEvent,
)
from ogy_frontends.acp_tui.screens.modals import DiffPreviewModal
from ogy_frontends.acp_tui.widgets.composer import PromptComposer, PromptTextArea
from ogy_frontends.acp_tui.widgets.transcript import (
    ActivityGroup,
    AssistantMessage,
    ThoughtBlock,
    ToolCard,
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
            await pilot.pause(0.15)
            assert app.client.active_session_id == "acp-sess-1"

            session_bar = app.query_one("#session-bar", Static)
            assert session_bar.size.height >= 1
            assert session_bar.outer_size.height >= 2
            bar_text = str(session_bar.render())
            assert "gemini-4-hydrogen" in bar_text
            assert "Effort" in bar_text

            # Chronological stream order: thought -> msg1 -> tool -> msg2
            await app._on_acp_event(AgentThoughtEvent("acp-sess-1", "Analyzing codebase..."))
            activity = app.query_one("#activity-bar", Static)
            assert activity.has_class("visible")

            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "First message before tool."))
            await app._on_acp_event(ToolCallEvent("acp-sess-1", "tc1", "run_command", "pytest -q", "running"))
            await app._on_acp_event(ToolCallEvent("acp-sess-1", "tc1", "run_command", "pytest -q", "completed"))
            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Final answer after tool."))
            await pilot.pause(0.05)

            tr = app.get_active_transcript()
            assert tr is not None
            children = list(tr.children)
            thought_indices = [i for i, w in enumerate(children) if isinstance(w, ThoughtBlock)]
            msg_indices = [i for i, w in enumerate(children) if isinstance(w, AssistantMessage)]
            tool_indices = [i for i, w in enumerate(children) if isinstance(w, ToolCard)]
            assert len(thought_indices) == 1
            assert children[thought_indices[0]].collapsed is True
            assert len(msg_indices) == 2
            assert len(tool_indices) == 1
            assert msg_indices[0] < tool_indices[0] < msg_indices[1]

            assert app.current_state is not None
            app.current_state.last_turn_monotonic -= 120.0
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
            assert not activity.has_class("visible")
            assert app.current_state.idle_gap_s < 5.0

            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            assert "310" in str(tel.query_one("#tel-thought").render())

            inp = app.query_one("#prompt-input", PromptTextArea)
            inp.value = "/mode yolo"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.mode != "yolo"

            inp.value = "/mode plan"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.mode == "plan"

            inp.value = "/model"
            await inp.action_submit()
            await pilot.pause(0.05)

            inp.value = "/btw what is 2+2?"
            await inp.action_submit()
            await pilot.pause(0.1)
            assert any(name == "prompt" for name, _ in fake.calls)

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

            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            assert any(name == "cancel" for name, _ in fake.calls)

    asyncio.run(_run())


def test_tui_slash_autocomplete_palette_glossary_and_quota_gauges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

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
            await pilot.pause(0.25)

            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            assert tel.query_one("#quota-gemini-5h", MetricGauge) is not None
            assert tel.query_one("#quota-3p-weekly", MetricGauge) is not None
            assert _human_quota_label("3p-5h", "fr") == "Tiers / Claude (5h)"
            assert _human_quota_label("3p-weekly", "en") == "3rd-Party / Claude (7d)"

            assert any(ins == "/btw " for ins, _ in app.get_slash_completions("/b"))
            assert any(ins == "/lang en" for ins, _ in app.get_slash_completions("/lang "))

            inp = app.query_one("#prompt-input", PromptTextArea)
            inp.focus()
            inp.value = "/mod"
            await pilot.pause(0.05)
            popup = app.query_one("#composer-popup", OptionList)
            assert popup.has_class("visible")
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert inp.value == "/model "
            arg_completions = app.get_slash_completions("/model ")
            assert any("gemini-4-hydrogen" in insert for insert, _ in arg_completions)

            provider = OptigravityCommandProvider(app.screen, match_style=None)
            discovered = [hit async for hit in provider.discover()]
            titles = [str(h.display) for h in discovered]
            assert any("Glossaire" in t for t in titles)
            assert any("Rotation" in t for t in titles)
            assert any("Modèle →" in t for t in titles)
            assert any("Langue / Language" in t for t in titles)

            await pilot.press("f1")
            await pilot.pause(0.05)
            assert isinstance(app.screen, GlossaryModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, GlossaryModal)

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
            await pilot.pause(0.15)
            assert app.current_state is not None
            assert app.current_state.model_id == "gemini-4-hydrogen"
            assert app.current_state.effort == "medium"

            inp = app.query_one("#prompt-input", PromptTextArea)
            inp.value = "Fix bug in parser.py"
            await inp.action_submit()
            await pilot.pause(0.15)
            assert app.current_state.model_id == "gemini-4-neon"
            assert app.current_state.effort == "low"

            inp.value = "Fix typo in README"
            await inp.action_submit()
            await pilot.pause(0.15)
            assert app.current_state.model_id == "gemini-4-neon"

            await app.action_new_session()
            await pilot.pause(0.1)
            assert app.current_state.model_id == "gemini-4-hydrogen"
            inp.value = "/model gemini-4-carbon"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert app.current_state.model_id == "gemini-4-carbon"

            inp.value = "Fix bug in parser.py"
            await inp.action_submit()
            await pilot.pause(0.15)
            assert app.current_state.model_id == "gemini-4-carbon"

    asyncio.run(_run())


def test_composer_paste_collapse_200_lines_history_and_at_file_no_content_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    secret_marker = "SUPER_SECRET_FILE_BODY_MUST_NEVER_BE_INJECTED"
    (tmp_path / "small.py").write_text(f"# {secret_marker}\nprint('ok')\n", encoding="utf-8")
    (tmp_path / "huge.log").write_text("x" * 12_000, encoding="utf-8")

    fake = FakeAgentConn()
    app = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake,
    )

    async def _run() -> None:
        async with app.run_test() as pilot:
            await pilot.pause(0.25)
            inp = app.query_one("#prompt-input", PromptTextArea)
            composer = app.query_one("#prompt-composer", PromptComposer)
            inp.focus()

            # 1. Paste 200 lines -> collapsed into compact chip, re-injected verbatim on submit
            big_paste = "\n".join(f"line {i}" for i in range(1, 201))
            inp.post_message(events.Paste(big_paste))
            await pilot.pause(0.05)
            assert "[Pasted #1 · 200 lignes]" in inp.value
            assert "line 200" not in inp.value

            # Attach an image block via extra_blocks
            chip = inp.attach_image(b"\x89PNG\r\n", mime_type="image/png")
            assert chip == "[Image #1]"

            await inp.action_submit()
            await pilot.pause(0.2)
            prompt_calls = [args for name, args in fake.calls if name == "prompt"]
            assert prompt_calls
            sent_blocks = prompt_calls[-1]["prompt"]
            assert "line 1" in sent_blocks[0].text and "line 200" in sent_blocks[0].text
            assert len(sent_blocks) == 2 and sent_blocks[1].type == "image"

            # 2. Multiline Ctrl+J newline + cursor-first Up/Down + draft preservation
            inp.value = "first prompt"
            await inp.action_submit()
            await pilot.pause(0.1)
            inp.value = "draft line 1"
            await pilot.press("ctrl+j")
            inp.insert("draft line 2")
            await pilot.pause(0.05)
            assert "\n" in inp.value
            # Up moves cursor from line 2 to line 1 (does NOT trigger history yet)
            await pilot.press("up")
            await pilot.pause(0.05)
            assert "draft line 1" in inp.value
            # Up from line 1 recalls previous history entry
            await pilot.press("up")
            await pilot.pause(0.05)
            assert inp.value == "first prompt"
            # Down restores preserved draft
            await pilot.press("down")
            await pilot.pause(0.05)
            assert "draft line 1" in inp.value

            # 3. @file autocomplete inserts ONLY relative path (0 content injection) + Tab toggles tree view
            inp.value = "Inspect @sm"
            await pilot.pause(0.05)
            popup = app.query_one("#composer-popup", OptionList)
            assert popup.has_class("visible")
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert composer._file_tree_mode is True
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert "@small.py" in inp.value
            assert secret_marker not in inp.value

    asyncio.run(_run())


def test_smart_scroll_no_hijacking_and_raw_markdown_copy(
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
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause(0.15)
            tr = app.get_active_transcript()
            assert tr is not None
            assert tr.is_anchored is True

            code_block = "```python\ndef hello():\n    return 42\n```\n"
            await app._on_acp_event(
                AgentMessageEvent("acp-sess-1", code_block + ("\n".join(f"Line {i}" for i in range(60))))
            )
            await pilot.pause(0.1)

            # User scrolls up manually -> anchor released, 0 scroll hijacking on new chunks
            tr.scroll_y = 0
            tr.detach_anchor()
            await pilot.pause(0.05)
            assert tr.is_anchored is False

            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "\nMore streamed text while reading above."))
            await pilot.pause(0.05)
            pill = app.query_one("#new-messages-pill", Button)
            assert pill.has_class("visible")
            assert tr.is_anchored is False
            assert tr.scroll_y == 0

            tr.pin_to_bottom()
            await pilot.pause(0.05)
            assert tr.is_anchored is True
            assert not pill.has_class("visible")

            msg = tr.query_one(AssistantMessage)
            assert "def hello():\n    return 42" in msg.raw_markdown
            assert msg.extract_code_blocks() == ["def hello():\n    return 42"]

    asyncio.run(_run())


def test_tool_transparency_activity_group_inline_diffs_and_expanded_errors(
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
            await pilot.pause(0.15)
            tr = app.get_active_transcript()
            assert tr is not None

            await app._on_acp_event(ToolCallEvent("acp-sess-1", "r1", "read_file", "a.py", "completed", kind="read"))
            await app._on_acp_event(ToolCallEvent("acp-sess-1", "s1", "grep_search", "foo", "completed", kind="search"))
            await pilot.pause(0.05)
            groups = list(tr.query(ActivityGroup))
            assert len(groups) == 1
            assert groups[0].collapsed is True

            await pilot.press("ctrl+o")
            await pilot.pause(0.05)
            assert app.verbosity == "high"
            assert groups[0].collapsed is False
            await pilot.press("ctrl+o")
            await pilot.pause(0.05)
            assert app.verbosity == "low"

            # Small inline diff (<= 25 lines) -> expanded by default
            await app._on_acp_event(
                ToolCallEvent(
                    "acp-sess-1",
                    "ed1",
                    "edit_file",
                    "app.py",
                    "completed",
                    kind="edit",
                    content=[{"type": "diff", "path": "app.py", "oldText": "x = 1\n", "newText": "x = 2\n"}],
                )
            )
            # Large diff (> 25 lines) -> collapsed summary + fullscreen button
            old_big = "\n".join(f"old_{i}" for i in range(30))
            new_big = "\n".join(f"new_{i}" for i in range(30))
            await app._on_acp_event(
                ToolCallEvent(
                    "acp-sess-1",
                    "ed2",
                    "edit_file",
                    "big.py",
                    "completed",
                    kind="edit",
                    content=[{"type": "diff", "path": "big.py", "oldText": old_big, "newText": new_big}],
                )
            )
            # Failed tool call -> ALWAYS expanded even in low verbosity
            await app._on_acp_event(
                ToolCallEvent(
                    "acp-sess-1",
                    "err1",
                    "run_command",
                    "pytest",
                    "failed",
                    raw_input={"CommandLine": "pytest"},
                    raw_output="AssertionError: boom",
                    is_error=True,
                )
            )
            await pilot.pause(0.05)

            cards = list(tr.query(ToolCard))
            assert len(cards) == 3
            assert cards[0].collapsed is False
            assert cards[1].collapsed is True
            assert "+30 / -30" in str(cards[1].title)
            assert cards[2].collapsed is False

    asyncio.run(_run())


def test_permission_modal_one_key_and_elicitation_form_and_url(
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
            await pilot.pause(0.15)
            loop = asyncio.get_running_loop()

            # 1. PermissionModal: F opens fullscreen DiffPreviewModal, Esc closes preview, Y selects allow_once
            fut1: asyncio.Future[str | None] = loop.create_future()
            await app._on_acp_event(
                PermissionRequestEvent(
                    session_id="acp-sess-1",
                    tool_call_id="p1",
                    tool_name="run_command",
                    title="Run tests",
                    options=[
                        ("allow_once", "Allow once", "allow_once"),
                        ("allow_always", "Allow always", "allow_always"),
                        ("reject_once", "Reject", "reject_once"),
                        ("reject_always", "Never", "reject_always"),
                    ],
                    future=fut1,
                    kind="execute",
                    raw_input={"CommandLine": "pytest -q"},
                )
            )
            await pilot.pause(0.05)
            assert isinstance(app.screen, PermissionModal)
            await pilot.press("f")
            await pilot.pause(0.05)
            assert isinstance(app.screen, DiffPreviewModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert isinstance(app.screen, PermissionModal)
            await pilot.press("y")
            await pilot.pause(0.05)
            assert fut1.done() and fut1.result() == "allow_once"

            # 2. PermissionModal: X selects reject_always
            fut2: asyncio.Future[str | None] = loop.create_future()
            await app._on_acp_event(
                PermissionRequestEvent(
                    session_id="acp-sess-1",
                    tool_call_id="p2",
                    tool_name="run_command",
                    title="rm -rf tmp",
                    options=[
                        ("allow_once", "Allow once", "allow_once"),
                        ("reject_always", "Never", "reject_always"),
                    ],
                    future=fut2,
                    kind="delete",
                )
            )
            await pilot.pause(0.05)
            await pilot.press("x")
            await pilot.pause(0.05)
            assert fut2.done() and fut2.result() == "reject_always"

            # 3. ElicitationModal (form mode) -> dynamic fields + Ctrl+Enter submit
            efut1: asyncio.Future[dict[str, object]] = loop.create_future()
            await app._on_acp_event(
                ElicitationRequestEvent(
                    session_id="acp-sess-1",
                    elicitation_id="el-form-1",
                    message="Configure deployment",
                    future=efut1,
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
            await pilot.pause(0.05)
            assert isinstance(app.screen, ElicitationModal)
            await pilot.press("ctrl+enter")
            await pilot.pause(0.05)
            assert efut1.done()
            assert efut1.result() == {"action": "accept", "content": {"env": "staging", "dry_run": True}}

            # 4. ElicitationModal (url mode) -> displays netloc, no form inputs, auto-completes on server notice
            efut2: asyncio.Future[dict[str, object]] = loop.create_future()
            await app._on_acp_event(
                ElicitationRequestEvent(
                    session_id="acp-sess-1",
                    elicitation_id="el-url-1",
                    message="Sign in to GitHub",
                    future=efut2,
                    mode="url",
                    url="https://github.com/login/device",
                )
            )
            await pilot.pause(0.05)
            assert isinstance(app.screen, ElicitationModal)
            assert "github.com" in str(app.screen.query_one("#elicit-url-host", Static).render())
            assert len(list(app.screen.query(Input))) == 0
            await app._on_acp_event(ElicitationCompleteEvent("acp-sess-1", "el-url-1"))
            await pilot.pause(0.05)
            assert efut2.done() and efut2.result()["action"] == "accept"

    asyncio.run(_run())


def test_multi_session_isolation_new_rotate_and_resume(
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
            await pilot.pause(0.15)
            assert app.client.active_session_id == "acp-sess-1"
            await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Message ONLY in session 1"))
            await pilot.pause(0.05)

            await app.action_new_session()
            await pilot.pause(0.1)
            assert app.client.active_session_id == "acp-sess-2"
            await app._on_acp_event(AgentMessageEvent("acp-sess-2", "Message ONLY in session 2"))
            await pilot.pause(0.05)

            tr1 = app._transcripts["acp-sess-1"]
            tr2 = app._transcripts["acp-sess-2"]
            assert tr1 is not tr2
            assert any("session 1" in m.raw_markdown for m in tr1.query(AssistantMessage))
            assert not any("session 2" in m.raw_markdown for m in tr1.query(AssistantMessage))
            assert any("session 2" in m.raw_markdown for m in tr2.query(AssistantMessage))
            assert not any("session 1" in m.raw_markdown for m in tr2.query(AssistantMessage))

            # /rotate opens HandoffReviewModal -> confirm writes .ogy/handoff.md & opens acp-sess-3
            await app.action_open_rotation_review()
            await pilot.pause(0.1)
            assert isinstance(app.screen, HandoffReviewModal)
            editor = app.screen.query_one("#handoff-editor", TextArea)
            editor.insert("\nCustom note before rotation")
            await pilot.press("ctrl+enter")
            await pilot.pause(0.2)
            assert app.client.active_session_id == "acp-sess-3"
            assert (tmp_path / ".ogy" / "handoff.md").is_file()
            assert "Custom note before rotation" in (tmp_path / ".ogy" / "handoff.md").read_text(encoding="utf-8")

            # Ctrl+S opens SessionPickerModal -> resume acp-sess-1 switches ContentSwitcher
            await pilot.press("ctrl+s")
            await pilot.pause(0.1)
            assert isinstance(app.screen, SessionPickerModal)
            app.screen.dismiss("acp-sess-1")
            await pilot.pause(0.1)
            assert app.client.active_session_id == "acp-sess-1"
            switcher = app.query_one("#session-switcher", ContentSwitcher)
            assert switcher.current == app._transcript_widget_id("acp-sess-1")

            # [V] switch_to_v1_run proposal when requesting Claude under oauth-personal
            inp = app.query_one("#prompt-input", PromptTextArea)
            inp.value = "/model claude-5.5-sonnet"
            await inp.action_submit()
            await pilot.pause(0.05)
            assert any(p.kind == "switch_to_v1_run" for p in app.active_proposals)
            await app.action_accept_proposal()
            await pilot.pause(0.05)
            assert not any(p.kind == "switch_to_v1_run" for p in app.active_proposals)

    asyncio.run(_run())


def test_scrollbar_syncs_position_and_streamed_file_links_no_readme_linkify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Markdown
    from ogy_frontends.acp_tui.widgets.transcript import create_safe_markdown_parser

    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    fake = FakeAgentConn()
    app = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake,
    )

    async def _run() -> None:
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause(0.15)
            tr = app.get_active_transcript()
            assert tr is not None

            # 1. Stream file links split across tiny 3-char chunks + plain README.md
            full_text = (
                "Voir [install.ps1](file:///C:/Users/pbapi/My%20Projects/install.ps1) et "
                "[README.md](file:///C:/Users/pbapi/README.md#L97-L109).\n\n"
                "Consultez aussi README.md, app.py et [Docs](https://example.com).\n\n"
                "```md\n[keep.py](file:///C:/Users/pbapi/keep.py)\n```\n\n"
                + "\n".join(f"Paragraph line {i}" for i in range(45))
            )
            for idx in range(0, len(full_text), 3):
                await app._on_acp_event(AgentMessageEvent("acp-sess-1", full_text[idx : idx + 3]))
            await tr.close_active_message()
            await pilot.pause(0.1)

            msg = tr.query_one(AssistantMessage)
            md_widget = msg.query_one(Markdown)
            assert "`install.ps1`" in md_widget.source
            assert "`README.md:97-109`" in md_widget.source
            assert "file:///C:/Users/pbapi/My%20Projects/install.ps1" not in md_widget.source
            assert "[keep.py](file:///C:/Users/pbapi/keep.py)" in md_widget.source

            # Safe parser must NOT auto-linkify README.md or app.py into http://README.md
            tokens = create_safe_markdown_parser().parse(md_widget.source)
            hrefs = [
                tok.attrs.get("href")
                for parent in tokens
                for tok in (parent.children or [])
                if tok.type == "link_open"
            ]
            assert "https://example.com" in hrefs
            assert not any("README.md" in str(h) or "app.py" in str(h) for h in hrefs)

            # 2. Scrollbar position stays in sync when scroll_y changes (super().watch_scroll_y)
            assert tr.max_scroll_y > 0
            tr.scroll_y = 0
            await pilot.pause(0.05)
            assert tr.vertical_scrollbar.position == 0
            mid = max(1, int(tr.max_scroll_y // 2))
            tr.scroll_y = mid
            await pilot.pause(0.05)
            assert tr.vertical_scrollbar.position == mid

    asyncio.run(_run())


def test_resume_existing_session_restores_history_via_acp_load_and_local_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from ogy_frontends.acp_tui.state.sessions import append_local_transcript_entry

    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

    # --- Path A: ACP server advertises loadSession=True and replays history ---
    class LoadableAgentConn(FakeAgentConn):
        def __init__(self) -> None:
            super().__init__()
            self.client_ref = None

        async def initialize(self, **kwargs):
            self.calls.append(("initialize", kwargs))
            return SimpleNamespace(
                protocol_version=1,
                agent_capabilities={"load_session": True, "session": {"fork": True}},
            )

        async def load_session(self, **kwargs):
            self.calls.append(("load_session", kwargs))
            sid = kwargs["session_id"]
            assert self.client_ref is not None
            await self.client_ref._handle_session_update(
                sid,
                SimpleNamespace(
                    session_update="user_message_chunk",
                    content=SimpleNamespace(text="Old user prompt from ACP"),
                    message_id="u1",
                ),
            )
            await self.client_ref._handle_session_update(
                sid,
                SimpleNamespace(
                    session_update="agent_message_chunk",
                    content=SimpleNamespace(text="Old assistant answer from ACP"),
                    message_id="a1",
                ),
            )
            return SimpleNamespace(config_options=[SimpleNamespace(id="model", current_value="gemini-4-neon")])

    fake_load = LoadableAgentConn()
    app1 = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake_load,
    )
    fake_load.client_ref = app1.client

    async def _run_acp_load() -> None:
        async with app1.run_test() as pilot:
            await pilot.pause(0.15)
            assert app1.client.supports_load is True
            await app1._resume_session_by_id("existing-acp-session-42")
            await pilot.pause(0.1)

            assert any(name == "load_session" for name, _ in fake_load.calls)
            tr = app1._transcripts["existing-acp-session-42"]
            user_texts = [str(w.render()) for w in tr.query(".user-msg")]
            assert any("Old user prompt from ACP" in txt for txt in user_texts)
            msgs = list(tr.query(AssistantMessage))
            assert any("Old assistant answer from ACP" in m.raw_markdown for m in msgs)
            assert not app1.query_one("#activity-bar", Static).has_class("visible")

    asyncio.run(_run_acp_load())

    # --- Path B: Server does NOT support loadSession -> local JSONL cache fallback ---
    append_local_transcript_entry(db_path, "cached-session-99", "user", "Cached prompt from previous run")
    append_local_transcript_entry(db_path, "cached-session-99", "assistant", "Cached answer from previous run")

    fake_no_load = FakeAgentConn()
    app2 = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake_no_load,
    )

    async def _run_local_cache() -> None:
        async with app2.run_test() as pilot:
            await pilot.pause(0.15)
            assert app2.client.supports_load is False
            await app2._resume_session_by_id("cached-session-99")
            await pilot.pause(0.1)

            tr = app2._transcripts["cached-session-99"]
            user_texts = [str(w.render()) for w in tr.query(".user-msg")]
            assert any("Cached prompt from previous run" in txt for txt in user_texts)
            msgs = list(tr.query(AssistantMessage))
            assert any("Cached answer from previous run" in m.raw_markdown for m in msgs)

    asyncio.run(_run_local_cache())

