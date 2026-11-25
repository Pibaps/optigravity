"""Textual interactive TUI (`ogy tui` / `ogy chat`) for ACP v1 (`agy acp`)."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape as rich_escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    ProgressBar,
    Static,
)
from textual.widgets.option_list import Option

from ogy_core.config import OgyConfig, load_config
from ogy_core.context_health import write_handoff
from ogy_core.router import route_stage1
from ogy_core.storage import connect, write_tx
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
from ogy_frontends.acp_tui.i18n import (
    GLOSSARY_MARKDOWN,
    SUPPORTED_LANGS,
    ZONE_LABELS,
    human_quota_label,
    t,
)
from ogy_frontends.acp_tui.orchestrator import (
    ActionProposal,
    SessionOrchestrator,
    SessionTelemetryState,
)


# ---------------------------------------------------------------------------
# Command catalog, defaults & helpers
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "gemini-4-hydrogen",
    "gemini-4-neon",
    "gemini-4-carbon",
]

DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_FRUGAL_MODEL = "gemini-4-hydrogen"
DEFAULT_FRUGAL_EFFORT = "medium"


def _resolve_acp_model_slug(available_models: list[str], target_model: str, target_effort: str) -> str:
    """Resolve `target_model` + `target_effort` against advertised ACP model slugs."""
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
DEFAULT_MODES = ["default", "accept-edits", "auto", "plan"]
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


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
    SlashCommandSpec("/btw", "/btw <question>", "cmd_btw", True),
    SlashCommandSpec("/lang", "/lang <fr|en>", "cmd_lang", True),
    SlashCommandSpec("/rotate", "/rotate", "cmd_rotate"),
    SlashCommandSpec("/handoff", "/handoff", "cmd_handoff"),
    SlashCommandSpec("/new", "/new", "cmd_new"),
    SlashCommandSpec("/sessions", "/sessions", "cmd_sessions"),
    SlashCommandSpec("/resume", "/resume <id>", "cmd_resume", True),
    SlashCommandSpec("/clear", "/clear", "cmd_clear"),
    SlashCommandSpec("/glossary", "/glossary", "cmd_glossary"),
    SlashCommandSpec("/help", "/help", "cmd_help"),
)


def _format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _human_quota_label(bucket_id: str, lang: str = "fr") -> str:
    return human_quota_label(bucket_id, lang)


def _safe_widget_id(raw: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "-" for ch in raw)


_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```)")
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://([^)\s]+)\)")
_LINE_ANCHOR_RE = re.compile(r"#L(\d+)(?:-L?(\d+))?$")


def _clean_file_links(markdown_text: str) -> str:
    """Replace raw `[label](file:///...)` links with compact inline code outside fenced blocks."""
    if "file://" not in markdown_text:
        return markdown_text

    def _replace_match(match: re.Match[str]) -> str:
        raw_label = match.group(1).strip()
        target = match.group(2)
        clean_label = raw_label.strip("`").strip()
        anchor_m = _LINE_ANCHOR_RE.search(target)
        if anchor_m and ":" not in clean_label and "#L" not in clean_label:
            start_l, end_l = anchor_m.group(1), anchor_m.group(2)
            suffix = f":{start_l}-{end_l}" if end_l else f":{start_l}"
            clean_label = f"{clean_label}{suffix}"
        return f"`{clean_label}`"

    parts = _FENCED_CODE_RE.split(markdown_text)
    for idx in range(0, len(parts), 2):
        parts[idx] = _FILE_LINK_RE.sub(_replace_match, parts[idx])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Modals (Permissions & Glossary)
# ---------------------------------------------------------------------------


class PermissionModal(ModalScreen[str | None]):
    """Interactive modal for `session/request_permission`."""

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
        background: rgba(8, 10, 14, 0.78);
    }
    #perm-dialog {
        width: 72;
        height: auto;
        border: solid #303a4b;
        background: #151922;
        padding: 1 2;
    }
    #perm-title {
        text-style: bold;
        color: #e6edf3;
        margin-bottom: 1;
    }
    #perm-detail {
        color: #c9d1d9;
        margin-bottom: 1;
    }
    #perm-hint {
        color: #8b949e;
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

    def __init__(self, event: PermissionRequestEvent, *, lang: str = "fr") -> None:
        super().__init__()
        self.event = event
        self.lang = lang

    def compose(self) -> ComposeResult:
        with Container(id="perm-dialog"):
            yield Static(t(self.lang, "perm_title", tool=self.event.tool_name), id="perm-title", markup=False)
            yield Static(self.event.title, id="perm-detail", markup=False)
            yield Static(t(self.lang, "perm_hint"), id="perm-hint", markup=False)
            with Horizontal(id="perm-buttons"):
                if self.event.options:
                    for opt_id, opt_name, kind in self.event.options:
                        variant = "success" if "allow" in kind else "error"
                        yield Button(opt_name, id=f"opt-{opt_id}", variant=variant)
                else:
                    yield Button(t(self.lang, "perm_allow"), id="opt-allow_once", variant="success")
                    yield Button(t(self.lang, "perm_reject"), id="opt-reject_once", variant="error")

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


class GlossaryModal(ModalScreen[None]):
    """Modal explaining Optigravity metrics, zones, quotas (`3p`), and shortcuts."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("f1", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    GlossaryModal {
        align: center middle;
        background: rgba(8, 10, 14, 0.80);
    }
    #glossary-dialog {
        width: 84;
        max-width: 92%;
        height: 82%;
        border: solid #303a4b;
        background: #151922;
        padding: 1 2;
    }
    #glossary-scroll {
        height: 1fr;
        padding-right: 1;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3344;
        scrollbar-color-hover: #485263;
        scrollbar-background: #151922;
    }
    #glossary-footer {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #glossary-close {
        background: #1f2633;
        color: #e6edf3;
        border: solid #303a4b;
    }
    """

    def __init__(self, *, lang: str = "fr") -> None:
        super().__init__()
        self.lang = lang

    def compose(self) -> ComposeResult:
        md = GLOSSARY_MARKDOWN.get(self.lang, GLOSSARY_MARKDOWN["fr"])
        with Container(id="glossary-dialog"):
            with VerticalScroll(id="glossary-scroll"):
                yield Markdown(md)
            with Horizontal(id="glossary-footer"):
                yield Button(t(self.lang, "glossary_close"), id="glossary-close")

    @on(Button.Pressed, "#glossary-close")
    def _on_close_pressed(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Telemetry Widgets (Pro Gauges & Aligned Rows)
# ---------------------------------------------------------------------------


class MetricGauge(Vertical):
    """Compact labeled progress gauge with dynamic severity coloring."""

    DEFAULT_CSS = """
    MetricGauge {
        height: auto;
        margin-bottom: 1;
    }
    MetricGauge .gauge-header {
        height: 1;
        width: 1fr;
    }
    MetricGauge .gauge-label {
        width: 1fr;
        color: #9aa4b2;
    }
    MetricGauge .gauge-value {
        width: auto;
        color: #e6edf3;
        text-style: bold;
    }
    MetricGauge ProgressBar {
        width: 1fr;
        height: 1;
        padding: 0;
    }
    MetricGauge ProgressBar Bar {
        width: 1fr;
    }
    MetricGauge ProgressBar Bar > .bar--bar,
    MetricGauge ProgressBar Bar > .bar--complete {
        color: #3fb950;
        background: #222936;
    }
    MetricGauge.gauge-warn ProgressBar Bar > .bar--bar,
    MetricGauge.gauge-warn ProgressBar Bar > .bar--complete {
        color: #d29922;
    }
    MetricGauge.gauge-danger ProgressBar Bar > .bar--bar,
    MetricGauge.gauge-danger ProgressBar Bar > .bar--complete {
        color: #f85149;
    }
    MetricGauge.gauge-accent ProgressBar Bar > .bar--bar,
    MetricGauge.gauge-accent ProgressBar Bar > .bar--complete {
        color: #6ea8fe;
    }
    MetricGauge.gauge-muted ProgressBar Bar > .bar--bar,
    MetricGauge.gauge-muted ProgressBar Bar > .bar--complete {
        color: #485263;
    }
    """

    def __init__(
        self,
        label: str,
        initial_value: str = "--",
        *,
        progress: float = 0.0,
        severity: str = "accent",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label_text = label
        self._value_text = initial_value
        self._initial_progress = max(0.0, min(100.0, progress))
        self._severity = severity

    def compose(self) -> ComposeResult:
        with Horizontal(classes="gauge-header"):
            yield Label(self._label_text, classes="gauge-label", markup=False)
            yield Label(self._value_text, classes="gauge-value", markup=False)
        yield ProgressBar(total=100, show_eta=False, show_percentage=False)

    def on_mount(self) -> None:
        self.set_state(self._initial_progress, self._value_text, severity=self._severity)

    def set_label(self, label: str) -> None:
        self.query_one(".gauge-label", Label).update(label)

    def set_state(
        self,
        percent: float,
        value_text: str,
        *,
        label: str | None = None,
        severity: str = "ok",
    ) -> None:
        clamped = max(0.0, min(100.0, float(percent)))
        if label is not None:
            self.query_one(".gauge-label", Label).update(label)
        self.query_one(".gauge-value", Label).update(value_text)
        self.query_one(ProgressBar).update(progress=clamped)
        self.set_classes(f"gauge-{severity}")


class MetricRow(Horizontal):
    """Single aligned key / value telemetry row."""

    DEFAULT_CSS = """
    MetricRow {
        height: 1;
        width: 1fr;
    }
    MetricRow .metric-key {
        width: 1fr;
        color: #8b949e;
    }
    MetricRow .metric-val {
        width: auto;
        color: #e6edf3;
    }
    MetricRow.val-warn .metric-val {
        color: #d29922;
        text-style: bold;
    }
    MetricRow.val-danger .metric-val {
        color: #f85149;
        text-style: bold;
    }
    """

    def __init__(self, key: str, value: str, *, val_id: str, row_id: str | None = None) -> None:
        super().__init__(id=row_id)
        self._key = key
        self._value = value
        self._val_id = val_id

    def compose(self) -> ComposeResult:
        yield Label(self._key, classes="metric-key", markup=False)
        yield Label(self._value, id=self._val_id, classes="metric-val", markup=False)

    def set_key(self, key: str) -> None:
        self.query_one(".metric-key", Label).update(key)

    def set_value(self, value: str, *, status: str = "") -> None:
        self.query_one(f"#{self._val_id}", Label).update(value)
        self.set_classes(f"val-{status}" if status else "")


class TelemetryPanel(VerticalScroll):
    """Live telemetry sidebar with smooth gauges, thin scrollbar, FR/EN labels, and per-bucket quotas."""

    DEFAULT_CSS = """
    TelemetryPanel {
        width: 36;
        border-left: solid #262e3d;
        padding: 1 2;
        background: #131720;
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3344;
        scrollbar-color-hover: #485263;
        scrollbar-background: #131720;
    }
    .panel-section {
        color: #6e7681;
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
        border-bottom: solid #1f2633;
    }
    .panel-section-first {
        margin-top: 0;
    }
    #tel-zone {
        margin-top: 1;
        text-style: bold;
    }
    #tel-zone-hint {
        color: #8b949e;
        margin-bottom: 1;
    }
    .zone-green { color: #3fb950; }
    .zone-amber { color: #d29922; }
    .zone-red { color: #f85149; }
    #quota-list {
        height: auto;
        margin-top: 1;
    }
    #quota-empty {
        color: #6e7681;
    }
    #tel-glossary-hint {
        color: #6e7681;
        margin-top: 1;
    }
    """

    def __init__(self, *, lang: str = "fr", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lang = lang
        self._quota_gauges: dict[str, MetricGauge] = {}

    def compose(self) -> ComposeResult:
        yield Static(t(self.lang, "section_health"), id="sec-health", classes="panel-section panel-section-first", markup=False)
        zone_title, zone_hint = ZONE_LABELS[self.lang][("green", "keep_warm")]
        yield Label(f"● {zone_title}", id="tel-zone", classes="zone-green", markup=False)
        yield Static(zone_hint, id="tel-zone-hint", markup=False)
        yield MetricGauge(t(self.lang, "gauge_context"), "0 / 1.0M (0%)", progress=0, severity="ok", id="gauge-ctx")
        yield MetricGauge(t(self.lang, "gauge_cache"), "--", progress=0, severity="muted", id="gauge-cache")

        yield Static(t(self.lang, "section_session"), id="sec-session", classes="panel-section", markup=False)
        yield MetricRow(t(self.lang, "metric_thought"), "0 tok", val_id="tel-thought", row_id="row-thought")
        yield MetricRow(t(self.lang, "metric_io"), "0 / 0", val_id="tel-io", row_id="row-io")
        yield MetricRow(t(self.lang, "metric_cost"), "$0.0000", val_id="tel-cost", row_id="row-cost")
        yield MetricRow(t(self.lang, "metric_idle"), "0s", val_id="tel-idle", row_id="row-idle")
        yield MetricRow(t(self.lang, "metric_fail"), "0", val_id="tel-fail", row_id="row-fail")

        yield Static(t(self.lang, "section_quotas"), id="sec-quotas", classes="panel-section", markup=False)
        with Vertical(id="quota-list"):
            yield Static(t(self.lang, "quotas_empty"), id="quota-empty", markup=False)

        yield Static(t(self.lang, "glossary_hint"), id="tel-glossary-hint", markup=False)

    def apply_lang(self, lang: str) -> None:
        self.lang = lang
        self.query_one("#sec-health", Static).update(t(lang, "section_health"))
        self.query_one("#sec-session", Static).update(t(lang, "section_session"))
        self.query_one("#sec-quotas", Static).update(t(lang, "section_quotas"))
        self.query_one("#gauge-ctx", MetricGauge).set_label(t(lang, "gauge_context"))
        self.query_one("#gauge-cache", MetricGauge).set_label(t(lang, "gauge_cache"))
        self.query_one("#row-thought", MetricRow).set_key(t(lang, "metric_thought"))
        self.query_one("#row-io", MetricRow).set_key(t(lang, "metric_io"))
        self.query_one("#row-cost", MetricRow).set_key(t(lang, "metric_cost"))
        self.query_one("#row-idle", MetricRow).set_key(t(lang, "metric_idle"))
        self.query_one("#row-fail", MetricRow).set_key(t(lang, "metric_fail"))
        self.query_one("#quota-empty", Static).update(t(lang, "quotas_empty"))
        self.query_one("#tel-glossary-hint", Static).update(t(lang, "glossary_hint"))

    def refresh_state(self, state: SessionTelemetryState, quotas: dict[str, dict[str, Any]]) -> None:
        health = state.assess_health()
        zone_table = ZONE_LABELS.get(self.lang, ZONE_LABELS["fr"])
        zone_title, zone_hint = zone_table.get(
            (health.zone, health.action),
            (f"{health.zone.upper()} · {health.action}", health.reason),
        )
        zone_lbl = self.query_one("#tel-zone", Label)
        zone_lbl.update(f"● {zone_title}")
        zone_lbl.set_classes(f"zone-{health.zone}")
        self.query_one("#tel-zone-hint", Static).update(zone_hint)

        # Context gauge
        ctx_pct = min(100.0, (state.context_tokens / max(1, state.context_max)) * 100.0)
        ctx_sev = "danger" if health.zone == "red" else ("warn" if health.zone == "amber" else "ok")
        self.query_one("#gauge-ctx", MetricGauge).set_state(
            ctx_pct,
            f"{_format_tokens(state.context_tokens)} / {_format_tokens(state.context_max)} ({int(ctx_pct)}%)",
            severity=ctx_sev,
        )

        # Cache hit gauge
        if health.cache_hit_pct is not None:
            c_pct = health.cache_hit_pct
            c_sev = "ok" if c_pct >= 70.0 else ("warn" if c_pct >= 40.0 else "danger")
            self.query_one("#gauge-cache", MetricGauge).set_state(c_pct, f"{c_pct:.1f}%", severity=c_sev)
        else:
            self.query_one("#gauge-cache", MetricGauge).set_state(0.0, "--", severity="muted")

        # Session metric rows
        self.query_one("#tel-thought", Label).update(f"{state.thought_tokens:,} tok")
        self.query_one("#tel-io", Label).update(
            f"{_format_tokens(state.input_tokens)} / {_format_tokens(state.output_tokens)}"
        )
        self.query_one("#tel-cost", Label).update(f"${state.total_cost_usd:.4f}")

        idle_s = int(state.idle_gap_s)
        idle_row = self.query_one("#row-idle", MetricRow)
        idle_status = "danger" if idle_s >= 180 else ("warn" if idle_s >= 150 else "")
        idle_row.set_value(f"{idle_s}s", status=idle_status)

        fail_row = self.query_one("#row-fail", MetricRow)
        fail_status = "danger" if state.consecutive_failures >= 2 else ("warn" if state.consecutive_failures == 1 else "")
        fail_row.set_value(str(state.consecutive_failures), status=fail_status)

        # Per-bucket quota gauges
        self._refresh_quotas(quotas)

    def _refresh_quotas(self, quotas: dict[str, dict[str, Any]]) -> None:
        container = self.query_one("#quota-list", Vertical)
        empty_lbl = self.query_one("#quota-empty", Static)

        valid_items: list[tuple[str, float]] = []
        for bid, info in sorted(quotas.items()):
            frac = info.get("remaining_fraction")
            if frac is not None:
                valid_items.append((bid, max(0.0, min(100.0, float(frac) * 100.0))))

        if not valid_items:
            empty_lbl.display = True
            for gauge in self._quota_gauges.values():
                gauge.display = False
            return

        empty_lbl.display = False
        seen: set[str] = set()
        for bid, pct in valid_items:
            seen.add(bid)
            label = human_quota_label(bid, self.lang)
            sev = "ok" if pct >= 50.0 else ("warn" if pct >= 20.0 else "danger")
            gauge = self._quota_gauges.get(bid)
            if gauge is None:
                gauge = MetricGauge(
                    label,
                    f"{int(round(pct))}%",
                    progress=pct,
                    severity=sev,
                    id=f"quota-{_safe_widget_id(bid)}",
                )
                self._quota_gauges[bid] = gauge
                container.mount(gauge)
            else:
                gauge.display = True
                gauge.set_state(pct, f"{int(round(pct))}%", label=label, severity=sev)

        for bid, gauge in self._quota_gauges.items():
            if bid not in seen:
                gauge.display = False


# ---------------------------------------------------------------------------
# Slash Autocomplete Suggester & Ctrl+P Command Palette Provider
# ---------------------------------------------------------------------------


class SlashCommandSuggester(Suggester):
    """Inline ghost-text autocompletion for `/` commands and their arguments."""

    def __init__(self, app_ref: OptigravityApp) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._app_ref = app_ref

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None
        candidates = self._app_ref.get_slash_completions(value)
        if not candidates:
            return None
        top_insert = candidates[0][0]
        if top_insert.lower().startswith(value.lower()) and len(top_insert) > len(value):
            return top_insert
        return None


class OptigravityCommandProvider(Provider):
    """Exposes all Optigravity TUI commands, models, efforts, modes, and languages in `Ctrl+P`."""

    def _build_entries(self) -> list[tuple[str, str, Any]]:
        app = self.app
        if not isinstance(app, OptigravityApp):
            return []
        lang = app.lang

        entries: list[tuple[str, str, Any]] = [
            (t(lang, "pal_glossary"), t(lang, "pal_glossary_help"), app.action_show_glossary),
            (t(lang, "pal_rotate"), t(lang, "pal_rotate_help"), app.action_accept_rotation),
            (t(lang, "pal_handoff"), t(lang, "pal_handoff_help"), lambda: app._run_palette_cmd("/handoff")),
            (t(lang, "pal_new"), t(lang, "pal_new_help"), app.action_new_session),
            (t(lang, "pal_sessions"), t(lang, "pal_sessions_help"), lambda: app._run_palette_cmd("/sessions")),
            (t(lang, "pal_cancel"), t(lang, "pal_cancel_help"), app.action_cancel_turn),
            (t(lang, "pal_telemetry"), t(lang, "pal_telemetry_help"), app.action_toggle_telemetry),
            (t(lang, "pal_help"), t(lang, "pal_help_help"), lambda: app._run_palette_cmd("/help")),
        ]

        if app.active_proposals:
            top = app.active_proposals[0]
            entries.insert(
                0,
                (
                    t(lang, "pal_proposal", title=top.title),
                    top.detail,
                    app.action_accept_proposal,
                ),
            )

        for code, label in (("fr", "Français (fr)"), ("en", "English (en)")):
            entries.append(
                (
                    t(lang, "pal_lang", label=label),
                    t(lang, "pal_lang_help", label=label),
                    lambda c=code: app._run_palette_cmd(f"/lang {c}"),
                )
            )

        for model in app.get_available_models():
            entries.append(
                (
                    t(lang, "pal_model", val=model),
                    t(lang, "pal_model_help", val=model),
                    lambda m=model: app._run_palette_cmd(f"/model {m}"),
                )
            )
        for effort in app.get_available_efforts():
            entries.append(
                (
                    t(lang, "pal_effort", val=effort),
                    t(lang, "pal_effort_help", val=effort),
                    lambda e=effort: app._run_palette_cmd(f"/effort {e}"),
                )
            )
        for mode in app.get_available_modes():
            entries.append(
                (
                    t(lang, "pal_mode", val=mode),
                    t(lang, "pal_mode_help", val=mode),
                    lambda md=mode: app._run_palette_cmd(f"/mode {md}"),
                )
            )
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


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------


class OptigravityApp(App[None]):
    """Interactive Textual TUI for Optigravity V2 (`agy acp`)."""

    TITLE = "Optigravity"
    SUB_TITLE = "ACP"
    COMMANDS = App.COMMANDS | {OptigravityCommandProvider}

    CSS = """
    Screen {
        background: #0e1116;
        color: #e6edf3;
    }
    Header {
        background: #131720;
        color: #c9d1d9;
        height: 1;
        dock: top;
    }
    Footer {
        background: #131720;
        color: #8b949e;
    }
    #session-bar {
        height: 2;
        background: #121620;
        color: #d0d7de;
        padding: 0 2;
        border-bottom: solid #262e3d;
    }
    #proposal-bar {
        height: auto;
        background: #231d12;
        border-bottom: solid #523d14;
        padding: 0 2;
        display: none;
        align: left middle;
    }
    #proposal-bar.visible {
        display: block;
    }
    #proposal-text {
        width: 1fr;
        color: #f0c674;
        padding: 0 1 0 0;
    }
    #proposal-apply-btn {
        height: 1;
        min-width: 12;
        border: none;
        background: #3b2e13;
        color: #f0c674;
    }
    #main-body {
        height: 1fr;
    }
    #stream-scroll {
        width: 1fr;
        padding: 1 2;
        background: #0e1116;
        scrollbar-size: 1 1;
        scrollbar-color: #2a3344;
        scrollbar-color-hover: #485263;
        scrollbar-background: #0e1116;
    }
    #stream-scroll Markdown {
        margin: 0 0 1 0;
        padding: 0 1;
        background: transparent;
    }
    #stream-scroll Collapsible {
        border: none;
        background: transparent;
        padding: 0;
        margin: 0 0 1 0;
    }
    #stream-scroll CollapsibleTitle {
        color: #6e7681;
        padding: 0 1;
    }
    .user-msg {
        border-left: thick #6ea8fe;
        background: #141a24;
        color: #e6edf3;
        padding: 0 1;
        margin: 1 0;
    }
    .tool-line {
        border-left: outer #303a4b;
        background: transparent;
        color: #8b949e;
        padding: 0 1;
        margin: 0;
    }
    .tool-running {
        border-left: outer #6ea8fe;
        color: #c9d1d9;
    }
    .tool-ok {
        border-left: outer #3fb950;
        color: #8b949e;
    }
    .tool-err {
        border-left: outer #f85149;
        color: #f85149;
    }
    .notice-line {
        border-left: outer #6ea8fe;
        background: #131720;
        color: #c9d1d9;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    .notice-warn {
        border-left: outer #d29922;
        color: #e3b341;
    }
    .notice-err {
        border-left: outer #f85149;
        color: #f85149;
    }
    #activity-bar {
        display: none;
        height: 1;
        padding: 0 2;
        background: #111622;
        color: #6ea8fe;
    }
    #activity-bar.visible {
        display: block;
    }
    #slash-popup {
        display: none;
        height: auto;
        max-height: 9;
        margin: 0 2;
        border: solid #2a3344;
        background: #151922;
        color: #c9d1d9;
        scrollbar-size: 1 1;
    }
    #slash-popup.visible {
        display: block;
    }
    #input-row {
        height: auto;
        padding: 0 2 1 2;
        background: #0e1116;
        border-top: solid #222936;
        align: left middle;
    }
    #prompt-input {
        width: 1fr;
        background: #151922;
        border: solid #2a3344;
        color: #e6edf3;
    }
    #prompt-input:focus {
        border: solid #6ea8fe;
    }
    #send-btn {
        min-width: 10;
        margin-left: 1;
        background: #1f2633;
        color: #e6edf3;
        border: solid #303a4b;
    }
    #send-btn:hover {
        background: #2a3446;
    }
    #telemetry-panel.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_turn", "Cancel", show=True, priority=True),
        Binding("ctrl+n", "new_session", "New", show=True),
        Binding("ctrl+r", "accept_rotation", "Rotate", show=True),
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
        self._popup_values: list[str] = []
        self._routed_sessions: set[str] = set()
        self._manual_override_sessions: set[str] = set()

        # Working spinner state
        self._working: bool = False
        self._working_label: str = ""
        self._working_since: float = 0.0
        self._spinner_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(t(self.lang, "connecting"), id="session-bar", markup=False)
        with Horizontal(id="proposal-bar"):
            yield Static("", id="proposal-text", markup=False)
            yield Button(t(self.lang, "apply"), id="proposal-apply-btn")
        with Horizontal(id="main-body"):
            yield VerticalScroll(id="stream-scroll")
            yield TelemetryPanel(lang=self.lang, id="telemetry-panel")
        yield Static("", id="activity-bar", markup=False)
        yield OptionList(id="slash-popup")
        with Horizontal(id="input-row"):
            yield Input(
                placeholder=t(self.lang, "input_placeholder"),
                suggester=SlashCommandSuggester(self),
                id="prompt-input",
            )
            yield Button(t(self.lang, "send"), id="send-btn")
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(5.0, self._tick_telemetry)
        self.set_interval(0.12, self._tick_spinner)
        self.query_one("#prompt-input", Input).focus()
        if self.auto_connect:
            self._boot_acp()

    async def on_unmount(self) -> None:
        for task in list(self._inflight_tasks):
            task.cancel()
        try:
            await self.client.close()
        except Exception:
            pass

    def _tick_telemetry(self) -> None:
        if self.current_state is not None:
            self._refresh_ui()

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
        if not self._working:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(SPINNER_FRAMES)
        elapsed = max(0.0, time.monotonic() - self._working_since)
        frame = SPINNER_FRAMES[self._spinner_idx]
        self.query_one("#activity-bar", Static).update(f"{frame} {self._working_label} ({elapsed:.1f}s)")

    def _apply_language(self, new_lang: str) -> None:
        self.lang = new_lang if new_lang in SUPPORTED_LANGS else "fr"
        self.query_one("#prompt-input", Input).placeholder = t(self.lang, "input_placeholder")
        self.query_one("#send-btn", Button).label = t(self.lang, "send")
        self.query_one("#proposal-apply-btn", Button).label = t(self.lang, "apply")
        self.query_one("#telemetry-panel", TelemetryPanel).apply_lang(self.lang)
        self._refresh_ui()

    @work(exclusive=True)
    async def _boot_acp(self) -> None:
        try:
            await self.client.connect(agent_conn=self._injected_agent_conn)
            sid = await self.client.new_session(self.client_config.cwd)
            self._ensure_state(sid)
            await self._apply_initial_profile(sid)
            self._refresh_ui()
            if self.initial_prompt:
                await self._dispatch_prompt(self.initial_prompt)
        except AcpAuthError as exc:
            await self._append_notice(t(self.lang, "auth_required", err=exc), level="err")
        except Exception as exc:
            await self._append_notice(t(self.lang, "acp_start_failed", err=exc), level="err")

    async def _set_model_and_effort(self, session_id: str, model_id: str, effort: str) -> tuple[str, str]:
        """Safely apply model and effort via ACP `session/set_config_option`."""
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
        """Apply CLI override (--model/--effort) or frugal default (gemini-4-hydrogen / medium)."""
        if self.initial_model or self.initial_effort:
            self._manual_override_sessions.add(session_id)
            target_model = self.initial_model or DEFAULT_FRUGAL_MODEL
            target_effort = self.initial_effort or DEFAULT_FRUGAL_EFFORT
            await self._set_model_and_effort(session_id, target_model, target_effort)
            return
        await self._set_model_and_effort(session_id, DEFAULT_FRUGAL_MODEL, DEFAULT_FRUGAL_EFFORT)

    async def _maybe_auto_route_first_prompt(self, session_id: str, prompt: str) -> None:
        """Run Stage 1 router on the 1st prompt (context == 0 -> 0 Switch Tax) unless manually overridden."""
        state = self._ensure_state(session_id)
        if (
            session_id in self._routed_sessions
            or session_id in self._manual_override_sessions
            or state.turn_count > 0
            or state.context_tokens > 0
        ):
            return

        self._routed_sessions.add(session_id)

        min_quota: float | None = None
        try:
            quotas = self.orchestrator.read_quota_snapshots()
            fracs = [
                float(info["remaining_fraction"])
                for info in quotas.values()
                if info.get("remaining_fraction") is not None
            ]
            if fracs:
                min_quota = min(fracs)
        except Exception:
            pass

        decision = route_stage1(
            prompt,
            has_test_runner=_detect_has_test_runner(self.client_config.cwd),
            remaining_quota_fraction=min_quota,
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
                t(
                    self.lang,
                    "auto_routed",
                    category=decision.category,
                    model=applied_model,
                    effort=applied_effort,
                )
            )

        # Persist routing decision for offline `ogy router-eval` (best-effort)
        try:
            import json as _json

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
                        (
                            session_id,
                            decision.stage,
                            decision.category,
                            _json.dumps(decision.features),
                            applied_model,
                            applied_effort,
                            state.mode,
                        ),
                    )
            finally:
                conn.close()
        except Exception:
            pass

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

    # ------------------------------------------------------------------
    # Dynamic config choices & slash completion
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
        """Return list of `(insert_text, display_label)` pairs for the current slash input."""
        if not raw_value.startswith("/"):
            return []

        if " " not in raw_value:
            prefix = raw_value.lower()
            matches: list[tuple[str, str]] = []
            for spec in SLASH_COMMANDS:
                if spec.name.startswith(prefix):
                    insert = f"{spec.name} " if spec.takes_arg else spec.name
                    desc = t(self.lang, spec.desc_key)
                    matches.append((insert, f"{spec.usage:<20}  {desc}"))
            return matches

        cmd_part, arg_part = raw_value.split(" ", 1)
        cmd = cmd_part.lower()
        arg_prefix = arg_part.strip().lower()

        if cmd == "/model":
            return [
                (f"/model {m}", f"/model {m}")
                for m in self.get_available_models()
                if m.lower().startswith(arg_prefix)
            ]
        if cmd == "/effort":
            return [
                (f"/effort {e}", f"/effort {e}")
                for e in self.get_available_efforts()
                if e.lower().startswith(arg_prefix)
            ]
        if cmd == "/mode":
            return [
                (f"/mode {md}", f"/mode {md}")
                for md in self.get_available_modes()
                if md.lower().startswith(arg_prefix)
            ]
        if cmd == "/lang":
            return [
                (f"/lang {code}", f"/lang {code}  ({lbl})")
                for code, lbl in (("fr", "Français"), ("en", "English"))
                if code.startswith(arg_prefix)
            ]
        if cmd == "/resume":
            return [
                (f"/resume {sid}", f"/resume {sid[:12]} ({st.model_id})")
                for sid, st in self.sessions.items()
                if sid.lower().startswith(arg_prefix)
            ]
        return []

    def _update_slash_popup(self, value: str) -> None:
        popup = self.query_one("#slash-popup", OptionList)
        candidates = self.get_slash_completions(value)
        if not value.startswith("/") or not candidates:
            self._popup_values = []
            popup.clear_options()
            popup.remove_class("visible")
            return

        self._popup_values = [insert for insert, _ in candidates]
        popup.clear_options()
        for idx, (_, label) in enumerate(candidates):
            popup.add_option(Option(label, id=f"slash-{idx}"))
        popup.highlighted = 0
        popup.add_class("visible")

    def _hide_slash_popup(self) -> None:
        popup = self.query_one("#slash-popup", OptionList)
        self._popup_values = []
        popup.clear_options()
        popup.remove_class("visible")

    @on(Input.Changed, "#prompt-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._update_slash_popup(event.value)

    @on(OptionList.OptionSelected, "#slash-popup")
    def _on_slash_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._popup_values):
            chosen = self._popup_values[idx]
            inp = self.query_one("#prompt-input", Input)
            inp.value = chosen
            inp.cursor_position = len(chosen)
            inp.focus()
            self._update_slash_popup(chosen)

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        try:
            inp = self.query_one("#prompt-input", Input)
            popup = self.query_one("#slash-popup", OptionList)
        except Exception:
            return
        if not inp.has_focus:
            return
        if not popup.has_class("visible") or not self._popup_values:
            return

        if event.key == "tab":
            idx = popup.highlighted if popup.highlighted is not None else 0
            if 0 <= idx < len(self._popup_values):
                chosen = self._popup_values[idx]
                inp.value = chosen
                inp.cursor_position = len(chosen)
                self._update_slash_popup(chosen)
                event.prevent_default()
                event.stop()
        elif event.key == "down":
            popup.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            popup.action_cursor_up()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self._hide_slash_popup()
            event.prevent_default()
            event.stop()

    # ------------------------------------------------------------------
    # ACP Event Handlers (strictly chronological mounting)
    # ------------------------------------------------------------------

    def _close_active_message(self) -> None:
        self._active_msg_widget = None
        self._active_msg_buffer = ""

    def _close_active_thought(self) -> None:
        self._active_thought_body = None
        self._active_thought_box = None
        self._active_thought_buffer = ""

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
                PermissionModal(event, lang=self.lang),
                callback=lambda choice: event.future.done() or event.future.set_result(choice),
            )
        elif isinstance(event, ElicitationRequestEvent):
            if not event.future.done():
                event.future.set_result({"action": "decline"})
        elif isinstance(event, SessionNoticeEvent):
            await self._append_notice(event.message)

    async def _handle_message_chunk(self, event: AgentMessageEvent) -> None:
        self._close_active_thought()
        self._set_working(True, t(self.lang, "working_responding"))
        stream = self.query_one("#stream-scroll", VerticalScroll)
        if self._active_msg_widget is None:
            self._active_msg_buffer = event.text
            md = Markdown(_clean_file_links(self._active_msg_buffer))
            self._active_msg_widget = md
            await stream.mount(md)
        else:
            self._active_msg_buffer += event.text
            await self._active_msg_widget.update(_clean_file_links(self._active_msg_buffer))
        stream.scroll_end(animate=False)

    async def _handle_thought_chunk(self, event: AgentThoughtEvent) -> None:
        self._close_active_message()
        self._set_working(True, t(self.lang, "working_thinking"))
        stream = self.query_one("#stream-scroll", VerticalScroll)
        if self._active_thought_body is None or self._active_thought_box is None:
            self._active_thought_buffer = event.text
            body = Static(self._active_thought_buffer, markup=False)
            box = Collapsible(
                body,
                title=t(self.lang, "thinking_title", chars=len(self._active_thought_buffer)),
                collapsed=True,
            )
            self._active_thought_body = body
            self._active_thought_box = box
            await stream.mount(box)
        else:
            self._active_thought_buffer += event.text
            self._active_thought_body.update(self._active_thought_buffer)
            self._active_thought_box.title = t(self.lang, "thinking_title", chars=len(self._active_thought_buffer))

    async def _handle_tool_call(self, event: ToolCallEvent) -> None:
        stream = self.query_one("#stream-scroll", VerticalScroll)
        state = self._ensure_state(event.session_id)

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

        if event.is_error or event.status in ("failed", "error"):
            icon, css_cls = "✗", "tool-line tool-err"
        elif event.status == "completed":
            icon, css_cls = "✓", "tool-line tool-ok"
        else:
            icon, css_cls = "●", "tool-line tool-running"

        title_part = f" · {event.title}" if event.title and event.title != event.name else ""
        label = f"{icon} {event.name}{title_part}"
        card = self._tool_widgets.get(event.tool_call_id)
        if card is None:
            # Close any open message/thought block so post-tool text mounts *below* this tool
            self._close_active_message()
            self._close_active_thought()
            card = Static(label, classes=css_cls, markup=False)
            self._tool_widgets[event.tool_call_id] = card
            await stream.mount(card)
        else:
            card.update(label)
            card.set_classes(css_cls)
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
            state.last_turn_monotonic = time.monotonic()
            state.input_tokens += event.input_tokens
            state.output_tokens += event.output_tokens
            state.thought_tokens += event.thought_tokens
            state.cached_read_tokens += event.cached_read_tokens
            state.cached_write_tokens += event.cached_write_tokens
            if event.cost_usd is not None:
                state.total_cost_usd += event.cost_usd
            self._close_active_message()
            self._close_active_thought()
            self._set_working(False)
        elif event.cost_usd is not None:
            state.total_cost_usd = max(state.total_cost_usd, event.cost_usd)
        self._refresh_ui()

    async def _append_notice(self, message: str, *, level: str = "info") -> None:
        self._close_active_message()
        self._close_active_thought()
        stream = self.query_one("#stream-scroll", VerticalScroll)
        css_cls = "notice-line"
        if level == "warn":
            css_cls = "notice-line notice-warn"
        elif level == "err":
            css_cls = "notice-line notice-err"
        await stream.mount(Static(f"ogy · {message}", classes=css_cls, markup=False))
        stream.scroll_end(animate=False)

    def _refresh_ui(self) -> None:
        state = self.current_state
        bar = self.query_one("#session-bar", Static)
        prop_bar = self.query_one("#proposal-bar", Horizontal)
        prop_text = self.query_one("#proposal-text", Static)
        if state is None:
            bar.update(t(self.lang, "session_none"))
            prop_bar.remove_class("visible")
            return

        bar.update(
            t(
                self.lang,
                "session_bar",
                sid=state.session_id[:12],
                count=len(self.sessions),
                model=state.model_id,
                effort=state.effort,
                mode=state.mode,
            )
        )
        try:
            quotas = self.orchestrator.read_quota_snapshots()
        except Exception:
            quotas = {}
        self.query_one("#telemetry-panel", TelemetryPanel).refresh_state(state, quotas)

        self.active_proposals = self.orchestrator.evaluate_proposals(state)
        if self.active_proposals:
            top = self.active_proposals[0]
            prop_text.update(f"{top.title} — {top.detail}")
            prop_bar.add_class("visible")
        else:
            prop_bar.remove_class("visible")

    # ------------------------------------------------------------------
    # Input & Slash Commands
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#send-btn")
    async def _on_send_pressed(self) -> None:
        await self._submit_input()

    @on(Button.Pressed, "#proposal-apply-btn")
    async def _on_proposal_apply_pressed(self) -> None:
        await self.action_accept_proposal()

    @on(Input.Submitted, "#prompt-input")
    async def _on_input_submitted(self) -> None:
        await self._submit_input()

    async def _submit_input(self) -> None:
        inp = self.query_one("#prompt-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.value = ""
        self._hide_slash_popup()

        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        # Concurrent steering: launch prompt as a background task so a second prompt can steer in flight
        task = asyncio.create_task(self._dispatch_prompt(text))
        self._inflight_tasks.add(task)
        task.add_done_callback(self._inflight_tasks.discard)

    async def _dispatch_prompt(self, text: str, *, display_text: str | None = None) -> None:
        self._close_active_message()
        self._close_active_thought()
        stream = self.query_one("#stream-scroll", VerticalScroll)
        await stream.mount(Static(f"› {display_text or text}", classes="user-msg", markup=False))
        stream.scroll_end(animate=False)

        state = self.current_state
        if state is not None and display_text is None:
            state.active_goal = text[:200]
            await self._maybe_auto_route_first_prompt(state.session_id, text)
        self._set_working(True, t(self.lang, "working_sending"))
        try:
            await self.client.send_prompt(text)
        except Exception as exc:
            await self._append_notice(t(self.lang, "prompt_error", err=exc), level="err")
        finally:
            self._set_working(False)

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

        if cmd == "/lang":
            if not arg or arg.lower() not in SUPPORTED_LANGS:
                await self._append_notice(t(self.lang, "lang_status", cur=self.lang))
                return
            new_lang = arg.lower()
            self._apply_language(new_lang)
            await self._append_notice(t(self.lang, "lang_changed", val=new_lang))
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
                avail = ", ".join(self.get_available_models())
                await self._append_notice(t(self.lang, "model_status", cur=cur, avail=avail))
                return
            compat = self.orchestrator.check_model_compatibility(arg)
            if compat is not None:
                await self._append_notice(f"{compat.title} : {compat.detail}", level="warn")
                return
            await self.client.set_config_option("model", arg)
            if self.client.active_session_id:
                self._manual_override_sessions.add(self.client.active_session_id)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "model_changed", val=arg))
            return

        if cmd == "/effort":
            if not arg:
                cur = state.effort if state else "--"
                avail = ", ".join(self.get_available_efforts())
                await self._append_notice(t(self.lang, "effort_status", cur=cur, avail=avail))
                return
            sid = self.client.active_session_id
            cfg = self.client.session_configs.get(sid or "", {})
            opt_id = "thought_level" if ("thought_level" in cfg and "thinking_level" not in cfg) else "thinking_level"
            await self.client.set_config_option(opt_id, arg)
            if sid:
                self._manual_override_sessions.add(sid)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "effort_changed", val=arg))
            return

        if cmd == "/mode":
            if not arg:
                cur = state.mode if state else "--"
                avail = ", ".join(self.get_available_modes())
                await self._append_notice(t(self.lang, "mode_status", cur=cur, avail=avail))
                return
            try:
                await self.client.set_config_option("mode", arg)
                self._refresh_ui()
                await self._append_notice(t(self.lang, "mode_changed", val=arg))
            except ValueError as exc:
                await self._append_notice(str(exc), level="warn")
            return

        if cmd in ("/rotate", "/clear"):
            await self.action_accept_rotation()
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
            rows = await self.client.list_sessions()
            ids = [str(r.get("session_id", ""))[:12] for r in rows[:8]]
            await self._append_notice(
                t(self.lang, "sessions_list", ids=", ".join(ids) or t(self.lang, "sessions_none"))
            )
            return

        if cmd == "/resume":
            if not arg:
                await self._append_notice(t(self.lang, "resume_usage"), level="warn")
                return
            await self.client.resume_session(arg)
            self._ensure_state(arg)
            self._refresh_ui()
            await self._append_notice(t(self.lang, "session_resumed", sid=arg[:12]))
            return

        await self._append_notice(t(self.lang, "cmd_unknown", cmd=rich_escape(cmd_line)), level="warn")

    # ------------------------------------------------------------------
    # Actions & Keybindings
    # ------------------------------------------------------------------

    def action_show_glossary(self) -> None:
        if isinstance(self.screen, GlossaryModal):
            self.pop_screen()
        else:
            self.push_screen(GlossaryModal(lang=self.lang))

    def action_toggle_telemetry(self) -> None:
        panel = self.query_one("#telemetry-panel", TelemetryPanel)
        panel.toggle_class("hidden")

    async def action_cancel_turn(self) -> None:
        await self.client.cancel_turn()
        self._set_working(False)
        await self._append_notice(t(self.lang, "turn_cancelled"), level="warn")

    async def action_new_session(self) -> None:
        if not self.client.is_connected:
            return
        sid = await self.client.new_session(self.client_config.cwd)
        self._ensure_state(sid)
        await self._apply_initial_profile(sid)
        self._refresh_ui()
        await self._append_notice(t(self.lang, "session_started", sid=sid[:12]))

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
        await self._append_notice(t(self.lang, "session_rotated", sid=new_sid[:12], path=path))
        await self._dispatch_prompt(f"Continue from session handoff:\n\n{content}")

    async def action_accept_proposal(self) -> None:
        if not self.active_proposals:
            return
        prop = self.active_proposals[0]
        if prop.kind == "rotate_session":
            await self.action_accept_rotation()
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
                t(
                    self.lang,
                    "upgrade_applied",
                    model=prop.target_model,
                    effort=prop.target_effort,
                    tax=prop.switch_tax_usd,
                )
            )
        elif prop.kind == "delegate_subagent":
            await self._append_notice(
                t(
                    self.lang,
                    "delegate_hint",
                    model=prop.target_model,
                    effort=prop.target_effort,
                    tax=prop.switch_tax_usd,
                ),
                level="warn",
            )


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
    app = OptigravityApp(
        client_config=cfg,
        initial_prompt=initial_prompt,
        initial_model=model,
        initial_effort=effort,
        lang=lang,
    )
    app.run()
    return 0
