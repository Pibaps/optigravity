"""Textual interactive TUI entry point (`ogy tui` / `ogy chat`) for ACP v1 (`agy acp`)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider

from ogy_core.config import OgyConfig, load_config
from ogy_frontends.acp_tui.client import AcpClient, AcpClientConfig
from ogy_frontends.acp_tui.i18n import SUPPORTED_LANGS, t
from ogy_frontends.acp_tui.orchestrator import ActionProposal, SessionOrchestrator
from ogy_frontends.acp_tui.screens.main import MainWorkspaceControllerMixin
from ogy_frontends.acp_tui.screens.modals import (
    DelegationModal,
    ElicitationModal,
    GlossaryModal,
    HandoffReviewModal,
    HelpGlossaryModal,
    PermissionModal,
    SessionPickerModal,
)
from ogy_frontends.acp_tui.state.sessions import SessionRegistry
from ogy_frontends.acp_tui.widgets.telemetry import (
    MetricGauge,
    MetricRow,
    TelemetryPanel,
    _human_quota_label,
)
from ogy_frontends.acp_tui.widgets.transcript import SessionTranscript, _clean_file_links

__all__ = [
    "DelegationModal",
    "ElicitationModal",
    "GlossaryModal",
    "HandoffReviewModal",
    "HelpGlossaryModal",
    "MetricGauge",
    "MetricRow",
    "OptigravityApp",
    "OptigravityCommandProvider",
    "PermissionModal",
    "SessionPickerModal",
    "TelemetryPanel",
    "_clean_file_links",
    "_human_quota_label",
    "run_tui",
]


class OptigravityCommandProvider(Provider):
    """Exposes Optigravity TUI commands, models, efforts, modes, and languages in `Ctrl+P`."""

    def _build_entries(self) -> list[tuple[str, str, Any]]:
        app = self.app
        if not isinstance(app, OptigravityApp):
            return []
        lang = app.lang
        entries: list[tuple[str, str, Any]] = [
            (t(lang, "pal_glossary"), t(lang, "pal_glossary_help"), app.action_show_glossary),
            (t(lang, "pal_rotate"), t(lang, "pal_rotate_help"), app.action_open_rotation_review),
            (t(lang, "pal_delegate"), t(lang, "pal_delegate_help"), app.action_open_delegation_modal),
            (t(lang, "pal_handoff"), t(lang, "pal_handoff_help"), lambda: app._run_palette_cmd("/handoff")),
            (t(lang, "pal_new"), t(lang, "pal_new_help"), app.action_new_session),
            (t(lang, "pal_sessions"), t(lang, "pal_sessions_help"), app.action_open_session_picker),
            (t(lang, "pal_cancel"), t(lang, "pal_cancel_help"), app.action_cancel_turn),
            (t(lang, "pal_telemetry"), t(lang, "pal_telemetry_help"), app.action_toggle_telemetry),
            (t(lang, "pal_help"), t(lang, "pal_help_help"), lambda: app._run_palette_cmd("/help")),
        ]
        if app.active_proposals:
            top = app.active_proposals[0]
            entries.insert(0, (t(lang, "pal_proposal", title=top.title), top.detail, app.action_accept_proposal))
        for code, label in (("fr", "Français (fr)"), ("en", "English (en)")):
            entries.append((t(lang, "pal_lang", label=label), t(lang, "pal_lang_help", label=label), lambda c=code: app._run_palette_cmd(f"/lang {c}")))
        for model in app.get_available_models():
            entries.append((t(lang, "pal_model", val=model), t(lang, "pal_model_help", val=model), lambda m=model: app._run_palette_cmd(f"/model {m}")))
        for effort in app.get_available_efforts():
            entries.append((t(lang, "pal_effort", val=effort), t(lang, "pal_effort_help", val=effort), lambda e=effort: app._run_palette_cmd(f"/effort {e}")))
        for mode in app.get_available_modes():
            entries.append((t(lang, "pal_mode", val=mode), t(lang, "pal_mode_help", val=mode), lambda md=mode: app._run_palette_cmd(f"/mode {md}")))
        return entries

    async def discover(self) -> Hits:
        for title, help_text, callback in self._build_entries():
            yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, help_text, callback in self._build_entries():
            score = matcher.match(f"{title} {help_text}")
            if score > 0:
                yield Hit(score, matcher.highlight(title), callback, help=help_text)


class OptigravityApp(MainWorkspaceControllerMixin, App[None]):
    """Interactive Textual 8+ TUI for Optigravity (`agy acp`)."""

    TITLE = "Optigravity"
    SUB_TITLE = "ACP"
    CSS_PATH = "styles.tcss"
    COMMANDS = App.COMMANDS | {OptigravityCommandProvider}

    BINDINGS = [
        Binding("ctrl+c", "cancel_turn", "Cancel", show=True, priority=True),
        Binding("ctrl+n", "new_session", "New", show=True),
        Binding("ctrl+s", "open_session_picker", "Sessions", show=True),
        Binding("ctrl+o", "toggle_verbosity", "Tools", show=True),
        Binding("ctrl+b", "toggle_telemetry", "Telemetry", show=True),
        Binding("f1", "show_glossary", "Glossary", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        *,
        client_config: AcpClientConfig | None = None,
        ogy_config: OgyConfig | None = None,
        initial_prompt: str | None = None,
        auto_connect: bool = True,
        injected_agent_conn: Any = None,
        initial_model: str | None = None,
        initial_effort: str | None = None,
        lang: str = "fr",
        subagents_service: Any = None,
    ) -> None:
        super().__init__()
        self.lang = lang if lang in SUPPORTED_LANGS else "fr"
        self.ogy_config = ogy_config or load_config()
        self.client_config = client_config or AcpClientConfig()
        self.initial_prompt = initial_prompt
        self.initial_model = initial_model
        self.initial_effort = initial_effort
        self.auto_connect = auto_connect
        self._injected_agent_conn = injected_agent_conn
        self.subagents_service = subagents_service

        self.client = AcpClient(
            self.client_config,
            ogy_config=self.ogy_config,
            on_event=self._on_acp_event,
        )
        self.orchestrator = SessionOrchestrator(
            ogy_config=self.ogy_config,
            auth_method=self.client_config.auth_method,
        )
        self.registry = SessionRegistry()
        self.active_proposals: list[ActionProposal] = []
        self._pending_v1_proposal: ActionProposal | None = None
        self._transcripts: dict[str, SessionTranscript] = {}
        self._replaying_sessions: set[str] = set()
        self._active_elicitations: dict[str, ElicitationModal] = {}
        self._inflight_tasks: set[asyncio.Task[Any]] = set()
        self.verbosity: str = "low"

        # Working spinner state
        self._working: bool = False
        self._working_label: str = ""
        self._working_since: float = 0.0
        self._spinner_idx: int = 0


def run_tui(
    *,
    cwd: str = ".",
    sandbox: bool = False,
    add_dirs: list[str] | None = None,
    agent: str | None = None,
    log_file: str | None = None,
    auth_method: str = "oauth-personal",
    initial_prompt: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    lang: str = "fr",
    command_override: list[str] | None = None,
) -> int:
    """Launch the Optigravity Textual TUI."""
    import json
    import os

    resolved_override = command_override
    if resolved_override is None:
        raw_override = os.environ.get("OGY_ACP_COMMAND_OVERRIDE", "").strip()
        if raw_override:
            try:
                parsed = json.loads(raw_override)
                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed) and parsed:
                    resolved_override = parsed
            except json.JSONDecodeError:
                pass

    cfg = AcpClientConfig(
        cwd=str(Path(cwd).resolve()),
        sandbox=sandbox,
        add_dirs=list(add_dirs or []),
        agent=agent,
        log_file=log_file,
        auth_method=auth_method,
        command_override=resolved_override,
    )
    app = OptigravityApp(
        client_config=cfg,
        initial_prompt=initial_prompt,
        initial_model=model,
        initial_effort=effort,
        lang=lang,
    )
    app.run()
    return 0
