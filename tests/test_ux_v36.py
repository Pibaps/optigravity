"""Comprehensive V3.6 UX test suite (Tier 1 CLI/Config + Tier 2 Headless TUI Pilot + Fake ACP)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from textual.containers import Horizontal
from textual.widgets import Button, Input, Static

from ogy_cli.config_cmd import set_config_value, validate_config_file
from ogy_cli.init_audit import cmd_init
from ogy_cli.main import build_parser, cmd_doctor, main as cli_main
from ogy_cli.stats_render import query_filtered_stats, render_stats_report
from ogy_core.config import load_config
from ogy_core.storage import connect, ensure_session, upsert_turn
from ogy_frontends.acp_tui.client import AgentMessageEvent, ToolCallEvent, UsageEvent
from ogy_frontends.acp_tui.screens.modals import HandoffReviewModal
from ogy_frontends.acp_tui.widgets.composer import PromptTextArea
from ogy_frontends.acp_tui.widgets.telemetry import TelemetryPanel
from ogy_frontends.acp_tui.widgets.transcript import AssistantMessage, WelcomeCard
from tests.test_acp_client import FakeAgentConn


# ---------------------------------------------------------------------------
# Tier 1: Config, CLI Doctor --json / --fix OAuth, Stats ROI Savings & Typos
# ---------------------------------------------------------------------------


def test_config_tui_theme_schema_and_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("OGY_CONFIG_PATH", str(cfg_path))

    assert set_config_value("tui.theme", "light", config_path=cfg_path) == "light"
    assert validate_config_file(cfg_path) == []
    assert load_config(cfg_path).tui_theme == "light"

    assert set_config_value("tui.theme", "high-contrast", config_path=cfg_path) == "high-contrast"
    assert load_config(cfg_path).tui_theme == "high-contrast"

    with pytest.raises(ValueError, match="tui.theme"):
        set_config_value("tui.theme", "neon-pink", config_path=cfg_path)


def test_doctor_json_and_oauth_autofix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_home = tmp_path / "_ogy_test_home"
    shared_gemini = fake_home / ".gemini"
    shared_gemini.mkdir(parents=True, exist_ok=True)
    (shared_gemini / "oauth_creds.json").write_text('{"token":"fake"}\n', encoding="utf-8")

    assert cmd_init(profile_mode="isolated", non_interactive=True) == 0
    capsys.readouterr()

    # Break the isolated OAuth link intentionally
    cfg = load_config()
    assert cfg.gemini_home is not None
    broken_oauth = cfg.gemini_home / "oauth_creds.json"
    if broken_oauth.exists() or broken_oauth.is_symlink():
        broken_oauth.unlink()

    # Doctor --json should report oauth_links anomaly
    assert cmd_doctor(as_json=True) == 0
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["profile_mode"] == "isolated"
    assert report["checks"]["oauth_links"]["status"] == "warn"
    assert any(a["code"] == "oauth_links" for a in report["anomalies"])

    # Doctor --fix --yes --json should repair oauth_links automatically
    assert cmd_doctor(fix=True, yes=True, as_json=True) == 0
    fix_report = json.loads(capsys.readouterr().out)
    assert any("Re-linked OAuth" in msg for msg in fix_report["fixed"])
    assert broken_oauth.exists()


def test_cli_typo_suggestion_and_help_epilog(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(["stat"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Did you mean 'ogy stats'?" in err

    help_text = build_parser().format_help()
    assert "Quick-start examples:" in help_text
    assert "ogy init --isolated" in help_text


def test_stats_report_includes_cache_savings_usd(tmp_path: Path) -> None:
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(
            conn,
            session_id="sess-roi-v36",
            surface="cli_acp",
            workspace_path=str(tmp_path),
            initial_model="gemini-4-hydrogen",
        )
        upsert_turn(
            conn,
            session_id="sess-roi-v36",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=5_000,
            cache_read_tokens=500_000,
            output_tokens=400,
            context_window_used=505_000,
            estimated_cost_usd=0.05,
            data_source="acp",
        )
        data = query_filtered_stats(conn)
        out = render_stats_report(conn, data)
        assert "est. saved $" in out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tier 2: Headless TUI Pilot + Fake ACP UX Tests (V3.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_welcome_card_shows_and_hides_on_first_message(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        tr = app.get_active_transcript()
        assert tr is not None
        cards = list(tr.query(WelcomeCard))
        assert len(cards) == 1
        assert cards[0].display is True

        await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Hello from ACP!"))
        await pilot.pause()
        assert cards[0].display is False


@pytest.mark.asyncio
async def test_tui_responsive_sidebar_auto_collapse_and_override(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(80, 24)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        panel = app.query_one("#telemetry-panel", TelemetryPanel)
        # Narrow terminal (< 100 cols) auto-hides sidebar and shows inline telemetry in session-bar
        assert panel.has_class("hidden")
        bar_text = str(app.query_one("#session-bar", Static).render())
        assert "Ctx " in bar_text
        assert "Cache " in bar_text

        # Manual Ctrl+B override forces sidebar visible even on narrow terminal
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert not panel.has_class("hidden")

    # Wide terminal (>= 120 cols) shows full sidebar by default
    app_wide = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app_wide.run_test(size=(140, 40)) as pilot:
        await app_wide.workers.wait_for_complete()
        await pilot.pause()
        panel_wide = app_wide.query_one("#telemetry-panel", TelemetryPanel)
        assert not panel_wide.has_class("hidden")
        assert not panel_wide.has_class("compact")


@pytest.mark.asyncio
async def test_tui_themes_live_switch_and_completions(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.tui_theme == "dark"
        assert not app.screen.has_class("theme-light")

        completions = app.get_slash_completions("/theme ")
        assert [c[0] for c in completions] == ["/theme dark", "/theme light", "/theme high-contrast"]

        await app._handle_slash_command("/theme light")
        await pilot.pause()
        assert app.tui_theme == "light"
        assert app.screen.has_class("theme-light")
        assert not app.screen.has_class("theme-high-contrast")

        await app._handle_slash_command("/theme high-contrast")
        await pilot.pause()
        assert app.tui_theme == "high-contrast"
        assert app.screen.has_class("theme-high-contrast")
        assert not app.screen.has_class("theme-light")

        await app._handle_slash_command("/theme dark")
        await pilot.pause()
        assert app.tui_theme == "dark"
        assert not app.screen.has_class("theme-light")
        assert not app.screen.has_class("theme-high-contrast")


@pytest.mark.asyncio
async def test_tui_prompt_queueing_and_clear_queue(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Simulate active working turn
        app._set_working(True, "Working...")
        ta = app.query_one("#prompt-input", PromptTextArea)
        ta.value = "Follow-up task while busy"
        await ta.action_submit()
        await pilot.pause()

        sid = app.registry.active_session_id
        assert sid is not None
        assert app.registry.queue_len(sid) == 1
        qbar = app.query_one("#queue-bar", Horizontal)
        assert qbar.has_class("visible")
        assert "Follow-up task" in str(app.query_one("#queue-text", Static).render())

        # /clear-queue empties the queue and hides #queue-bar
        await app._handle_slash_command("/clear-queue")
        await pilot.pause()
        assert app.registry.queue_len(sid) == 0
        assert not qbar.has_class("visible")


@pytest.mark.asyncio
async def test_tui_per_session_draft_preservation(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        sid1 = app.registry.active_session_id
        assert sid1 is not None
        ta = app.query_one("#prompt-input", PromptTextArea)
        ta.value = "Draft for session one"

        # Switch to a new session -> draft for sid1 is saved, composer cleared
        await app.action_new_session()
        await pilot.pause()
        sid2 = app.registry.active_session_id
        assert sid2 is not None and sid2 != sid1
        assert ta.text == ""

        ta.value = "Draft for session two"

        # Resume back to sid1 -> draft for sid1 is restored
        await app._resume_session_by_id(sid1)
        await pilot.pause()
        assert ta.text == "Draft for session one"


@pytest.mark.asyncio
async def test_tui_proposal_hotkey_safety(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Put session in red zone so [R] rotate_session proposal is active
        await app._on_acp_event(UsageEvent(session_id="acp-sess-1", context_used=130_000, is_turn_final=True))
        await pilot.pause()
        assert any(p.hotkey == "r" for p in app.active_proposals)

        # Composer is focused: typing 'r' MUST insert 'r' and NOT open HandoffReviewModal
        ta = app.query_one("#prompt-input", PromptTextArea)
        ta.focus()
        await pilot.press("r")
        await pilot.pause()
        assert not isinstance(app.screen, HandoffReviewModal)
        assert ta.text == "r"

        # Pressing Alt+R DOES open HandoffReviewModal even while composer is focused
        await pilot.press("alt+r")
        await asyncio.sleep(0.05)
        await pilot.pause()
        assert isinstance(app.screen, HandoffReviewModal)


@pytest.mark.asyncio
async def test_tui_transcript_search_ctrl_f(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await app._on_acp_event(AgentMessageEvent("acp-sess-1", "First alpha message"))
        await app._on_acp_event(ToolCallEvent("acp-sess-1", "t1", "run_command", "alpha tool", "completed"))
        await app._on_acp_event(AgentMessageEvent("acp-sess-1", "Second alpha message"))
        tr = app.get_active_transcript()
        assert tr is not None
        await tr.close_active_message()
        await pilot.pause()

        await pilot.press("ctrl+f")
        await pilot.pause()
        sbar = app.query_one("#transcript-search-bar", Horizontal)
        assert sbar.has_class("visible")

        sinp = app.query_one("#transcript-search-input", Input)
        sinp.value = "alpha"
        await pilot.pause()
        count_txt = str(app.query_one("#transcript-search-count", Static).render())
        assert count_txt == "1/3"

        await pilot.press("enter")
        await pilot.pause()
        assert str(app.query_one("#transcript-search-count", Static).render()) == "2/3"

        await pilot.press("escape")
        await pilot.pause()
        assert not sbar.has_class("visible")


@pytest.mark.asyncio
async def test_tui_copy_code_button_slash_copy_export_and_status(tmp_path: Path, make_tui_app: Any) -> None:
    copied: list[str] = []
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en", cwd=tmp_path)
    app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]

    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        tr = app.get_active_transcript()
        assert tr is not None
        await tr.append_user_message("Show me a helper")
        await app._on_acp_event(
            AgentMessageEvent("acp-sess-1", "Here is the snippet:\n```python\ndef add(a, b):\n    return a + b\n```\n")
        )
        await tr.close_active_message()
        await pilot.pause()

        msgs = list(tr.query(AssistantMessage))
        assert len(msgs) == 1
        code_btn = msgs[0].query_one(".copy-code-btn", Button)
        assert code_btn.display is True

        # /copy code copies raw code without fences
        await app._handle_slash_command("/copy code")
        await pilot.pause()
        assert copied[-1] == "def add(a, b):\n    return a + b"

        # /export writes clean Markdown file
        export_target = tmp_path / "exported_session.md"
        await app._handle_slash_command(f"/export {export_target}")
        await pilot.pause()
        assert export_target.is_file()
        exported_text = export_target.read_text(encoding="utf-8")
        assert "## User" in exported_text
        assert "## Assistant" in exported_text
        assert "def add(a, b):" in exported_text

        # /status appends inline summary
        await app._handle_slash_command("/status")
        await pilot.pause()
        notices = [str(w.render()) for w in tr.query(".notice-line")]
        assert any("Model:" in n and "Cache:" in n for n in notices)


@pytest.mark.asyncio
async def test_composer_live_token_estimate_badge(make_tui_app: Any) -> None:
    app = make_tui_app(injected_agent_conn=FakeAgentConn(), lang="en")
    async with app.run_test(size=(120, 36)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        ta = app.query_one("#prompt-input", PromptTextArea)
        bar = app.query_one("#attachment-bar", Static)
        assert not bar.has_class("visible")

        ta.value = "a" * 400
        await pilot.pause()
        assert bar.has_class("visible")
        assert "~100 tok" in str(bar.render())
