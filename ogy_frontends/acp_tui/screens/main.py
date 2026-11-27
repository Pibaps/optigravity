"""Main TUI workspace controller: multi-session ContentSwitcher, SQLite worker, ACP event routing, slash commands."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape as rich_escape
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Footer, Header, Input, Static

from ogy_core import get_version
from ogy_core.context_health import write_handoff
from ogy_core.router import route_stage1
from ogy_core.storage import connect, write_tx
from ogy_frontends.acp_tui.client import (
    AcpAuthError,
    AcpEvent,
    AgentMessageEvent,
    AgentThoughtEvent,
    AvailableCommandsEvent,
    ConfigUpdateEvent,
    CurrentModeUpdateEvent,
    ElicitationCompleteEvent,
    ElicitationRequestEvent,
    PermissionRequestEvent,
    PlanUpdateEvent,
    SessionNoticeEvent,
    ToolCallEvent,
    UsageEvent,
    UserMessageEvent,
)
from ogy_frontends.acp_tui.i18n import SUPPORTED_LANGS, t
from ogy_frontends.acp_tui.orchestrator import ActionProposal, SessionTelemetryState
from ogy_frontends.acp_tui.screens.modals import (
    ContextInspectorModal,
    DelegationModal,
    DiffPreviewModal,
    ElicitationModal,
    HandoffReviewModal,
    HelpGlossaryModal,
    PermissionModal,
    QuickSelectModal,
    SessionPickerModal,
)
from ogy_frontends.acp_tui.state.sessions import (
    TelemetrySnapshot,
    append_local_transcript_entry,
    fetch_telemetry_snapshot,
    load_local_transcript_entries,
    load_sessions_meta_readonly,
)
from ogy_frontends.acp_tui.widgets.composer import PromptComposer, PromptTextArea, _format_size
from ogy_frontends.acp_tui.widgets.telemetry import (
    TelemetryPanel,
    _format_tokens,
    _safe_widget_id,
    estimate_cache_savings_usd,
)
from ogy_frontends.acp_tui.widgets.transcript import (
    AssistantMessage,
    SessionTranscript,
    ShellCard,
    ToolCard,
)

DEFAULT_MODELS = ["gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon"]
DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_MODES = ["default", "accept-edits", "auto", "plan"]
DEFAULT_FRUGAL_MODEL = "gemini-4-hydrogen"
DEFAULT_FRUGAL_EFFORT = "medium"
SUPPORTED_THEMES = ("dark", "light", "high-contrast")
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    name: str
    usage: str
    desc_key: str
    takes_arg: bool = False


SLASH_COMMANDS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("/model", "/model <slug>", "cmd_model", True),
    SlashCommandSpec("/effort", "/effort <level>", "cmd_effort", True),
    SlashCommandSpec("/mode", "/mode <mode>", "cmd_mode", True),
    SlashCommandSpec("/select", "/select [kind]", "cmd_select", True),
    SlashCommandSpec("/btw", "/btw <question>", "cmd_btw", True),
    SlashCommandSpec("/delegate", "/delegate [question]", "cmd_delegate", True),
    SlashCommandSpec("/diff", "/diff", "cmd_diff"),
    SlashCommandSpec("/attach", "/attach <path>", "cmd_attach", True),
    SlashCommandSpec("/context", "/context", "cmd_context"),
    SlashCommandSpec("/mcp", "/mcp", "cmd_mcp"),
    SlashCommandSpec("/config", "/config [key val]", "cmd_config", True),
    SlashCommandSpec("/theme", "/theme <name>", "cmd_theme", True),
    SlashCommandSpec("/lang", "/lang <fr|en>", "cmd_lang", True),
    SlashCommandSpec("/copy", "/copy [code]", "cmd_copy", True),
    SlashCommandSpec("/export", "/export [path]", "cmd_export", True),
    SlashCommandSpec("/status", "/status", "cmd_status"),
    SlashCommandSpec("/find", "/find", "cmd_find"),
    SlashCommandSpec("/clear-queue", "/clear-queue", "cmd_clear_queue"),
    SlashCommandSpec("/fork", "/fork", "cmd_fork"),
    SlashCommandSpec("/compact", "/compact", "cmd_compact"),
    SlashCommandSpec("/rotate", "/rotate", "cmd_rotate"),
    SlashCommandSpec("/handoff", "/handoff", "cmd_handoff"),
    SlashCommandSpec("/new", "/new", "cmd_new"),
    SlashCommandSpec("/sessions", "/sessions", "cmd_sessions"),
    SlashCommandSpec("/resume", "/resume [id]", "cmd_resume", True),
    SlashCommandSpec("/clear", "/clear", "cmd_clear"),
    SlashCommandSpec("/glossary", "/glossary", "cmd_glossary"),
    SlashCommandSpec("/help", "/help", "cmd_help"),
)


def _resolve_acp_model_slug(available_models: list[str], target_model: str, target_effort: str) -> str:
    if not available_models or target_model in available_models:
        return target_model
    combined = f"{target_model}-{target_effort}"
    if combined in available_models:
        return combined
    for candidate in available_models:
        if candidate.startswith(f"{target_model}-"):
            return candidate
    return target_model


def _detect_has_test_runner(cwd: str) -> bool:
    root = Path(cwd)
    return any(
        (root / marker).exists()
        for marker in ("pyproject.toml", "pytest.ini", "tests", "package.json", "Cargo.toml", "go.mod")
    )


def _detect_git_branch(cwd: str) -> str:
    """Return current git branch name (run in worker thread; returns '' outside git repos)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip()
            return branch if branch and branch != "HEAD" else ""
    except Exception:
        pass
    return ""


def _collect_git_diff(cwd: str) -> tuple[str, Text]:
    """Collect `git status --short` + unified `git diff` (+ staged) in a worker thread.

    Returns `(status, rich_text)` where `status` is `'ok' | 'clean' | 'not_git'`.
    """
    try:
        st = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if st.returncode != 0:
            return "not_git", Text("")
        d_unstaged = subprocess.run(
            ["git", "diff", "--no-color"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        d_staged = subprocess.run(
            ["git", "diff", "--cached", "--no-color"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except Exception:
        return "not_git", Text("")

    status_out = st.stdout.strip()
    diff_parts = [p for p in (d_staged.stdout.strip(), d_unstaged.stdout.strip()) if p]
    if not status_out and not diff_parts:
        return "clean", Text("")

    rich_out = Text()
    if status_out:
        rich_out.append("=== git status --short ===\n", style="bold #6ea8fe")
        rich_out.append(status_out + "\n\n", style="#c9d1d9")
    for raw_diff in diff_parts:
        for line in raw_diff.splitlines():
            if line.startswith("+++") or line.startswith("---") or line.startswith("diff --git"):
                rich_out.append(line + "\n", style="bold #8b949e")
            elif line.startswith("@@"):
                rich_out.append(line + "\n", style="bold #6ea8fe")
            elif line.startswith("+"):
                rich_out.append(line + "\n", style="#3fb950 on #12261e")
            elif line.startswith("-"):
                rich_out.append(line + "\n", style="#f85149 on #2b1417")
            else:
                rich_out.append(line + "\n", style="#c9d1d9")
    return "ok", rich_out


def _collect_mcp_servers(agy_config_dir: Path, cwd: str) -> list[dict[str, Any]]:
    """Read configured MCP servers from profile & workspace `mcp_config.json` (read-only)."""
    candidates = [
        ("profile", agy_config_dir / "mcp_config.json"),
        ("workspace", Path(cwd) / ".agents" / "mcp_config.json"),
    ]
    servers: list[dict[str, Any]] = []
    for scope, path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_map = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(raw_map, dict):
            continue
        for name, spec in sorted(raw_map.items()):
            if not isinstance(spec, dict):
                continue
            cmd = str(spec.get("command") or spec.get("url") or "?")
            eager = bool(spec.get("eager", False))
            servers.append({"name": name, "scope": scope, "command": cmd, "eager": eager, "path": str(path)})
    return servers


def _collect_context_artifacts(cwd: str) -> dict[str, list[tuple[str, int]]]:
    """Scan workspace for agy/Claude rules, memory files, and skills (run in worker thread)."""
    root = Path(cwd).resolve()
    memory_files: list[tuple[str, int]] = []
    for name in ("GEMINI.md", "AGENTS.md", "CLAUDE.md", ".ogy/handoff.md"):
        p = root / name
        if p.is_file():
            try:
                memory_files.append((name, p.stat().st_size))
            except OSError:
                pass
    rules: list[tuple[str, int]] = []
    rules_dir = root / ".agents" / "rules"
    if rules_dir.is_dir():
        for p in sorted(rules_dir.glob("*.md"))[:20]:
            try:
                rules.append((p.relative_to(root).as_posix(), p.stat().st_size))
            except OSError:
                pass
    skills: list[tuple[str, int]] = []
    skills_dir = root / ".agents" / "skills"
    if skills_dir.is_dir():
        for p in sorted(skills_dir.glob("*/SKILL.md"))[:20]:
            try:
                skills.append((p.parent.name, p.stat().st_size))
            except OSError:
                pass
    return {"memory": memory_files, "rules": rules, "skills": skills}


def _build_handoff_draft(state: SessionTelemetryState, cwd: str) -> str:
    git_short = ""
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0:
            git_short = proc.stdout.strip()
    except Exception:
        git_short = ""

    lines = [
        "# Session Handoff",
        "",
        f"## Goal\n{state.active_goal}",
        "",
        "## Modified Files",
    ]
    if git_short:
        lines.append(f"```\n{git_short}\n```")
    elif state.modified_files:
        lines.extend(f"- {f}" for f in state.modified_files[:20])
    else:
        lines.append("- (none recorded)")

    lines.extend(["", "## Last Error / Checks"])
    lines.append(state.last_error_summary or "No recent tool errors.")
    if state.consecutive_failures >= 2:
        lines.extend(["", "## Negative Constraints", "- Do not repeat failing approach without changing strategy."])
    lines.extend(["", "## Next Step", "Resume task cleanly from this handoff."])
    return "\n".join(lines) + "\n"


class MainWorkspaceControllerMixin:
    """Mixin implementing the main TUI layout, non-blocking SQLite worker, multi-session transcripts, and actions."""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(t(self.lang, "connecting"), id="session-bar", markup=False)
        with Horizontal(id="proposal-bar"):
            yield Static("", id="proposal-text", markup=False)
            yield Button(t(self.lang, "apply"), id="proposal-apply-btn")
        with Horizontal(id="main-body"):
            with Vertical(id="transcript-pane"):
                yield ContentSwitcher(id="session-switcher")
                yield Button(t(self.lang, "new_messages_pill"), id="new-messages-pill")
            yield TelemetryPanel(lang=self.lang, id="telemetry-panel")
        with Horizontal(id="transcript-search-bar"):
            yield Input(
                placeholder=t(self.lang, "transcript_search_placeholder"),
                id="transcript-search-input",
            )
            yield Static("", id="transcript-search-count", markup=False)
        with Horizontal(id="queue-bar"):
            yield Static("", id="queue-text", markup=False)
            yield Button(t(self.lang, "queue_clear_btn"), id="queue-clear-btn")
        yield Static("", id="activity-bar", markup=False)
        yield PromptComposer(
            cwd=self.client_config.cwd,
            db_path=self.ogy_config.db_path,
            slash_provider=self.get_slash_completions,
            enter_behavior=self.ogy_config.tui_enter_behavior,
            lang=self.lang,
            firebreak_limits=(
                self.ogy_config.firebreak_file_max_bytes,
                self.ogy_config.firebreak_doc_max_bytes,
                self.ogy_config.firebreak_data_max_bytes,
            ),
            id="prompt-composer",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_theme(self.tui_theme, notify=False)
        self._apply_responsive_layout(self.size.width)
        self.set_interval(5.0, self._schedule_telemetry_poll)
        self.set_interval(0.12, self._tick_spinner)
        self._schedule_telemetry_poll()
        self._detect_git_branch_worker()
        self.query_one("#prompt-input", PromptTextArea).focus()
        if self.auto_connect:
            self._boot_acp()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def _apply_responsive_layout(self, width: int) -> None:
        try:
            panel = self.query_one("#telemetry-panel", TelemetryPanel)
        except Exception:
            return
        if self._sidebar_user_override is not None:
            panel.set_class(not self._sidebar_user_override, "hidden")
        else:
            panel.set_class(width < 100, "hidden")
        panel.set_class(100 <= width < 120, "compact")
        if self.current_state is not None:
            self._refresh_ui()

    def _apply_theme(self, theme_name: str, *, notify: bool = True) -> None:
        norm = theme_name.strip().lower()
        if norm not in SUPPORTED_THEMES:
            return
        self.tui_theme = norm
        for target in (self, self.screen):
            try:
                target.set_class(norm == "light", "theme-light")
                target.set_class(norm == "high-contrast", "theme-high-contrast")
            except Exception:
                pass
        if notify:
            self._toast(t(self.lang, "theme_changed", val=norm))

    def _toast(self, message: str, *, severity: str = "information", timeout: float = 3.0) -> None:
        try:
            self.notify(message, severity=severity, timeout=timeout)
        except Exception:
            pass

    @work(thread=True, exclusive=True, group="git-branch-detect")
    def _detect_git_branch_worker(self) -> None:
        branch = _detect_git_branch(self.client_config.cwd)
        self.call_from_thread(self._apply_git_branch, branch)

    def _apply_git_branch(self, branch: str) -> None:
        self._git_branch = branch
        for tr in self._transcripts.values():
            tr.update_welcome_meta(branch=branch)
        if self.current_state is not None:
            self._refresh_ui()

    async def on_unmount(self) -> None:
        self._working = False
        for tr in list(self._transcripts.values()):
            try:
                await tr.close_active_message()
                tr.close_active_thought()
                tr.close_active_user_message()
            except Exception:
                pass
        for task in list(self._inflight_tasks):
            task.cancel()
        try:
            await self.client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Non-blocking SQLite telemetry worker (Invariant #2: 0 sync SQLite on UI thread)
    # ------------------------------------------------------------------

    def _schedule_telemetry_poll(self) -> None:
        self._poll_telemetry_worker()

    @work(thread=True, exclusive=True, group="sqlite-telemetry-poll")
    def _poll_telemetry_worker(self) -> None:
        snap = fetch_telemetry_snapshot(self.ogy_config.db_path, self.registry.active_session_id)
        self.call_from_thread(self._apply_telemetry_snapshot, snap)

    def _apply_telemetry_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self.registry.update_snapshot(snapshot)
        if self.current_state is not None:
            self._refresh_ui()

    @work(thread=True, group="routing-decision-persist")
    def _persist_routing_decision_worker(
        self,
        session_id: str,
        stage: str,
        category: str,
        features: dict[str, Any],
        model: str,
        effort: str,
        mode: str,
    ) -> None:
        try:
            conn = connect(self.ogy_config.db_path, init_schema=True)
            try:
                with write_tx(conn):
                    conn.execute(
                        """
                        INSERT INTO routing_decisions (
                            session_id, stage, detected_category, features_json,
                            chosen_model, chosen_effort, chosen_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (session_id, stage, category, json.dumps(features), model, effort, mode),
                    )
            finally:
                conn.close()
        except Exception:
            pass

    def _schedule_transcript_persist(self, session_id: str, role: str, text: str) -> None:
        if session_id in self._replaying_sessions or not text.strip():
            return
        self._persist_transcript_entry_worker(session_id, role, text)

    @work(thread=True, group="transcript-cache-persist")
    def _persist_transcript_entry_worker(self, session_id: str, role: str, text: str) -> None:
        append_local_transcript_entry(self.ogy_config.db_path, session_id, role, text)

    # ------------------------------------------------------------------
    # Multi-session isolated SessionTranscript management
    # ------------------------------------------------------------------

    def _transcript_widget_id(self, session_id: str) -> str:
        return f"transcript-{_safe_widget_id(session_id)}"

    def _build_welcome_info(self) -> dict[str, str]:
        ws_name = Path(self.client_config.cwd).name or self.client_config.cwd
        return {
            "version": f"v{get_version()}",
            "workspace": ws_name,
            "branch": self._git_branch,
            "profile": self.ogy_config.profile_mode,
            "subagents": self.ogy_config.subagents_privacy_mode,
        }

    def _switch_session_draft(self, old_sid: str | None, new_sid: str | None) -> None:
        if not old_sid or not new_sid or old_sid == new_sid:
            return
        try:
            ta = self.query_one("#prompt-input", PromptTextArea)
        except Exception:
            return
        self.registry.save_draft(old_sid, ta.text)
        restored = self.registry.get_draft(new_sid)
        ta.value = restored

    async def _ensure_transcript(self, session_id: str, *, activate: bool = False) -> SessionTranscript:
        prev_sid = self.registry.active_session_id
        existing = self._transcripts.get(session_id)
        switcher = self.query_one("#session-switcher", ContentSwitcher)
        if existing is None:
            wid = self._transcript_widget_id(session_id)
            existing = SessionTranscript(
                session_id,
                lang=self.lang,
                verbosity=self.verbosity,
                on_persist=self._schedule_transcript_persist,
                welcome_info=self._build_welcome_info(),
                id=wid,
            )
            self._transcripts[session_id] = existing
            await switcher.mount(existing)
        if activate:
            self._switch_session_draft(prev_sid, session_id)
            switcher.current = existing.id
            self._update_new_messages_pill(existing.is_anchored, False)
            self._refresh_queue_bar()
        return existing

    def get_active_transcript(self) -> SessionTranscript | None:
        sid = self.registry.active_session_id or self.client.active_session_id
        if not sid:
            return None
        return self._transcripts.get(sid)

    def _update_new_messages_pill(self, anchored: bool, has_unread: bool) -> None:
        pill = self.query_one("#new-messages-pill", Button)
        if not anchored and has_unread:
            pill.add_class("visible")
        else:
            pill.remove_class("visible")

    def on_session_transcript_scroll_anchor_changed(self, event: SessionTranscript.ScrollAnchorChanged) -> None:
        if event.session_id == (self.registry.active_session_id or self.client.active_session_id):
            self._update_new_messages_pill(event.anchored, event.has_unread)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "new-messages-pill":
            tr = self.get_active_transcript()
            if tr is not None:
                tr.pin_to_bottom()
        elif btn_id == "proposal-apply-btn":
            await self.action_accept_proposal()
        elif btn_id == "queue-clear-btn":
            await self._clear_active_queue()

    async def on_assistant_message_copy_requested(self, event: AssistantMessage.CopyRequested) -> None:
        try:
            self.copy_to_clipboard(event.raw_text)
        except Exception:
            pass
        self._toast(t(self.lang, "copied_notice", chars=len(event.raw_text)))

    async def on_assistant_message_copy_code_requested(self, event: AssistantMessage.CopyCodeRequested) -> None:
        try:
            self.copy_to_clipboard(event.raw_code)
        except Exception:
            pass
        self._toast(t(self.lang, "copied_code_notice", chars=len(event.raw_code)))

    def on_tool_card_fullscreen_diff_requested(self, event: ToolCard.FullscreenDiffRequested) -> None:
        self.push_screen(DiffPreviewModal(event.title, event.diff_text, lang=self.lang))

    def on_shell_card_attach_requested(self, event: ShellCard.AttachRequested) -> None:
        bounded = event.output.strip()
        if len(bounded) > 6000:
            bounded = bounded[:6000] + "\n[… truncated]"
        block = f"[Shell output: `$ {event.command}` (exit {event.exit_code})]\n```\n{bounded}\n```"
        ta = self.query_one("#prompt-input", PromptTextArea)
        ta.value = (ta.text.rstrip() + "\n\n" + block).strip() if ta.text.strip() else block
        ta.focus()
        self._toast(t(self.lang, "shell_attached", chars=len(bounded)))

    # ------------------------------------------------------------------
    # Working spinner & language
    # ------------------------------------------------------------------

    def _set_working(self, active: bool, label: str = "") -> None:
        bar = self.query_one("#activity-bar", Static)
        if active:
            if not self._working:
                self._working_since = time.monotonic()
            self._working = True
            if label:
                self._working_label = label
            elapsed = max(0.0, time.monotonic() - self._working_since)
            frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
            bar.update(f"{frame} {self._working_label} ({elapsed:.1f}s)")
            bar.add_class("visible")
        else:
            self._working = False
            self._working_label = ""
            bar.update("")
            bar.remove_class("visible")

    def _tick_spinner(self) -> None:
        if not self._working or not self.is_running:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)
        elapsed = max(0.0, time.monotonic() - self._working_since)
        frame = SPINNER_FRAMES[self._spinner_idx]
        try:
            self.query_one("#activity-bar", Static).update(f"{frame} {self._working_label} ({elapsed:.1f}s)")
        except Exception:
            self._working = False

    def _apply_language(self, new_lang: str) -> None:
        self.lang = new_lang if new_lang in SUPPORTED_LANGS else "fr"
        self.query_one("#prompt-composer", PromptComposer).apply_lang(self.lang)
        self.query_one("#proposal-apply-btn", Button).label = t(self.lang, "apply")
        self.query_one("#new-messages-pill", Button).label = t(self.lang, "new_messages_pill")
        self.query_one("#queue-clear-btn", Button).label = t(self.lang, "queue_clear_btn")
        self.query_one("#transcript-search-input", Input).placeholder = t(self.lang, "transcript_search_placeholder")
        self.query_one("#telemetry-panel", TelemetryPanel).apply_lang(self.lang)
        for tr in self._transcripts.values():
            tr.update_welcome_meta(lang=self.lang)
        self._refresh_ui()

    # ------------------------------------------------------------------
    # ACP Boot & Routing
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _boot_acp(self) -> None:
        try:
            await self.client.connect(agent_conn=self._injected_agent_conn)
            target_resume: str | None = getattr(self, "resume_session_id", None)
            if not target_resume and getattr(self, "continue_last", False):
                try:
                    sessions = await self.client.list_sessions()
                except Exception:
                    sessions = []
                if sessions:
                    target_resume = str(sessions[-1].get("session_id") or sessions[-1].get("sessionId") or "") or None
                if not target_resume:
                    db_meta = await asyncio.to_thread(load_sessions_meta_readonly, self.ogy_config.db_path, 1)
                    if db_meta:
                        target_resume = next(iter(db_meta.keys()))

            if target_resume:
                await self._resume_session_by_id(target_resume)
                sid = target_resume
            else:
                sid = await self.client.new_session(self.client_config.cwd)
                self._ensure_state(sid, activate=True)
                await self._ensure_transcript(sid, activate=True)
                await self._apply_initial_profile(sid)
                self._refresh_ui()

            if self.initial_prompt:
                await self._dispatch_prompt(self.initial_prompt, session_id=sid)
        except AcpAuthError as exc:
            await self._append_notice(t(self.lang, "auth_required", err=exc), level="err")
        except Exception as exc:
            await self._append_notice(t(self.lang, "acp_start_failed", err=exc), level="err")

    def _ensure_state(self, session_id: str, *, activate: bool = False) -> SessionTelemetryState:
        cfg = self.client.session_configs.get(session_id, {})
        if activate:
            prev_sid = self.registry.active_session_id
            state = self.registry.set_active(session_id, config_options=cfg)
            self._switch_session_draft(prev_sid, session_id)
            return state
        return self.registry.ensure(session_id, config_options=cfg)

    @property
    def sessions(self) -> dict[str, SessionTelemetryState]:
        return self.registry.sessions

    @property
    def current_state(self) -> SessionTelemetryState | None:
        sid = self.client.active_session_id or self.registry.active_session_id
        if not sid:
            return None
        return self._ensure_state(sid)

    async def _set_model_and_effort(self, session_id: str, model_id: str, effort: str) -> tuple[str, str]:
        available = self.client.get_config_choices("model", session_id)
        resolved_model = _resolve_acp_model_slug(available, model_id, effort)
        cfg = self.client.session_configs.get(session_id, {})
        state = self._ensure_state(session_id)

        if state.model_id != resolved_model:
            try:
                await self.client.set_config_option("model", resolved_model, session_id=session_id)
            except Exception:
                pass

        opt_id = "thought_level" if ("thought_level" in cfg and "thinking_level" not in cfg) else "thinking_level"
        if state.effort != effort:
            try:
                await self.client.set_config_option(opt_id, effort, session_id=session_id)
            except Exception:
                pass
        return state.model_id, state.effort

    async def _apply_initial_profile(self, session_id: str) -> None:
        if self.initial_model or self.initial_effort:
            self.registry.mark_manual_override(session_id)
            await self._set_model_and_effort(
                session_id,
                self.initial_model or DEFAULT_FRUGAL_MODEL,
                self.initial_effort or DEFAULT_FRUGAL_EFFORT,
            )
            return
        await self._set_model_and_effort(session_id, DEFAULT_FRUGAL_MODEL, DEFAULT_FRUGAL_EFFORT)

    async def _maybe_auto_route_first_prompt(self, session_id: str, prompt: str) -> None:
        state = self._ensure_state(session_id)
        if (
            session_id in self.registry.routed_sessions
            or session_id in self.registry.manual_override_sessions
            or state.turn_count > 0
            or state.context_tokens > 0
        ):
            return

        self.registry.mark_routed(session_id)
        decision = route_stage1(
            prompt,
            has_test_runner=_detect_has_test_runner(self.client_config.cwd),
            remaining_quota_fraction=self.registry.min_quota_fraction(),
        )
        if self.orchestrator.check_model_compatibility(decision.model_id) is not None:
            return

        prev_model, prev_effort = state.model_id, state.effort
        applied_model, applied_effort = await self._set_model_and_effort(
            session_id, decision.model_id, decision.effort
        )
        self._refresh_ui()

        if applied_model != prev_model or applied_effort != prev_effort:
            await self._append_notice(
                t(self.lang, "auto_routed", category=decision.category, model=applied_model, effort=applied_effort),
                session_id=session_id,
            )

        self._persist_routing_decision_worker(
            session_id, decision.stage, decision.category, dict(decision.features), applied_model, applied_effort, state.mode
        )

    # ------------------------------------------------------------------
    # Dynamic config choices & slash completions
    # ------------------------------------------------------------------

    def get_available_models(self) -> list[str]:
        advertised = self.client.get_config_choices("model")
        return advertised if advertised else list(DEFAULT_MODELS)

    def get_available_efforts(self) -> list[str]:
        advertised = self.client.get_config_choices("thinking_level")
        return advertised if advertised else list(DEFAULT_EFFORTS)

    def get_available_modes(self) -> list[str]:
        advertised = self.client.get_config_choices("mode")
        if advertised:
            return [m for m in advertised if m.lower() != "yolo"]
        return list(DEFAULT_MODES)

    def get_slash_completions(self, raw_value: str) -> list[tuple[str, str]]:
        if not raw_value.startswith("/"):
            return []
        if " " not in raw_value:
            prefix = raw_value.lower()
            items: list[tuple[str, str]] = [
                (f"{s.name} " if s.takes_arg else s.name, f"{s.usage:<20}  {t(self.lang, s.desc_key)}")
                for s in SLASH_COMMANDS
                if s.name.startswith(prefix)
            ]
            local_names = {s.name for s in SLASH_COMMANDS}
            for acmd in self.client.get_available_commands():
                cname = f"/{acmd['name']}"
                if cname in local_names or not cname.lower().startswith(prefix):
                    continue
                takes_arg = bool(acmd.get("input_hint"))
                usage = f"{cname} <{acmd['input_hint']}>" if takes_arg else cname
                desc = acmd.get("description") or "ACP agent command"
                items.append((f"{cname} " if takes_arg else cname, f"{usage:<20}  [agy] {desc}"))
            return items
        cmd_part, arg_part = raw_value.split(" ", 1)
        cmd = cmd_part.lower()
        arg_prefix = arg_part.strip().lower()
        if cmd == "/model":
            return [(f"/model {m}", f"/model {m}") for m in self.get_available_models() if m.lower().startswith(arg_prefix)]
        if cmd == "/effort":
            return [(f"/effort {e}", f"/effort {e}") for e in self.get_available_efforts() if e.lower().startswith(arg_prefix)]
        if cmd == "/mode":
            return [(f"/mode {md}", f"/mode {md}") for md in self.get_available_modes() if md.lower().startswith(arg_prefix)]
        if cmd == "/select":
            return [
                (f"/select {k}", f"/select {k}")
                for k in ("model", "effort", "mode", "theme")
                if k.startswith(arg_prefix)
            ]
        if cmd == "/theme":
            return [
                (f"/theme {th}", f"/theme {th}")
                for th in SUPPORTED_THEMES
                if th.startswith(arg_prefix)
            ]
        if cmd == "/copy":
            return [
                (f"/copy {opt}", f"/copy {opt}")
                for opt in ("code", "last")
                if opt.startswith(arg_prefix)
            ]
        if cmd == "/lang":
            return [
                (f"/lang {code}", f"/lang {code}  ({lbl})")
                for code, lbl in (("fr", "Français"), ("en", "English"))
                if code.startswith(arg_prefix)
            ]
        if cmd == "/attach":
            try:
                entries = self.query_one("#prompt-composer", PromptComposer)._file_index
            except Exception:
                entries = []
            img_entries = [
                e for e in entries
                if Path(e.rel_path).suffix.lower() in _IMAGE_MIME_TYPES
                and (not arg_prefix or arg_prefix in e.rel_path.lower())
            ][:10]
            return [
                (f"/attach {e.rel_path}", f"/attach {e.rel_path}  ({_format_size(e.size_bytes, self.lang)})")
                for e in img_entries
            ]
        if cmd == "/config":
            from ogy_cli.config_cmd import CONFIG_SCHEMA

            return [
                (f"/config {k} ", f"/config {k}")
                for k in sorted(CONFIG_SCHEMA.keys())
                if k.lower().startswith(arg_prefix)
            ][:12]
        if cmd == "/resume":
            return [
                (f"/resume {sid}", f"/resume {sid[:12]} ({st.model_id})")
                for sid, st in self.registry.sessions.items()
                if sid.lower().startswith(arg_prefix)
            ]
        return []

    # ------------------------------------------------------------------
    # ACP Event Handlers (strictly isolated per event.session_id)
    # ------------------------------------------------------------------

    async def _on_acp_event(self, event: AcpEvent) -> None:
        replaying = getattr(event, "session_id", None) in self._replaying_sessions
        if isinstance(event, UserMessageEvent):
            tr = await self._ensure_transcript(event.session_id)
            await tr.append_user_chunk(event.text, message_id=event.message_id)
        elif isinstance(event, AgentMessageEvent):
            if not replaying:
                self._set_working(True, t(self.lang, "working_responding"))
            tr = await self._ensure_transcript(event.session_id)
            await tr.append_assistant_chunk(event.text, message_id=event.message_id)
        elif isinstance(event, AgentThoughtEvent):
            if not replaying:
                self._set_working(True, t(self.lang, "working_thinking"))
            tr = await self._ensure_transcript(event.session_id)
            await tr.append_thought_chunk(event.text)
        elif isinstance(event, ToolCallEvent):
            await self._handle_tool_call(event, replaying=replaying)
        elif isinstance(event, PlanUpdateEvent):
            tr = await self._ensure_transcript(event.session_id)
            await tr.update_plan(event.entries)
        elif isinstance(event, AvailableCommandsEvent):
            # Commands are already cached in self.client.session_commands; nothing blocking needed.
            pass
        elif isinstance(event, CurrentModeUpdateEvent):
            state = self._ensure_state(event.session_id)
            state.mode = event.mode_id
            self._refresh_ui()
        elif isinstance(event, UsageEvent):
            await self._handle_usage(event)
        elif isinstance(event, ConfigUpdateEvent):
            state = self._ensure_state(event.session_id)
            if "model" in event.config_options:
                state.model_id = event.config_options["model"]
            if "mode" in event.config_options:
                state.mode = event.config_options["mode"]
            if "thinking_level" in event.config_options:
                state.effort = event.config_options["thinking_level"]
            elif "thought_level" in event.config_options:
                state.effort = event.config_options["thought_level"]
            self._refresh_ui()
        elif isinstance(event, PermissionRequestEvent):
            self.push_screen(
                PermissionModal(event, lang=self.lang),
                callback=lambda choice: event.future.done() or event.future.set_result(choice),
            )
        elif isinstance(event, ElicitationRequestEvent):
            modal = ElicitationModal(event, lang=self.lang)
            if event.elicitation_id:
                self._active_elicitations[event.elicitation_id] = modal

            def _on_elicit_done(res: dict[str, Any] | None) -> None:
                if event.elicitation_id:
                    self._active_elicitations.pop(event.elicitation_id, None)
                if not event.future.done():
                    event.future.set_result(res or {"action": "cancel"})

            self.push_screen(modal, callback=_on_elicit_done)
        elif isinstance(event, ElicitationCompleteEvent):
            modal = self._active_elicitations.pop(event.elicitation_id, None)
            if modal is not None and modal.is_current:
                modal.complete_from_server()
        elif isinstance(event, SessionNoticeEvent):
            await self._append_notice(event.message, session_id=event.session_id)

    async def _handle_tool_call(self, event: ToolCallEvent, *, replaying: bool = False) -> None:
        state = self._ensure_state(event.session_id)
        if not replaying:
            if event.status in ("completed", "failed", "error"):
                if event.is_error:
                    state.consecutive_failures += 1
                    state.last_error_summary = f"{event.name}: {str(event.raw_output or '')[:160]}"
                else:
                    state.consecutive_failures = 0
                if self._working:
                    self._set_working(True, t(self.lang, "working_thinking"))
            else:
                self._set_working(True, t(self.lang, "working_tool", name=event.name))

        if isinstance(event.raw_input, dict):
            path_val = event.raw_input.get("TargetFile") or event.raw_input.get("path")
            if isinstance(path_val, str) and path_val not in state.modified_files:
                state.modified_files.append(path_val)

        tr = await self._ensure_transcript(event.session_id)
        await tr.handle_tool_call(event)
        if not replaying:
            self._refresh_ui()

    async def _handle_usage(self, event: UsageEvent) -> None:
        state = self._ensure_state(event.session_id)
        if event.context_used is not None:
            state.context_tokens = event.context_used
        if event.context_max is not None:
            state.context_max = event.context_max
        if event.is_turn_final:
            state.turn_count += 1
            state.last_turn_monotonic = time.monotonic()
            state.input_tokens += event.input_tokens
            state.output_tokens += event.output_tokens
            state.thought_tokens += event.thought_tokens
            state.cached_read_tokens += event.cached_read_tokens
            state.cached_write_tokens += event.cached_write_tokens
            if event.cost_usd is not None:
                state.total_cost_usd += event.cost_usd
            tr = self._transcripts.get(event.session_id)
            if tr is not None:
                await tr.close_active_message()
                tr.close_active_thought()
            self._set_working(False)
            self.query_one("#prompt-composer", PromptComposer).refresh_file_index()
        elif event.cost_usd is not None:
            state.total_cost_usd = max(state.total_cost_usd, event.cost_usd)
        self._refresh_ui()

    async def _append_notice(self, message: str, *, level: str = "info", session_id: str | None = None) -> None:
        sid = session_id or self.client.active_session_id or self.registry.active_session_id or "default"
        tr = await self._ensure_transcript(sid, activate=(sid == self.registry.active_session_id or not self._transcripts))
        await tr.append_notice(message, level=level)

    def _refresh_queue_bar(self) -> None:
        try:
            qbar = self.query_one("#queue-bar", Horizontal)
            qtext = self.query_one("#queue-text", Static)
        except Exception:
            return
        sid = self.registry.active_session_id or self.client.active_session_id
        count = self.registry.queue_len(sid)
        if count > 0 and sid:
            nxt = self.registry.peek_prompt(sid)
            preview = (nxt.display_text if nxt else "").replace("\n", " ")[:64]
            qtext.update(t(self.lang, "queue_status", n=count, preview=preview))
            qbar.add_class("visible")
        else:
            qtext.update("")
            qbar.remove_class("visible")

    async def _clear_active_queue(self) -> None:
        sid = self.registry.active_session_id or self.client.active_session_id
        removed = self.registry.clear_queue(sid)
        self._refresh_queue_bar()
        if removed:
            self._toast(t(self.lang, "queue_cleared", n=removed))

    def _build_session_bar_renderable(self, state: SessionTelemetryState, *, sidebar_hidden: bool) -> Text:
        health = state.assess_health()
        zone_dot, zone_style = {
            "green": ("●", "bold #3fb950"),
            "amber": ("▲", "bold #d29922"),
            "red": ("■", "bold #f85149"),
        }.get(health.zone, ("●", "bold #3fb950"))

        mode_style = {
            "plan": "bold #58a6ff",
            "accept-edits": "bold #d29922",
            "auto": "bold #3fb950",
        }.get(state.mode.lower(), "bold #c9d1d9")

        out = Text()
        out.append(f"{zone_dot} ", style=zone_style)
        base_str = t(
            self.lang,
            "session_bar",
            sid=state.session_id[:12],
            count=len(self.registry.sessions),
            model=state.model_id,
            effort=state.effort,
            mode=state.mode,
        )
        # Highlight the mode value with semantic color while preserving exact text
        mode_marker = f"Mode : {state.mode}" if self.lang == "fr" else f"Mode: {state.mode}"
        if mode_marker in base_str:
            before, after = base_str.split(mode_marker, 1)
            prefix_label = "Mode : " if self.lang == "fr" else "Mode: "
            out.append(before)
            out.append(prefix_label)
            out.append(state.mode, style=mode_style)
            out.append(after)
        else:
            out.append(base_str)

        if self._git_branch:
            out.append(f"  ·  ⎇ {self._git_branch}", style="#6ea8fe")
        if self.ogy_config.profile_mode == "isolated":
            out.append("  ·  [isolated]", style="bold #3fb950")
        if sidebar_hidden:
            ctx_pct = int(min(100.0, (state.context_tokens / max(1, state.context_max)) * 100.0))
            cache_str = f"{health.cache_hit_pct:.0f}%" if health.cache_hit_pct is not None else "--"
            out.append(
                f"  ·  Ctx {ctx_pct}% · Cache {cache_str} · ${state.total_cost_usd:.3f}",
                style="#8b949e",
            )
        return out

    def _refresh_ui(self) -> None:
        state = self.current_state
        bar = self.query_one("#session-bar", Static)
        prop_bar = self.query_one("#proposal-bar", Horizontal)
        prop_text = self.query_one("#proposal-text", Static)
        if state is None:
            bar.update(t(self.lang, "session_none"))
            prop_bar.remove_class("visible")
            self._refresh_queue_bar()
            return

        tel_panel = self.query_one("#telemetry-panel", TelemetryPanel)
        sidebar_hidden = tel_panel.has_class("hidden")
        bar.update(self._build_session_bar_renderable(state, sidebar_hidden=sidebar_hidden))

        snap = self.registry.latest_snapshot
        cap = self.ogy_config.budget_hard_cap_usd
        tel_panel.refresh_state(
            state,
            snap.quotas,
            snap.subagent_runs,
            snap.subagent_summary,
            budget_hard_cap_usd=cap,
            enterprise_cache=self.ogy_config.enterprise_cache_pricing,
        )
        if cap is not None and cap > 0 and state.total_cost_usd >= cap:
            warned: set[str] = getattr(self, "_budget_warned_sessions", set())
            if state.session_id not in warned:
                warned.add(state.session_id)
                self._budget_warned_sessions = warned
                self._toast(
                    t(self.lang, "budget_cap_warning", cost=state.total_cost_usd, cap=cap),
                    severity="warning",
                    timeout=5.0,
                )
        self._refresh_queue_bar()

        proposals = self.orchestrator.evaluate_proposals(state)
        if self._pending_v1_proposal is not None:
            proposals = [self._pending_v1_proposal] + proposals
        self.active_proposals = proposals
        if self.active_proposals:
            top = self.active_proposals[0]
            prop_text.update(f"{top.title} — {top.detail}")
            prop_bar.add_class("visible")
        else:
            prop_bar.remove_class("visible")

    # ------------------------------------------------------------------
    # Composer submission, Prompt Queueing & Slash Commands
    # ------------------------------------------------------------------

    async def on_prompt_composer_prompt_submitted(self, event: PromptComposer.PromptSubmitted) -> None:
        text = event.text.strip()
        if not text:
            return
        if text.startswith("!"):
            task = asyncio.create_task(self._run_inline_shell(text))
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)
            return
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return
        composer = self.query_one("#prompt-composer", PromptComposer)
        sid = self.client.active_session_id or self.registry.active_session_id
        composer.persist_prompt(event.display_text, session_id=sid)

        if self._working and sid:
            q_len = self.registry.enqueue_prompt(
                sid,
                text,
                display_text=event.display_text,
                extra_blocks=event.extra_blocks or None,
            )
            self._refresh_queue_bar()
            self._toast(t(self.lang, "queue_added", n=q_len))
            return

        task = asyncio.create_task(
            self._dispatch_prompt(text, display_text=event.display_text, extra_blocks=event.extra_blocks or None)
        )
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    async def _dispatch_prompt(
        self,
        text: str,
        *,
        display_text: str | None = None,
        extra_blocks: list[Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        sid = session_id or self.client.active_session_id or self.registry.active_session_id
        self._turn_cancelled_flag = False
        if sid:
            tr = await self._ensure_transcript(sid, activate=True)
            await tr.append_user_message(display_text or text)

        state = self._ensure_state(sid) if sid else self.current_state
        if state is not None and (display_text is None or not display_text.startswith("/btw")):
            state.active_goal = (display_text or text)[:200]
            await self._maybe_auto_route_first_prompt(state.session_id, text)

        self._set_working(True, t(self.lang, "working_sending"))
        try:
            await self.client.send_prompt(text, session_id=sid, extra_blocks=extra_blocks)
        except Exception as exc:
            await self._append_notice(t(self.lang, "prompt_error", err=exc), level="err", session_id=sid)
        finally:
            self._set_working(False)
            if sid and not self._turn_cancelled_flag:
                nxt = self.registry.pop_prompt(sid)
                self._refresh_queue_bar()
                if nxt is not None:
                    q_task = asyncio.create_task(
                        self._dispatch_prompt(
                            nxt.text,
                            display_text=nxt.display_text,
                            extra_blocks=list(nxt.extra_blocks) or None,
                            session_id=sid,
                        )
                    )
                    self._inflight_tasks.add(q_task)
                    q_task.add_done_callback(self._inflight_tasks.discard)

    def _run_palette_cmd(self, cmd_line: str) -> None:
        task = asyncio.create_task(self._handle_slash_command(cmd_line))
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    async def _handle_slash_command(self, cmd_line: str) -> None:
        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        state = self.current_state

        if cmd == "/help":
            lines = "\n".join(f"  {s.usage:<20} {t(self.lang, s.desc_key)}" for s in SLASH_COMMANDS)
            await self._append_notice(f"{t(self.lang, 'help_header')}\n{lines}")
            return
        if cmd == "/glossary":
            self.action_show_glossary()
            return
        if cmd == "/find":
            self.action_open_transcript_search()
            return
        if cmd == "/clear-queue":
            await self._clear_active_queue()
            return
        if cmd == "/theme":
            if not arg or arg.lower() not in SUPPORTED_THEMES:
                await self._append_notice(t(self.lang, "theme_status", cur=self.tui_theme))
                return
            self._apply_theme(arg.lower(), notify=True)
            return
        if cmd == "/copy":
            tr = self.get_active_transcript()
            if tr is None:
                self._toast(t(self.lang, "copy_empty"), severity="warning")
                return
            if arg.lower() == "code":
                code = tr.get_last_code_block()
                if not code:
                    self._toast(t(self.lang, "copy_empty"), severity="warning")
                    return
                try:
                    self.copy_to_clipboard(code)
                except Exception:
                    pass
                self._toast(t(self.lang, "copied_code_notice", chars=len(code)))
                return
            last_msg = tr.get_last_assistant_message()
            if last_msg is None:
                self._toast(t(self.lang, "copy_empty"), severity="warning")
                return
            try:
                self.copy_to_clipboard(last_msg.raw_markdown)
            except Exception:
                pass
            self._toast(t(self.lang, "copied_notice", chars=len(last_msg.raw_markdown)))
            return
        if cmd == "/export":
            await self._export_active_session(arg)
            return
        if cmd == "/status":
            if state is None:
                await self._append_notice(t(self.lang, "session_none"))
                return
            health = state.assess_health()
            ctx_pct = int(min(100.0, (state.context_tokens / max(1, state.context_max)) * 100.0))
            cache_str = f"{health.cache_hit_pct:.1f}%" if health.cache_hit_pct is not None else "--"
            sub = self.registry.latest_snapshot.subagent_summary or {}
            await self._append_notice(
                t(
                    self.lang,
                    "status_summary",
                    sid=state.session_id[:12],
                    profile=self.ogy_config.profile_mode,
                    model=state.model_id,
                    effort=state.effort,
                    mode=state.mode,
                    ctx=f"{_format_tokens(state.context_tokens)}/{_format_tokens(state.context_max)}",
                    pct=ctx_pct,
                    cache=cache_str,
                    cost=state.total_cost_usd,
                    api=float(sub.get("cost_usd") or 0.0),
                    runs=int(sub.get("runs") or 0),
                )
            )
            return
        if cmd == "/lang":
            if not arg or arg.lower() not in SUPPORTED_LANGS:
                await self._append_notice(t(self.lang, "lang_status", cur=self.lang))
                return
            self._apply_language(arg.lower())
            await self._append_notice(t(self.lang, "lang_changed", val=arg.lower()))
            return
        if cmd == "/delegate":
            await self.action_open_delegation_modal(arg)
            return
        if cmd == "/btw":
            if not arg:
                await self._append_notice(t(self.lang, "btw_usage"))
                return
            wrapped = (
                "[Side question (/btw) — answer concisely in <= 5 lines without modifying any files "
                f"or changing the active task]: {arg}"
            )
            task = asyncio.create_task(self._dispatch_prompt(wrapped, display_text=f"/btw {arg}"))
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)
            return
        if cmd == "/model":
            if not arg:
                cur = state.model_id if state else "--"
                await self._append_notice(t(self.lang, "model_status", cur=cur, avail=", ".join(self.get_available_models())))
                return
            compat = self.orchestrator.check_model_compatibility(arg)
            if compat is not None:
                self._pending_v1_proposal = compat
                self._refresh_ui()
                await self._append_notice(f"{compat.title} : {compat.detail}", level="warn")
                return
            self._pending_v1_proposal = None
            await self.client.set_config_option("model", arg)
            if self.client.active_session_id:
                self.registry.mark_manual_override(self.client.active_session_id)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "model_changed", val=arg))
            return
        if cmd == "/effort":
            if not arg:
                cur = state.effort if state else "--"
                await self._append_notice(t(self.lang, "effort_status", cur=cur, avail=", ".join(self.get_available_efforts())))
                return
            sid = self.client.active_session_id
            cfg = self.client.session_configs.get(sid or "", {})
            opt_id = "thought_level" if ("thought_level" in cfg and "thinking_level" not in cfg) else "thinking_level"
            await self.client.set_config_option(opt_id, arg)
            if sid:
                self.registry.mark_manual_override(sid)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "effort_changed", val=arg))
            return
        if cmd == "/mode":
            if not arg:
                cur = state.mode if state else "--"
                await self._append_notice(t(self.lang, "mode_status", cur=cur, avail=", ".join(self.get_available_modes())))
                return
            try:
                await self.client.set_config_option("mode", arg)
                self._refresh_ui()
                await self._append_notice(t(self.lang, "mode_changed", val=arg))
            except ValueError as exc:
                await self._append_notice(str(exc), level="warn")
            return
        if cmd in ("/rotate", "/clear"):
            await self.action_open_rotation_review()
            return
        if cmd == "/handoff":
            if state:
                res = write_handoff(
                    goal=state.active_goal,
                    modified_files=state.modified_files,
                    checks=state.last_error_summary or "ok",
                    negative_constraints=[],
                    next_step="Resume from handoff",
                    workspace_path=self.client_config.cwd,
                )
                await self._append_notice(t(self.lang, "handoff_written", path=res["path"], lines=res["lines"]))
            return
        if cmd == "/new":
            await self.action_new_session()
            return
        if cmd == "/sessions":
            await self.action_open_session_picker()
            return
        if cmd == "/resume":
            if not arg:
                await self.action_open_session_picker()
                return
            await self._resume_session_by_id(arg)
            return
        if cmd == "/fork":
            await self.action_fork_session()
            return
        if cmd == "/compact":
            await self.action_open_rotation_review()
            return
        if cmd == "/diff":
            await self._open_git_diff()
            return
        if cmd == "/attach":
            await self._attach_image_path(arg)
            return
        if cmd == "/context":
            await self._open_context_inspector()
            return
        if cmd == "/mcp":
            await self._show_mcp_servers()
            return
        if cmd == "/config":
            await self._handle_config_command(arg)
            return
        if cmd == "/select":
            kind = arg.lower() if arg.lower() in ("model", "effort", "mode", "theme") else "model"
            self.action_open_quick_select(kind)
            return

        # Forward ACP server-advertised slash commands (e.g. /review, /plan, skills) directly to agy
        agent_cmd_names = {f"/{c['name'].lower()}" for c in self.client.get_available_commands()}
        if cmd in agent_cmd_names:
            task = asyncio.create_task(self._dispatch_prompt(cmd_line.strip(), display_text=cmd_line.strip()))
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)
            return

        await self._append_notice(t(self.lang, "cmd_unknown", cmd=rich_escape(cmd_line)), level="warn")

    async def _run_inline_shell(self, raw_text: str) -> None:
        """Execute `!cmd` or `!w cmd` (ogy wrap) locally without blocking the Textual UI thread."""
        body = raw_text[1:].strip()
        if not body:
            return
        wrapped = False
        if body.startswith("w "):
            wrapped = True
            body = body[2:].strip()
        if not body:
            return

        sid = self.registry.active_session_id or self.client.active_session_id or "default"
        tr = await self._ensure_transcript(sid, activate=True)
        card = await tr.mount_shell_card(body, wrapped=wrapped)
        t0 = time.monotonic()

        try:
            if wrapped:
                import shlex
                import sys
                from ogy_core.firebreaks import run_wrapped_command

                tokens = shlex.split(body)
                # Run ogy wrap in a subprocess so stdout/stderr are cleanly captured
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "ogy_cli.main",
                    "wrap",
                    "--",
                    *tokens,
                    cwd=self.client_config.cwd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    body,
                    cwd=self.client_config.cwd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            try:
                out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
                rc = int(proc.returncode or 0)
                output = out_bytes.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                rc = 124
                output = t(self.lang, "shell_timeout", sec=30)
        except Exception as exc:
            rc = 1
            output = str(exc)

        elapsed = max(0.0, time.monotonic() - t0)
        card.set_result(exit_code=rc, output=output, elapsed_s=elapsed)

    async def _open_git_diff(self) -> None:
        status, diff_text = await asyncio.to_thread(_collect_git_diff, self.client_config.cwd)
        if status == "not_git":
            self._toast(t(self.lang, "diff_not_git"), severity="warning")
            return
        if status == "clean":
            self._toast(t(self.lang, "diff_clean"))
            return
        self.push_screen(DiffPreviewModal(t(self.lang, "diff_title"), diff_text, lang=self.lang))

    async def _attach_image_path(self, raw_path: str) -> None:
        if not raw_path.strip():
            await self._append_notice(t(self.lang, "attach_usage"))
            return
        p = Path(raw_path.strip()).expanduser()
        if not p.is_absolute():
            p = Path(self.client_config.cwd) / p
        if not p.is_file():
            self._toast(t(self.lang, "attach_not_found", path=raw_path), severity="warning")
            return
        ext = p.suffix.lower()
        mime = _IMAGE_MIME_TYPES.get(ext)
        if not mime:
            self._toast(t(self.lang, "attach_unsupported", ext=ext or "(none)"), severity="warning")
            return
        try:
            size = p.stat().st_size
        except OSError as exc:
            self._toast(str(exc), severity="error")
            return
        if size > _MAX_IMAGE_BYTES:
            self._toast(t(self.lang, "attach_too_large", size=_format_size(size, self.lang)), severity="warning")
            return
        data = await asyncio.to_thread(p.read_bytes)
        ta = self.query_one("#prompt-input", PromptTextArea)
        chip = ta.attach_image(data, mime_type=mime)
        ta.focus()
        self._toast(t(self.lang, "attach_done", chip=chip, size=_format_size(size, self.lang)))

    async def _open_context_inspector(self) -> None:
        state = self.current_state
        artifacts, mcp_servers = await asyncio.gather(
            asyncio.to_thread(_collect_context_artifacts, self.client_config.cwd),
            asyncio.to_thread(_collect_mcp_servers, self.ogy_config.agy_config_dir, self.client_config.cwd),
        )
        lines: list[str] = []
        if state is not None:
            health = state.assess_health()
            ctx_pct = min(100.0, (state.context_tokens / max(1, state.context_max)) * 100.0)
            cache_str = f"{health.cache_hit_pct:.1f}%" if health.cache_hit_pct is not None else "--"
            saved_usd = estimate_cache_savings_usd(state, enterprise=self.ogy_config.enterprise_cache_pricing)
            lines.extend(
                [
                    "## Session & Context Health",
                    f"- **Session**: `{state.session_id}` · **Profile**: `{self.ogy_config.profile_mode}`",
                    f"- **Model / Effort / Mode**: `{state.model_id}` / `{state.effort}` / `{state.mode}`",
                    f"- **Context Window**: `{_format_tokens(state.context_tokens)} / {_format_tokens(state.context_max)}` (`{ctx_pct:.1f}%` · zone `{health.zone}`)",
                    f"- **Prompt Cache Hit**: `{cache_str}` · **Est. Cache Saved**: `${saved_usd:.4f}`",
                    f"- **Est. Session Cost**: `${state.total_cost_usd:.4f}` · **Turns**: `{state.turn_count}`",
                    "",
                ]
            )
            if state.context_tokens > 0:
                lines.append("## Switch Tax Preview (if changing model now)")
                for m in self.get_available_models():
                    tax = self.orchestrator.compute_switch_tax(state, m)
                    cur_tag = " *(current)*" if m == state.model_id else ""
                    lines.append(f"- `{m}`{cur_tag}: `~${tax:.4f}`")
                lines.append("")

        lines.append("## Workspace Rules, Memory & Skills")
        mem = artifacts.get("memory") or []
        rules = artifacts.get("rules") or []
        skills = artifacts.get("skills") or []
        if mem:
            lines.append("- **Memory files**: " + ", ".join(f"`{n}` ({_format_size(sz, self.lang)})" for n, sz in mem))
        else:
            lines.append("- **Memory files**: *(none detected)*")
        if rules:
            lines.append("- **Rules (`.agents/rules`)**: " + ", ".join(f"`{n}` ({_format_size(sz, self.lang)})" for n, sz in rules))
        if skills:
            lines.append("- **Skills (`.agents/skills`)**: " + ", ".join(f"`{n}`" for n, _ in skills))
        lines.append("")

        lines.extend(
            [
                "## Optigravity Firebreaks & MCP",
                f"- **File / Doc / Data limits**: `{_format_size(self.ogy_config.firebreak_file_max_bytes, self.lang)}` / `{_format_size(self.ogy_config.firebreak_doc_max_bytes, self.lang)}` / `{_format_size(self.ogy_config.firebreak_data_max_bytes, self.lang)}`",
                f"- **Loop threshold**: `{self.ogy_config.firebreak_loop_threshold}` · **Subagents privacy**: `{self.ogy_config.subagents_privacy_mode}`",
                f"- **Configured MCP servers**: `{len(mcp_servers)}`",
            ]
        )
        for srv in mcp_servers[:10]:
            mode_tag = "eager" if srv["eager"] else "lazy"
            lines.append(f"  - `{srv['name']}` ({srv['scope']}, {mode_tag}) → `{srv['command']}`")

        self.push_screen(ContextInspectorModal("\n".join(lines), lang=self.lang))

    async def _show_mcp_servers(self) -> None:
        servers = await asyncio.to_thread(_collect_mcp_servers, self.ogy_config.agy_config_dir, self.client_config.cwd)
        if not servers:
            await self._append_notice(t(self.lang, "mcp_empty"))
            return
        lines = [t(self.lang, "mcp_header")]
        for srv in servers:
            tag = "eager" if srv["eager"] else "lazy"
            lines.append(f"  - {srv['name']} [{srv['scope']} · {tag}] → {srv['command']}")
        await self._append_notice("\n".join(lines))

    async def _handle_config_command(self, arg: str) -> None:
        from ogy_cli.config_cmd import set_config_value

        parts = arg.strip().split(maxsplit=1) if arg.strip() else []
        if not parts:
            summary = (
                f"config_path={self.ogy_config.config_path}\n"
                f"  general.profile_mode = {self.ogy_config.profile_mode}\n"
                f"  general.language = {self.ogy_config.language}\n"
                f"  tui.theme = {self.tui_theme}\n"
                f"  tui.enter_behavior = {self.ogy_config.tui_enter_behavior}\n"
                f"  budget.hard_cap_usd = {self.ogy_config.budget_hard_cap_usd}\n"
                f"  subagents.privacy_mode = {self.ogy_config.subagents_privacy_mode}"
            )
            await self._append_notice(summary)
            return
        if len(parts) < 2:
            await self._append_notice("Usage: /config <section.key> <value>")
            return
        key, val = parts[0], parts[1]
        try:
            parsed = await asyncio.to_thread(set_config_value, key, val, config_path=self.ogy_config.config_path)
            if key == "tui.theme":
                self._apply_theme(str(parsed), notify=False)
            elif key == "general.language":
                self._apply_language(str(parsed))
            elif key == "budget.hard_cap_usd":
                self.ogy_config.budget_hard_cap_usd = float(parsed) if parsed else None
                self._refresh_ui()
            self._toast(t(self.lang, "config_updated", key=key, val=parsed))
        except Exception as exc:
            self._toast(t(self.lang, "config_failed", err=exc), severity="error")

    async def _export_active_session(self, custom_path: str = "") -> None:
        tr = self.get_active_transcript()
        state = self.current_state
        if tr is None:
            self._toast(t(self.lang, "export_empty"), severity="warning")
            return
        meta: dict[str, Any] = {}
        if state is not None:
            health = state.assess_health()
            meta = {
                "Model": state.model_id,
                "Effort": state.effort,
                "Mode": state.mode,
                "Turns": state.turn_count,
                "Cache Hit": f"{health.cache_hit_pct:.1f}%" if health.cache_hit_pct is not None else "--",
                "Est. Cost": f"${state.total_cost_usd:.4f}",
                "Profile": self.ogy_config.profile_mode,
            }
        md = tr.export_markdown(meta)
        if not md:
            self._toast(t(self.lang, "export_empty"), severity="warning")
            return
        if custom_path:
            out_path = Path(custom_path).expanduser()
            if not out_path.is_absolute():
                out_path = Path(self.client_config.cwd) / out_path
        else:
            safe_sid = _safe_widget_id(tr.session_id)[:32] or "session"
            out_path = Path(self.client_config.cwd) / ".ogy" / "exports" / f"{safe_sid}.md"
        try:
            await asyncio.to_thread(self._write_export_file, out_path, md)
            self._toast(t(self.lang, "export_saved", path=str(out_path)))
        except Exception as exc:
            self._toast(t(self.lang, "export_failed", err=exc), severity="error")

    @staticmethod
    def _write_export_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # In-Transcript Search (`Ctrl+F` / `/find`)
    # ------------------------------------------------------------------

    def action_open_transcript_search(self) -> None:
        bar = self.query_one("#transcript-search-bar", Horizontal)
        inp = self.query_one("#transcript-search-input", Input)
        bar.add_class("visible")
        inp.focus()
        if inp.value.strip():
            self._run_transcript_search(inp.value)

    def _close_transcript_search(self) -> None:
        bar = self.query_one("#transcript-search-bar", Horizontal)
        bar.remove_class("visible")
        self._search_matches = []
        self._search_idx = 0
        self.query_one("#transcript-search-count", Static).update("")
        tr = self.get_active_transcript()
        if tr is not None:
            tr.highlight_search_match(None)
        self.query_one("#prompt-input", PromptTextArea).focus()

    def _run_transcript_search(self, query: str) -> None:
        tr = self.get_active_transcript()
        count_lbl = self.query_one("#transcript-search-count", Static)
        if tr is None or not query.strip():
            self._search_matches = []
            self._search_idx = 0
            count_lbl.update("")
            if tr is not None:
                tr.highlight_search_match(None)
            return
        self._search_matches = tr.search_widgets(query)
        self._search_idx = 0
        if not self._search_matches:
            count_lbl.update(t(self.lang, "transcript_search_none"))
            tr.highlight_search_match(None)
            return
        count_lbl.update(t(self.lang, "transcript_search_count", cur=1, total=len(self._search_matches)))
        tr.highlight_search_match(self._search_matches[0])

    def _step_transcript_search(self, delta: int) -> None:
        if not self._search_matches:
            return
        tr = self.get_active_transcript()
        if tr is None:
            return
        self._search_idx = (self._search_idx + delta) % len(self._search_matches)
        self.query_one("#transcript-search-count", Static).update(
            t(self.lang, "transcript_search_count", cur=self._search_idx + 1, total=len(self._search_matches))
        )
        tr.highlight_search_match(self._search_matches[self._search_idx])

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "transcript-search-input":
            self._run_transcript_search(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "transcript-search-input":
            self._step_transcript_search(1)

    # ------------------------------------------------------------------
    # Actions & Contextual Proposal Hotkeys (Alt+R/U/D/V or unfocused r/u/d/v)
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        # Handle navigation inside transcript search bar
        try:
            sbar = self.query_one("#transcript-search-bar", Horizontal)
            sinp = self.query_one("#transcript-search-input", Input)
            if sbar.has_class("visible") and sinp.has_focus:
                if event.key == "escape":
                    self._close_transcript_search()
                    event.prevent_default()
                    event.stop()
                    return
                if event.key in ("up", "shift+enter"):
                    self._step_transcript_search(-1)
                    event.prevent_default()
                    event.stop()
                    return
                if event.key == "down":
                    self._step_transcript_search(1)
                    event.prevent_default()
                    event.stop()
                    return
        except Exception:
            pass

        # Contextual proposals: Alt+<key> works anywhere; bare <key> ONLY when composer is NOT focused
        if not self.active_proposals:
            return
        ta = self.query_one("#prompt-input", PromptTextArea)
        key = event.key.lower()
        for prop in self.active_proposals:
            hk = prop.hotkey.lower()
            is_alt = key == f"alt+{hk}"
            is_bare_unfocused = (key == hk) and (not ta.has_focus)
            if is_alt or is_bare_unfocused:
                event.prevent_default()
                event.stop()
                task = asyncio.create_task(self._apply_proposal(prop))
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)
                return

    def action_show_glossary(self) -> None:
        if isinstance(self.screen, HelpGlossaryModal):
            self.pop_screen()
        else:
            self.push_screen(HelpGlossaryModal(lang=self.lang))

    def action_toggle_telemetry(self) -> None:
        panel = self.query_one("#telemetry-panel", TelemetryPanel)
        now_visible = panel.has_class("hidden")
        self._sidebar_user_override = now_visible
        panel.set_class(not now_visible, "hidden")
        if self.current_state is not None:
            self._refresh_ui()

    def action_toggle_verbosity(self) -> None:
        self.verbosity = "high" if self.verbosity == "low" else "low"
        for tr in self._transcripts.values():
            tr.set_verbosity(self.verbosity)
        self._toast(t(self.lang, "verbosity_notice", mode=self.verbosity))

    def action_toggle_thoughts(self) -> None:
        tr = self.get_active_transcript()
        if tr is None:
            return
        expanded = tr.toggle_all_thoughts()
        state_lbl = t(self.lang, "thoughts_expanded" if expanded else "thoughts_collapsed")
        self._toast(t(self.lang, "thoughts_toggled", state=state_lbl))

    async def action_cycle_mode(self) -> None:
        """Cycle ACP execution mode (`default -> accept-edits -> auto -> plan`, never `yolo`)."""
        if isinstance(self.screen, ModalScreen):
            return
        state = self.current_state
        if state is None or not self.client.is_connected:
            return
        modes = [m for m in self.get_available_modes() if m.lower() != "yolo"]
        if not modes:
            return
        try:
            idx = modes.index(state.mode)
            next_mode = modes[(idx + 1) % len(modes)]
        except ValueError:
            next_mode = modes[0]
        try:
            await self.client.set_config_option("mode", next_mode)
            self._refresh_ui()
            self._toast(t(self.lang, "mode_cycled", val=next_mode))
        except Exception as exc:
            self._toast(str(exc), severity="warning")

    def action_open_quick_select(self, kind: str = "model") -> None:
        """Open keyboard-first QuickSelectModal (`Alt+M` / `/select [model|effort|mode|theme]`)."""
        if isinstance(self.screen, ModalScreen):
            return
        state = self.current_state
        options: list[tuple[str, str, str]] = []
        if kind == "model":
            title = t(self.lang, "quick_select_title", kind=t(self.lang, "quick_select_model"))
            for m in self.get_available_models():
                cur = "● " if (state and state.model_id == m) else "  "
                tax_str = ""
                if state and state.context_tokens > 0 and state.model_id != m:
                    tax = self.orchestrator.compute_switch_tax(state, m)
                    tax_str = f"  (Switch Tax ~${tax:.4f})"
                options.append(("model", m, f"{cur}{m}{tax_str}"))
        elif kind == "effort":
            title = t(self.lang, "quick_select_title", kind=t(self.lang, "quick_select_effort"))
            for e in self.get_available_efforts():
                cur = "● " if (state and state.effort == e) else "  "
                options.append(("effort", e, f"{cur}{e}"))
        elif kind == "mode":
            title = t(self.lang, "quick_select_title", kind=t(self.lang, "quick_select_mode"))
            for md in self.get_available_modes():
                cur = "● " if (state and state.mode == md) else "  "
                options.append(("mode", md, f"{cur}{md}"))
        else:
            title = t(self.lang, "quick_select_title", kind=t(self.lang, "quick_select_theme"))
            for th in SUPPORTED_THEMES:
                cur = "● " if self.tui_theme == th else "  "
                options.append(("theme", th, f"{cur}{th}"))

        def _on_selected(choice: tuple[str, str] | None) -> None:
            if not choice:
                return
            cat, val = choice
            cmd_map = {"model": "/model", "effort": "/effort", "mode": "/mode", "theme": "/theme"}
            cmd = cmd_map.get(cat)
            if cmd:
                self._run_palette_cmd(f"{cmd} {val}")

        self.push_screen(QuickSelectModal(title, options, lang=self.lang), callback=_on_selected)

    async def action_cancel_turn(self) -> None:
        self._turn_cancelled_flag = True
        await self.client.cancel_turn()
        self._set_working(False)
        await self._append_notice(t(self.lang, "turn_cancelled"), level="warn")

    async def action_new_session(self) -> None:
        if not self.client.is_connected:
            return
        sid = await self.client.new_session(self.client_config.cwd)
        self._ensure_state(sid, activate=True)
        await self._ensure_transcript(sid, activate=True)
        await self._apply_initial_profile(sid)
        self._refresh_ui()
        await self._append_notice(t(self.lang, "session_started", sid=sid[:12]), session_id=sid)

    async def action_fork_session(self, session_id: str | None = None) -> None:
        if not self.client.is_connected:
            return
        src_sid = session_id or self.client.active_session_id or self.registry.active_session_id
        if not src_sid:
            return
        if not self.client.supports_fork:
            self._toast(t(self.lang, "fork_unsupported"), severity="warning")
            return
        try:
            new_sid = await self.client.fork_session(src_sid, self.client_config.cwd)
        except Exception as exc:
            self._toast(t(self.lang, "fork_failed", err=exc), severity="error")
            return
        if not new_sid:
            self._toast(t(self.lang, "fork_unsupported"), severity="warning")
            return
        self._ensure_state(new_sid, activate=True)
        await self._ensure_transcript(new_sid, activate=True)
        self._refresh_ui()
        self._toast(t(self.lang, "fork_done", sid=new_sid[:12]))
        await self._append_notice(t(self.lang, "fork_done", sid=new_sid[:12]), session_id=new_sid)

    async def _close_session_by_id(self, session_id: str) -> None:
        if not self.client.is_connected:
            return
        active_sid = self.registry.active_session_id or self.client.active_session_id
        if session_id == active_sid:
            self._toast(t(self.lang, "session_close_active_warn"), severity="warning")
            return
        try:
            await self.client.close_session(session_id)
        except Exception:
            pass
        self.registry.sessions.pop(session_id, None)
        self.registry.prompt_queues.pop(session_id, None)
        self.registry.drafts.pop(session_id, None)
        tr = self._transcripts.pop(session_id, None)
        if tr is not None:
            try:
                await tr.remove()
            except Exception:
                pass
        self._refresh_ui()
        self._toast(t(self.lang, "session_closed", sid=session_id[:12]))

    async def _resume_session_by_id(self, session_id: str) -> None:
        if not self.client.is_connected:
            return
        existing = self._transcripts.get(session_id)
        if existing is not None and existing.has_conversation_content:
            await self.client.resume_session(session_id)
            self._ensure_state(session_id, activate=True)
            await self._ensure_transcript(session_id, activate=True)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "session_resumed", sid=session_id[:12]), session_id=session_id)
            return

        # Opening an existing session not yet loaded in memory: prepare transcript first,
        # replay via ACP `session/load` when supported, and fall back to local JSONL cache.
        self._ensure_state(session_id, activate=True)
        tr = await self._ensure_transcript(session_id, activate=True)
        self._replaying_sessions.add(session_id)
        try:
            if self.client.supports_load:
                try:
                    await self.client.load_session(session_id)
                except Exception:
                    await self.client.resume_session(session_id)
            else:
                await self.client.resume_session(session_id)

            await tr.close_active_message()
            tr.close_active_thought()
            tr.close_active_user_message()

            if not tr.has_conversation_content:
                cached = await asyncio.to_thread(load_local_transcript_entries, self.ogy_config.db_path, session_id)
                for entry in cached:
                    role = entry.get("role", "")
                    text = entry.get("text", "")
                    if not text:
                        continue
                    if role == "user":
                        await tr.append_user_message(text)
                    elif role == "thought":
                        await tr.append_thought_chunk(text)
                        tr.close_active_thought()
                    elif role == "assistant":
                        await tr.append_assistant_chunk(text)
                        await tr.close_active_message()
        finally:
            self._replaying_sessions.discard(session_id)

        tr.pin_to_bottom()
        self._refresh_ui()
        await self._append_notice(t(self.lang, "session_resumed", sid=session_id[:12]), session_id=session_id)

    async def action_open_session_picker(self) -> None:
        if not self.client.is_connected:
            return
        try:
            acp_sessions = await self.client.list_sessions()
        except Exception:
            acp_sessions = [{"session_id": sid} for sid in self.registry.sessions]
        # Include any in-memory sessions (e.g. rotated sessions kept consultable)
        known = {str(s.get("session_id") or "") for s in acp_sessions}
        for sid, st in self.registry.sessions.items():
            if sid not in known:
                acp_sessions.append({"session_id": sid, "model": st.model_id, "title": st.active_goal})
        db_meta = await asyncio.to_thread(load_sessions_meta_readonly, self.ogy_config.db_path)

        def _on_picked(result: Any) -> None:
            if not result:
                return
            if isinstance(result, dict):
                act = result.get("action")
                sid = str(result.get("session_id") or "")
                if not sid:
                    return
                if act == "fork":
                    coro = self.action_fork_session(sid)
                elif act == "close":
                    coro = self._close_session_by_id(sid)
                else:
                    coro = self._resume_session_by_id(sid)
            else:
                coro = self._resume_session_by_id(str(result))
            task = asyncio.create_task(coro)
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)

        self.push_screen(
            SessionPickerModal(
                acp_sessions,
                db_meta,
                active_session_id=self.registry.active_session_id,
                supports_fork=self.client.supports_fork,
                lang=self.lang,
            ),
            callback=_on_picked,
        )

    def _get_configured_subagents_service(self) -> Any | None:
        """Return injected or configured SubagentsService only if enabled and an API key is available."""
        if getattr(self, "subagents_service", None) is not None:
            return self.subagents_service
        if self.ogy_config.subagents_privacy_mode == "disabled":
            return None
        try:
            from ogy_subagents.secrets import resolve_api_key
            from ogy_subagents.server import SubagentsService

            tiers = ("tier1",) if self.ogy_config.subagents_privacy_mode == "tier1_only" else ("free", "tier1")
            if any(resolve_api_key(tier).key for tier in tiers):
                return SubagentsService(cfg=self.ogy_config)
        except Exception:
            return None
        return None

    async def _try_build_subagent_handoff(self, state: SessionTelemetryState) -> str | None:
        """Optionally use Level 1 `ogy_build_handoff` when subagents are configured (instant local fallback otherwise)."""
        svc = self._get_configured_subagents_service()
        if svc is None:
            return None
        git_short = ""
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "status", "--short"],
                cwd=self.client_config.cwd,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0:
                git_short = proc.stdout.strip()
        except Exception:
            git_short = ""
        recent_ctx = (
            f"Model={state.model_id}, turns={state.turn_count}, context_tokens={state.context_tokens}, "
            f"modified_files={', '.join(state.modified_files[:20])}"
        )
        try:
            text = await asyncio.wait_for(
                svc.build_handoff(
                    state.active_goal or "Continue active task",
                    git_status=git_short,
                    last_error=state.last_error_summary or "",
                    recent_context=recent_ctx,
                    parent_session_id=state.session_id,
                ),
                timeout=12.0,
            )
            return text.strip() if isinstance(text, str) and text.strip() else None
        except Exception:
            return None

    async def action_open_rotation_review(self) -> None:
        state = self.current_state
        if state is None or not self.client.is_connected:
            return
        subagent_draft = await self._try_build_subagent_handoff(state)
        if subagent_draft:
            draft = subagent_draft
            self._schedule_telemetry_poll()
            await self._append_notice(t(self.lang, "handoff_subagent_used"))
        else:
            draft = await asyncio.to_thread(_build_handoff_draft, state, self.client_config.cwd)

        def _on_confirmed(edited_handoff: str | None) -> None:
            if edited_handoff:
                task = asyncio.create_task(self._execute_confirmed_rotation(edited_handoff))
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)

        self.push_screen(HandoffReviewModal(draft, lang=self.lang), callback=_on_confirmed)

    async def action_open_delegation_modal(self, initial_question: str = "") -> None:
        """Open user-driven `[D] > 60K` delegation modal (ToS Section 6 compliant)."""
        state = self.current_state
        prefill = initial_question.strip() or (state.active_goal if state else "")

        def _on_delegated(payload: dict[str, Any] | None) -> None:
            if not payload:
                return
            mode = str(payload.get("mode") or "")
            question = str(payload.get("question") or "").strip()
            scope_paths = list(payload.get("scope_paths") or [])
            if not question:
                return
            if mode == "insert_instruction":
                scope_arg = f", scope_paths={json.dumps(scope_paths)}" if scope_paths else ""
                instruction = (
                    f"Delegate this read-only codebase research to the MCP subagent tool "
                    f"`ogy_research_repo(question={json.dumps(question)}{scope_arg})` "
                    f"(off OAuth quota) and use its concise sourced report before editing."
                )
                ta = self.query_one("#prompt-input", PromptTextArea)
                ta.text = (ta.text.rstrip() + "\n\n" + instruction).strip() if ta.text.strip() else instruction
                ta.focus()
                task = asyncio.create_task(self._append_notice(t(self.lang, "delegate_inserted")))
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)
            elif mode == "run_parallel":
                task = asyncio.create_task(self._run_parallel_delegation(question, scope_paths))
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)

        self.push_screen(DelegationModal(prefill, lang=self.lang), callback=_on_delegated)

    async def _run_parallel_delegation(self, question: str, scope_paths: list[str]) -> None:
        """Run read-only `ogy_research_repo` in parallel and attach its sourced report to the next prompt."""
        svc = self._get_configured_subagents_service()
        if svc is None:
            await self._append_notice(
                t(self.lang, "delegate_failed", err="No Gemini API key configured or subagents disabled."),
                level="warn",
            )
            return
        self._set_working(True, t(self.lang, "delegate_running"))
        try:
            sid = self.registry.active_session_id or self.client.active_session_id
            report = await svc.research_repo(
                question,
                scope_paths=scope_paths or None,
                workspace_path=self.client_config.cwd,
                parent_session_id=sid,
            )
            self._schedule_telemetry_poll()
            block = (
                f"[Read-only subagent report (`ogy_research_repo`) for: {question}]\n"
                f"{report}\n"
                f"[/subagent report]"
            )
            ta = self.query_one("#prompt-input", PromptTextArea)
            ta.text = (ta.text.rstrip() + "\n\n" + block).strip() if ta.text.strip() else block
            ta.focus()
            await self._append_notice(t(self.lang, "delegate_done", chars=len(report)))
        except Exception as exc:
            await self._append_notice(t(self.lang, "delegate_failed", err=exc), level="err")
        finally:
            self._set_working(False)

    async def _execute_confirmed_rotation(self, handoff_content: str) -> None:
        """Write handoff, open fresh isolated session (keeping old session consultable in Ctrl+S)."""
        handoff_dir = Path(self.client_config.cwd) / ".ogy"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        handoff_path = handoff_dir / "handoff.md"
        await asyncio.to_thread(handoff_path.write_text, handoff_content, "utf-8")

        new_sid = await self.client.new_session(self.client_config.cwd)
        self._ensure_state(new_sid, activate=True)
        await self._ensure_transcript(new_sid, activate=True)
        await self._apply_initial_profile(new_sid)
        self._refresh_ui()
        await self._append_notice(
            t(self.lang, "session_rotated", sid=new_sid[:12], path=str(handoff_path)),
            session_id=new_sid,
        )
        await self._dispatch_prompt(f"Continue from session handoff:\n\n{handoff_content}", session_id=new_sid)

    async def action_accept_rotation(self) -> None:
        await self.action_open_rotation_review()

    async def action_accept_proposal(self) -> None:
        if not self.active_proposals:
            return
        await self._apply_proposal(self.active_proposals[0])

    async def _apply_proposal(self, prop: ActionProposal) -> None:
        if prop.kind == "rotate_session":
            await self.action_open_rotation_review()
        elif prop.kind == "upgrade_config":
            if prop.target_model:
                await self.client.set_config_option("model", prop.target_model)
            if prop.target_effort:
                sid = self.client.active_session_id
                cfg = self.client.session_configs.get(sid or "", {})
                opt_id = "thought_level" if ("thought_level" in cfg and "thinking_level" not in cfg) else "thinking_level"
                await self.client.set_config_option(opt_id, prop.target_effort)
            self._refresh_ui()
            await self._append_notice(
                t(self.lang, "upgrade_applied", model=prop.target_model, effort=prop.target_effort, tax=prop.switch_tax_usd)
            )
        elif prop.kind == "delegate_subagent":
            await self.action_open_delegation_modal()
        elif prop.kind == "switch_to_v1_run":
            model = prop.target_model or "claude-5.5-sonnet"
            cmd = f"ogy run --model {model}"
            try:
                self.copy_to_clipboard(cmd)
            except Exception:
                pass
            self._pending_v1_proposal = None
            self._refresh_ui()
            await self._append_notice(t(self.lang, "v1_switch_notice", model=model), level="warn")
