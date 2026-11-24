"""Textual interactive TUI (`ogy tui` / `ogy chat`) for ACP v1 (`agy acp`)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    ProgressBar,
    Static,
)

from ogy_core.config import OgyConfig, load_config
from ogy_core.context_health import write_handoff
from ogy_frontends.acp_tui.client import (
    AcpAuthError,
    AcpClient,
    AcpClientConfig,
    AcpEvent,
    AgentMessageEvent,
    AgentThoughtEvent,
    ConfigUpdateEvent,
    ElicitationRequestEvent,
    PermissionRequestEvent,
    SessionNoticeEvent,
    ToolCallEvent,
    UsageEvent,
)
from ogy_frontends.acp_tui.orchestrator import (
    ActionProposal,
    SessionOrchestrator,
    SessionTelemetryState,
)


class PermissionModal(ModalScreen[str | None]):
    """Interactive modal for `session/request_permission`."""

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
    }
    #perm-dialog {
        width: 72;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #perm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #perm-buttons {
        margin-top: 1;
        height: auto;
    }
    #perm-buttons Button {
        margin-right: 1;
    }
    """

    def __init__(self, event: PermissionRequestEvent) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        with Container(id="perm-dialog"):
            yield Static(f"Permission requested: {self.event.tool_name}", id="perm-title")
            yield Static(self.event.title, id="perm-detail")
            yield Static(
                "[ogy firebreak reminder] Prefer bounded reads and wrapped shell commands.",
                id="perm-hint",
            )
            with Horizontal(id="perm-buttons"):
                if self.event.options:
                    for opt_id, opt_name, kind in self.event.options:
                        variant = "success" if "allow" in kind else "error"
                        yield Button(opt_name, id=f"opt-{opt_id}", variant=variant)
                else:
                    yield Button("Allow once", id="opt-allow_once", variant="success")
                    yield Button("Reject once", id="opt-reject_once", variant="error")

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("opt-"):
            chosen = btn_id[4:]
            if "reject" in chosen:
                self.dismiss(None)
            else:
                self.dismiss(chosen)
        else:
            self.dismiss(None)


class TelemetryPanel(Static):
    """Live telemetry sidebar showing thoughtTokens, cache hit, context rot gauge, cost, and quotas."""

    DEFAULT_CSS = """
    TelemetryPanel {
        width: 36;
        border-left: solid $primary;
        padding: 1;
        background: $panel;
        height: 1fr;
    }
    .zone-green { color: $success; text-style: bold; }
    .zone-amber { color: $warning; text-style: bold; }
    .zone-red { color: $error; text-style: bold; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Optigravity Telemetry", classes="panel-title")
        yield Label("Zone: GREEN (keep_warm)", id="tel-zone", classes="zone-green")
        yield ProgressBar(total=100, show_eta=False, id="tel-ctx-bar")
        yield Label("Context: 0 / 1,000K (0%)", id="tel-ctx")
        yield Label("Cache Hit: --", id="tel-cache")
        yield Label("Thought Tokens: 0", id="tel-thought")
        yield Label("In / Out: 0 / 0", id="tel-io")
        yield Label("Cost: $0.0000", id="tel-cost")
        yield Label("Idle Gap: 0s", id="tel-idle")
        yield Label("Failures: 0", id="tel-fail")
        yield Static("Quotas (cached): --", id="tel-quota")

    def refresh_state(self, state: SessionTelemetryState, quotas: dict[str, dict[str, Any]]) -> None:
        health = state.assess_health()
        zone_lbl = self.query_one("#tel-zone", Label)
        zone_lbl.update(f"Zone: {health.zone.upper()} ({health.action})")
        zone_lbl.set_classes(f"zone-{health.zone}")

        pct = min(100, int((state.context_tokens / max(1, state.context_max)) * 100))
        bar = self.query_one("#tel-ctx-bar", ProgressBar)
        bar.update(progress=pct)

        self.query_one("#tel-ctx", Label).update(
            f"Context: {state.context_tokens:,} / {state.context_max:,} ({pct}%)"
        )
        cache_str = f"{health.cache_hit_pct:.1f}%" if health.cache_hit_pct is not None else "--"
        self.query_one("#tel-cache", Label).update(f"Cache Hit: {cache_str}")
        self.query_one("#tel-thought", Label).update(f"Thought Tokens: {state.thought_tokens:,}")
        self.query_one("#tel-io", Label).update(
            f"In / Out: {state.input_tokens:,} / {state.output_tokens:,}"
        )
        self.query_one("#tel-cost", Label).update(f"Cost: ${state.total_cost_usd:.4f}")
        self.query_one("#tel-idle", Label).update(f"Idle Gap: {int(state.idle_gap_s)}s")
        self.query_one("#tel-fail", Label).update(f"Failures: {state.consecutive_failures}")

        if quotas:
            parts = []
            for bid, info in sorted(quotas.items()):
                frac = info.get("remaining_fraction")
                if frac is not None:
                    parts.append(f"{bid}:{int(float(frac) * 100)}%")
            q_text = "Quotas: " + (" | ".join(parts) if parts else "--")
        else:
            q_text = "Quotas (cached): --"
        self.query_one("#tel-quota", Static).update(q_text)


class OptigravityApp(App[None]):
    """Interactive Textual TUI for Optigravity V2 (`agy acp`)."""

    TITLE = "Optigravity V2 (ACP)"
    CSS = """
    #session-bar {
        height: 1;
        background: $boost;
        padding: 0 1;
    }
    #proposal-banner {
        height: auto;
        background: $warning-darken-3;
        color: $text;
        padding: 0 1;
        display: none;
    }
    #proposal-banner.visible {
        display: block;
    }
    #main-body {
        height: 1fr;
    }
    #stream-scroll {
        width: 1fr;
        padding: 1;
    }
    #input-row {
        height: auto;
        padding: 0 1;
        border-top: solid $primary;
    }
    #prompt-input {
        width: 1fr;
    }
    .tool-card {
        border: round $secondary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    .user-msg {
        background: $primary-darken-3;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_turn", "Cancel Turn", show=True, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+n", "new_session", "New Session", show=True),
        Binding("ctrl+r", "accept_rotation", "Rotate", show=False),
    ]

    def __init__(
        self,
        *,
        client_config: AcpClientConfig | None = None,
        ogy_config: OgyConfig | None = None,
        initial_prompt: str | None = None,
        auto_connect: bool = True,
        injected_agent_conn: Any = None,
    ) -> None:
        super().__init__()
        self.ogy_config = ogy_config or load_config()
        self.client_config = client_config or AcpClientConfig()
        self.initial_prompt = initial_prompt
        self.auto_connect = auto_connect
        self._injected_agent_conn = injected_agent_conn

        self.client = AcpClient(
            self.client_config,
            ogy_config=self.ogy_config,
            on_event=self._on_acp_event,
        )
        self.orchestrator = SessionOrchestrator(
            ogy_config=self.ogy_config,
            auth_method=self.client_config.auth_method,
        )
        self.sessions: dict[str, SessionTelemetryState] = {}
        self.active_proposals: list[ActionProposal] = []

        self._active_msg_widget: Markdown | None = None
        self._active_msg_buffer: str = ""
        self._active_thought_body: Static | None = None
        self._active_thought_box: Collapsible | None = None
        self._active_thought_buffer: str = ""
        self._tool_widgets: dict[str, Static] = {}
        self._inflight_tasks: set[asyncio.Task[Any]] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Session: (connecting...)", id="session-bar")
        yield Static("", id="proposal-banner")
        with Horizontal(id="main-body"):
            yield VerticalScroll(id="stream-scroll")
            yield TelemetryPanel(id="telemetry-panel")
        with Horizontal(id="input-row"):
            yield Input(
                placeholder="Prompt, @file, or /help (/model, /effort, /mode, /rotate, /sessions)...",
                id="prompt-input",
            )
            yield Button("Send", id="send-btn", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        if self.auto_connect:
            self._boot_acp()

    @work(exclusive=True)
    async def _boot_acp(self) -> None:
        try:
            await self.client.connect(agent_conn=self._injected_agent_conn)
            sid = await self.client.new_session(self.client_config.cwd)
            self._ensure_state(sid)
            self._refresh_ui()
            if self.initial_prompt:
                await self._dispatch_prompt(self.initial_prompt)
        except AcpAuthError as exc:
            await self._append_notice(f"Auth required: {exc}")
        except Exception as exc:
            await self._append_notice(f"Failed to start `agy acp`: {exc}")

    def _ensure_state(self, session_id: str) -> SessionTelemetryState:
        if session_id not in self.sessions:
            cfg = self.client.session_configs.get(session_id, {})
            self.sessions[session_id] = SessionTelemetryState(
                session_id=session_id,
                model_id=cfg.get("model", "gemini-4-hydrogen"),
                effort=cfg.get("thinking_level") or cfg.get("thought_level") or "medium",
                mode=cfg.get("mode", "auto"),
            )
        return self.sessions[session_id]

    @property
    def current_state(self) -> SessionTelemetryState | None:
        sid = self.client.active_session_id
        if not sid:
            return None
        return self._ensure_state(sid)

    async def _on_acp_event(self, event: AcpEvent) -> None:
        if isinstance(event, AgentMessageEvent):
            await self._handle_message_chunk(event)
        elif isinstance(event, AgentThoughtEvent):
            await self._handle_thought_chunk(event)
        elif isinstance(event, ToolCallEvent):
            await self._handle_tool_call(event)
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
                PermissionModal(event),
                callback=lambda choice: event.future.done() or event.future.set_result(choice),
            )
        elif isinstance(event, ElicitationRequestEvent):
            if not event.future.done():
                event.future.set_result({"action": "decline"})
        elif isinstance(event, SessionNoticeEvent):
            await self._append_notice(event.message)

    async def _handle_message_chunk(self, event: AgentMessageEvent) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        if self._active_msg_widget is None:
            self._active_msg_buffer = event.text
            md = Markdown(self._active_msg_buffer)
            self._active_msg_widget = md
            await stream.mount(md)
        else:
            self._active_msg_buffer += event.text
            await self._active_msg_widget.update(self._active_msg_buffer)
        stream.scroll_end(animate=False)

    async def _handle_thought_chunk(self, event: AgentThoughtEvent) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        if self._active_thought_body is None or self._active_thought_box is None:
            self._active_thought_buffer = event.text
            body = Static(self._active_thought_buffer)
            box = Collapsible(body, title=f"Thinking ({len(self._active_thought_buffer)} chars)", collapsed=True)
            self._active_thought_body = body
            self._active_thought_box = box
            await stream.mount(box)
        else:
            self._active_thought_buffer += event.text
            self._active_thought_body.update(self._active_thought_buffer)
            self._active_thought_box.title = f"Thinking ({len(self._active_thought_buffer)} chars)"

    async def _handle_tool_call(self, event: ToolCallEvent) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        state = self._ensure_state(event.session_id)

        if event.status in ("completed", "failed", "error"):
            if event.is_error:
                state.consecutive_failures += 1
                state.last_error_summary = f"{event.name}: {str(event.raw_output or '')[:160]}"
            else:
                state.consecutive_failures = 0

        if isinstance(event.raw_input, dict):
            path_val = event.raw_input.get("TargetFile") or event.raw_input.get("path")
            if isinstance(path_val, str) and path_val not in state.modified_files:
                state.modified_files.append(path_val)

        label = f"[{event.status.upper()}] {event.name} — {event.title}"
        card = self._tool_widgets.get(event.tool_call_id)
        if card is None:
            card = Static(label, classes="tool-card")
            self._tool_widgets[event.tool_call_id] = card
            await stream.mount(card)
        else:
            card.update(label)
        stream.scroll_end(animate=False)
        self._refresh_ui()

    async def _handle_usage(self, event: UsageEvent) -> None:
        state = self._ensure_state(event.session_id)
        if event.context_used is not None:
            state.context_tokens = event.context_used
        if event.context_max is not None:
            state.context_max = event.context_max
        if event.is_turn_final:
            state.turn_count += 1
            state.input_tokens += event.input_tokens
            state.output_tokens += event.output_tokens
            state.thought_tokens += event.thought_tokens
            state.cached_read_tokens += event.cached_read_tokens
            state.cached_write_tokens += event.cached_write_tokens
            if event.cost_usd is not None:
                state.total_cost_usd += event.cost_usd
            self._active_msg_widget = None
            self._active_msg_buffer = ""
            self._active_thought_body = None
            self._active_thought_box = None
            self._active_thought_buffer = ""
        elif event.cost_usd is not None:
            state.total_cost_usd = max(state.total_cost_usd, event.cost_usd)
        self._refresh_ui()

    async def _append_notice(self, message: str) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        await stream.mount(Static(f"[ogy] {message}", classes="tool-card"))
        stream.scroll_end(animate=False)

    def _refresh_ui(self) -> None:
        state = self.current_state
        bar = self.query_one("#session-bar", Static)
        banner = self.query_one("#proposal-banner", Static)
        if state is None:
            bar.update("Session: (none)")
            banner.remove_class("visible")
            return

        bar.update(
            f"Session: {state.session_id[:12]} | Sessions: {len(self.sessions)} | "
            f"Model: {state.model_id} | Effort: {state.effort} | Mode: {state.mode}"
        )
        try:
            quotas = self.orchestrator.read_quota_snapshots()
        except Exception:
            quotas = {}
        self.query_one("#telemetry-panel", TelemetryPanel).refresh_state(state, quotas)

        self.active_proposals = self.orchestrator.evaluate_proposals(state)
        if self.active_proposals:
            top = self.active_proposals[0]
            banner.update(f"{top.title} — {top.detail}")
            banner.add_class("visible")
        else:
            banner.remove_class("visible")

    @on(Button.Pressed, "#send-btn")
    async def _on_send_pressed(self) -> None:
        await self._submit_input()

    @on(Input.Submitted, "#prompt-input")
    async def _on_input_submitted(self) -> None:
        await self._submit_input()

    async def _submit_input(self) -> None:
        inp = self.query_one("#prompt-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.value = ""

        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        # Concurrent steering: launch prompt as a background task so a second prompt can steer in flight
        task = asyncio.create_task(self._dispatch_prompt(text))
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    async def _dispatch_prompt(self, text: str) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        await stream.mount(Static(f"> {text}", classes="user-msg"))
        stream.scroll_end(animate=False)

        state = self.current_state
        if state is not None:
            state.active_goal = text[:200]
        try:
            await self.client.send_prompt(text)
        except Exception as exc:
            await self._append_notice(f"Prompt error: {exc}")

    async def _handle_slash_command(self, cmd_line: str) -> None:
        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            await self._append_notice(
                "Commands: /model <slug>, /effort <low|medium|high>, /mode <default|accept-edits|auto|plan>, "
                "/rotate, /handoff, /new, /sessions, /resume <id>"
            )
            return
        if cmd == "/model" and arg:
            compat = self.orchestrator.check_model_compatibility(arg)
            if compat is not None:
                await self._append_notice(f"{compat.title}: {compat.detail}")
                return
            await self.client.set_config_option("model", arg)
            self._refresh_ui()
            return
        if cmd == "/effort" and arg:
            await self.client.set_config_option("thinking_level", arg)
            self._refresh_ui()
            return
        if cmd == "/mode" and arg:
            try:
                await self.client.set_config_option("mode", arg)
            except ValueError as exc:
                await self._append_notice(str(exc))
            self._refresh_ui()
            return
        if cmd in ("/rotate", "/clear"):
            await self.action_accept_rotation()
            return
        if cmd == "/handoff":
            state = self.current_state
            if state:
                res = write_handoff(
                    goal=state.active_goal,
                    modified_files=state.modified_files,
                    checks=state.last_error_summary or "ok",
                    negative_constraints=[],
                    next_step="Resume from handoff",
                    workspace_path=self.client_config.cwd,
                )
                await self._append_notice(f"Handoff written to {res['path']} ({res['lines']} lines)")
            return
        if cmd == "/new":
            await self.action_new_session()
            return
        if cmd == "/sessions":
            rows = await self.client.list_sessions()
            ids = [str(r.get("session_id", ""))[:12] for r in rows[:8]]
            await self._append_notice(f"Sessions: {', '.join(ids) or '(none)'}")
            return
        if cmd == "/resume" and arg:
            await self.client.resume_session(arg)
            self._ensure_state(arg)
            self._refresh_ui()
            return
        await self._append_notice(f"Unknown or incomplete command: {cmd_line}")

    async def action_cancel_turn(self) -> None:
        await self.client.cancel_turn()
        await self._append_notice("Turn cancelled (`session/cancel`). `agy acp` remains active.")

    async def action_new_session(self) -> None:
        if not self.client.is_connected:
            return
        sid = await self.client.new_session(self.client_config.cwd)
        self._ensure_state(sid)
        self._refresh_ui()
        await self._append_notice(f"Started new session {sid[:12]}")

    async def action_accept_rotation(self) -> None:
        state = self.current_state
        if state is None or not self.client.is_connected:
            return
        new_sid, path, content = await self.orchestrator.execute_rotation(
            self.client,
            state,
            cwd=self.client_config.cwd,
        )
        self._ensure_state(new_sid)
        self._refresh_ui()
        await self._append_notice(f"Rotated to {new_sid[:12]} using {path}")
        await self._dispatch_prompt(f"Continue from session handoff:\n\n{content}")


def run_tui(
    *,
    cwd: str = ".",
    sandbox: bool = False,
    add_dirs: list[str] | None = None,
    agent: str | None = None,
    log_file: str | None = None,
    auth_method: str = "oauth-personal",
    initial_prompt: str | None = None,
) -> int:
    """Launch the Optigravity V2 Textual TUI."""
    cfg = AcpClientConfig(
        cwd=str(Path(cwd).resolve()),
        sandbox=sandbox,
        add_dirs=list(add_dirs or []),
        agent=agent,
        log_file=log_file,
        auth_method=auth_method,
    )
    app = OptigravityApp(client_config=cfg, initial_prompt=initial_prompt)
    app.run()
    return 0
