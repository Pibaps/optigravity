"""Keyboard-first modals: PermissionModal, ElicitationModal (form+url), SessionPickerModal, HandoffReviewModal, HelpGlossaryModal."""

from __future__ import annotations

import json
import webbrowser
from typing import Any
from urllib.parse import urlparse

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, Select, Static, Switch, TextArea
from textual.widgets.option_list import Option

from ogy_frontends.acp_tui.client import ElicitationRequestEvent, PermissionRequestEvent
from ogy_frontends.acp_tui.i18n import GLOSSARY_MARKDOWN, t
from ogy_frontends.acp_tui.widgets.transcript import build_colored_unified_diff, extract_diff_blocks

_HIGH_RISK_TOOLS = frozenset({"execute", "delete", "run_command", "bash", "shell", "powershell", "rm"})
_MED_RISK_TOOLS = frozenset({"edit", "write", "edit_file", "write_file", "write_to_file", "replace_file_content"})


class DiffPreviewModal(ModalScreen[None]):
    """Fullscreen preview modal for large diffs, commands, or pasted blocks."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("f", "close", "Close", show=False),
    ]

    def __init__(self, title: str, content: Text | str, *, lang: str = "fr") -> None:
        super().__init__()
        self._title = title
        self._content = content
        self.lang = lang

    def compose(self) -> ComposeResult:
        with Container(id="diff-fullscreen-dialog"):
            yield Static(self._title, classes="modal-title", markup=False)
            with VerticalScroll(id="diff-fullscreen-scroll"):
                yield Static(self._content)
            with Horizontal(classes="modal-buttons"):
                yield Button(t(self.lang, "glossary_close"), id="diff-close-btn")

    @on(Button.Pressed, "#diff-close-btn")
    def action_close(self) -> None:
        self.dismiss(None)


class PermissionModal(ModalScreen[str | None]):
    """Keyboard-first modal for `session/request_permission` with risk badge, diff/command preview, and 1-key shortcuts."""

    BINDINGS = [
        Binding("y", "allow_once", "Allow once", show=False, priority=True),
        Binding("1", "allow_once", "Allow once", show=False, priority=True),
        Binding("a", "allow_always", "Allow always", show=False, priority=True),
        Binding("2", "allow_always", "Allow always", show=False, priority=True),
        Binding("n", "reject_once", "Reject once", show=False, priority=True),
        Binding("escape", "reject_once", "Cancel", show=False, priority=True),
        Binding("x", "reject_always", "Reject always", show=False, priority=True),
        Binding("f", "fullscreen", "Fullscreen", show=False, priority=True),
    ]

    def __init__(self, event: PermissionRequestEvent, *, lang: str = "fr") -> None:
        super().__init__()
        self.event = event
        self.lang = lang
        self._preview_renderable: Text | str = ""

    def _risk_level(self) -> tuple[str, str]:
        kind = (self.event.kind or "").lower()
        name = (self.event.tool_name or "").lower()
        if kind in ("execute", "delete") or name in _HIGH_RISK_TOOLS:
            return t(self.lang, "perm_risk_high"), "risk-high"
        if kind in ("edit", "write") or name in _MED_RISK_TOOLS or extract_diff_blocks(self.event.content, self.event.raw_input):
            return t(self.lang, "perm_risk_med"), "risk-med"
        return t(self.lang, "perm_risk_low"), "risk-low"

    def _build_preview(self) -> Text | str:
        diffs = extract_diff_blocks(self.event.content, self.event.raw_input)
        if diffs:
            out = Text()
            for path, old_t, new_t in diffs:
                r_diff, _, _, _ = build_colored_unified_diff(path, old_t, new_t)
                out.append_text(r_diff)
            return out
        raw = self.event.raw_input
        if isinstance(raw, dict):
            cmd = raw.get("CommandLine") or raw.get("command") or raw.get("cmd")
            if cmd:
                return Text(f"$ {cmd}", style="bold #f0c674")
            return Text(json.dumps(raw, indent=2, ensure_ascii=False))
        if raw is not None:
            return Text(str(raw))
        return Text(self.event.title)

    def _resolve_option(self, target_kind: str) -> str | None:
        for opt_id, _, kind in self.event.options:
            k = kind.lower()
            if k == target_kind:
                return opt_id
        if target_kind == "allow_once":
            for opt_id, _, kind in self.event.options:
                if "allow" in kind.lower() and "always" not in kind.lower():
                    return opt_id
            for opt_id, _, kind in self.event.options:
                if "allow" in kind.lower():
                    return opt_id
            return "allow_once"
        if target_kind == "allow_always":
            for opt_id, _, kind in self.event.options:
                if "allow" in kind.lower() and "always" in kind.lower():
                    return opt_id
            return self._resolve_option("allow_once")
        if target_kind == "reject_always":
            for opt_id, _, kind in self.event.options:
                if "reject" in kind.lower() and "always" in kind.lower():
                    return opt_id
            return None
        # reject_once
        for opt_id, _, kind in self.event.options:
            if "reject" in kind.lower() and "always" not in kind.lower():
                return opt_id
        return None

    def compose(self) -> ComposeResult:
        risk_label, risk_cls = self._risk_level()
        self._preview_renderable = self._build_preview()
        with Container(classes="modal-dialog", id="perm-dialog"):
            with Horizontal(classes="modal-title"):
                yield Static(f"[{risk_label}] ", classes=risk_cls, markup=False)
                yield Static(t(self.lang, "perm_title", tool=self.event.tool_name), id="perm-title", markup=False)
            yield Static(self.event.title, id="perm-detail", markup=False)
            with VerticalScroll(classes="modal-scroll", id="perm-preview-scroll"):
                yield Static(self._preview_renderable, id="perm-preview")
            yield Static(t(self.lang, "perm_shortcuts"), id="perm-shortcuts", markup=False)
            yield Static(t(self.lang, "perm_hint"), id="perm-hint", markup=False)
            with Horizontal(classes="modal-buttons", id="perm-buttons"):
                if self.event.options:
                    for opt_id, opt_name, kind in self.event.options:
                        variant = "success" if "allow" in kind else "error"
                        yield Button(opt_name, id=f"opt-{opt_id}", variant=variant)
                else:
                    yield Button(t(self.lang, "perm_allow"), id="opt-allow_once", variant="success")
                    yield Button(t(self.lang, "perm_reject"), id="opt-reject_once", variant="error")

    def action_allow_once(self) -> None:
        self.dismiss(self._resolve_option("allow_once"))

    def action_allow_always(self) -> None:
        self.dismiss(self._resolve_option("allow_always"))

    def action_reject_once(self) -> None:
        self.dismiss(self._resolve_option("reject_once"))

    def action_reject_always(self) -> None:
        self.dismiss(self._resolve_option("reject_always"))

    def action_fullscreen(self) -> None:
        self.app.push_screen(DiffPreviewModal(self.event.title, self._preview_renderable, lang=self.lang))

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("opt-"):
            chosen = btn_id[4:]
            if "reject" in chosen and not any(o[0] == chosen for o in self.event.options):
                self.dismiss(None)
            else:
                self.dismiss(chosen)
        else:
            self.dismiss(None)


class ElicitationModal(ModalScreen[dict[str, Any]]):
    """ACP Elicitation modal supporting both dynamic `form` schemas and explicit-consent `url` flows."""

    BINDINGS = [
        Binding("escape", "cancel_elicit", "Cancel", show=False, priority=True),
        Binding("ctrl+enter", "accept_elicit", "Submit", show=False, priority=True),
    ]

    def __init__(self, event: ElicitationRequestEvent, *, lang: str = "fr") -> None:
        super().__init__()
        self.event = event
        self.lang = lang
        self._field_specs: dict[str, dict[str, Any]] = {}
        self._widget_ids: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog", id="elicit-dialog"):
            if self.event.mode == "url":
                yield Static(t(self.lang, "elicit_url_title"), classes="modal-title", markup=False)
                yield Static(self.event.message, id="elicit-message", markup=False)
                url = self.event.url or ""
                host = urlparse(url).netloc or url
                yield Static(t(self.lang, "elicit_url_host", host=host), id="elicit-url-host", classes="risk-med", markup=False)
                yield Static(url, id="elicit-url-raw", markup=False)
                yield Static(t(self.lang, "elicit_url_consent"), id="elicit-url-consent", markup=False)
                with Horizontal(classes="modal-buttons"):
                    yield Button(t(self.lang, "elicit_url_open"), id="elicit-open-url", variant="primary")
                    yield Button(t(self.lang, "elicit_url_copy"), id="elicit-copy-url")
                    yield Button(t(self.lang, "elicit_decline"), id="elicit-decline", variant="error")
                    yield Button(t(self.lang, "elicit_cancel"), id="elicit-cancel")
            else:
                yield Static(t(self.lang, "elicit_form_title"), classes="modal-title", markup=False)
                yield Static(self.event.message, id="elicit-message", markup=False)
                schema_dict = self.event.requested_schema or {}
                props = schema_dict.get("properties") if isinstance(schema_dict, dict) else {}
                required = set(schema_dict.get("required") or []) if isinstance(schema_dict, dict) else set()
                with VerticalScroll(classes="modal-scroll", id="elicit-form-fields"):
                    if isinstance(props, dict):
                        for idx, (fname, fspec) in enumerate(props.items()):
                            spec = fspec if isinstance(fspec, dict) else {}
                            self._field_specs[fname] = spec
                            wid = f"elicit-f-{idx}"
                            self._widget_ids[fname] = wid
                            title = str(spec.get("title") or fname)
                            if fname in required:
                                title += " *"
                            yield Label(title, markup=False)
                            ftype = str(spec.get("type") or "string")
                            enum_vals = spec.get("enum")
                            if ftype == "boolean":
                                yield Switch(value=bool(spec.get("default", False)), id=wid)
                            elif isinstance(enum_vals, list) and enum_vals:
                                opts = [(str(v), str(v)) for v in enum_vals]
                                default_v = str(spec["default"]) if "default" in spec else Select.BLANK
                                yield Select(opts, value=default_v, id=wid)
                            else:
                                default_s = str(spec.get("default") or "")
                                desc = str(spec.get("description") or "")
                                yield Input(value=default_s, placeholder=desc, id=wid)
                with Horizontal(classes="modal-buttons"):
                    yield Button(t(self.lang, "elicit_accept"), id="elicit-accept", variant="success")
                    yield Button(t(self.lang, "elicit_decline"), id="elicit-decline", variant="error")
                    yield Button(t(self.lang, "elicit_cancel"), id="elicit-cancel")

    def complete_from_server(self) -> None:
        """Auto-dismiss URL elicitation upon receiving `elicitation/complete`."""
        self.dismiss({"action": "accept", "content": {}})

    def _collect_form_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for fname, wid in self._widget_ids.items():
            spec = self._field_specs.get(fname, {})
            ftype = str(spec.get("type") or "string")
            widget = self.query_one(f"#{wid}")
            if isinstance(widget, Switch):
                values[fname] = bool(widget.value)
            elif isinstance(widget, Select):
                if widget.value != Select.BLANK:
                    values[fname] = str(widget.value)
            elif isinstance(widget, Input):
                raw = widget.value
                if ftype == "integer":
                    try:
                        values[fname] = int(raw)
                    except ValueError:
                        values[fname] = raw
                elif ftype == "number":
                    try:
                        values[fname] = float(raw)
                    except ValueError:
                        values[fname] = raw
                else:
                    values[fname] = raw
        return values

    def action_accept_elicit(self) -> None:
        if self.event.mode == "url":
            # Never fall back to a form for URL mode; open requires explicit button click
            return
        self.dismiss({"action": "accept", "content": self._collect_form_values()})

    def action_cancel_elicit(self) -> None:
        self.dismiss({"action": "cancel"})

    @on(Button.Pressed, "#elicit-accept")
    def _on_accept_btn(self) -> None:
        self.action_accept_elicit()

    @on(Button.Pressed, "#elicit-decline")
    def _on_decline_btn(self) -> None:
        self.dismiss({"action": "decline"})

    @on(Button.Pressed, "#elicit-cancel")
    def _on_cancel_btn(self) -> None:
        self.dismiss({"action": "cancel"})

    @on(Button.Pressed, "#elicit-open-url")
    def _on_open_url(self) -> None:
        if self.event.url:
            try:
                webbrowser.open(self.event.url)
            except Exception:
                pass
        self.dismiss({"action": "accept", "content": {}})

    @on(Button.Pressed, "#elicit-copy-url")
    def _on_copy_url(self) -> None:
        if self.event.url:
            try:
                self.app.copy_to_clipboard(self.event.url)
            except Exception:
                pass


class SessionPickerModal(ModalScreen[str | None]):
    """Fuzzy session switcher modal (`Ctrl+S` / `/resume`) enriched with ogy.db metadata."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
    ]

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        db_meta: dict[str, dict[str, Any]],
        *,
        active_session_id: str | None = None,
        lang: str = "fr",
    ) -> None:
        super().__init__()
        self._raw_sessions = sessions
        self._db_meta = db_meta
        self._active_session_id = active_session_id
        self.lang = lang
        self._filtered_ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog", id="session-picker-dialog"):
            yield Static(t(self.lang, "sessions_modal_title"), classes="modal-title", markup=False)
            yield Input(placeholder=t(self.lang, "sessions_search_placeholder"), id="session-picker-filter")
            yield OptionList(id="session-picker-list")
            with Horizontal(classes="modal-buttons"):
                yield Button(t(self.lang, "glossary_close"), id="session-picker-close")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#session-picker-filter", Input).focus()

    def _populate(self, query: str) -> None:
        q = query.strip().lower()
        opt_list = self.query_one("#session-picker-list", OptionList)
        opt_list.clear_options()
        self._filtered_ids = []

        # Merge ACP sessions and any local db sessions
        merged: dict[str, dict[str, Any]] = {}
        for item in self._raw_sessions:
            sid = str(item.get("session_id") or item.get("sessionId") or "")
            if sid:
                merged[sid] = dict(item)
        for sid, meta in self._db_meta.items():
            merged.setdefault(sid, {"session_id": sid})
            merged[sid].update(meta)

        for idx, (sid, info) in enumerate(merged.items()):
            title = str(info.get("title") or info.get("initial_prompt") or "")[:48].replace("\n", " ")
            model = str(info.get("initial_model") or info.get("model") or "")
            turns = int(info.get("turn_count") or 0)
            cost = float(info.get("cost_usd") or 0.0)
            started = str(info.get("started_at") or info.get("updated_at") or "")[:16]
            marker = "● " if sid == self._active_session_id else "  "
            label = f"{marker}{sid[:12]}  {started}  {model}  {turns}t  ${cost:.4f}  {title}"
            if q and q not in label.lower() and q not in sid.lower():
                continue
            self._filtered_ids.append(sid)
            opt_list.add_option(Option(label, id=f"sess-{idx}"))
        if self._filtered_ids:
            opt_list.highlighted = 0

    @on(Input.Changed, "#session-picker-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#session-picker-filter")
    def _on_filter_submitted(self) -> None:
        opt_list = self.query_one("#session-picker-list", OptionList)
        idx = opt_list.highlighted if opt_list.highlighted is not None else 0
        if 0 <= idx < len(self._filtered_ids):
            self.dismiss(self._filtered_ids[idx])

    @on(OptionList.OptionSelected, "#session-picker-list")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._filtered_ids):
            self.dismiss(self._filtered_ids[idx])

    def on_key(self, event: events.Key) -> None:
        opt_list = self.query_one("#session-picker-list", OptionList)
        if event.key == "down":
            opt_list.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            opt_list.action_cursor_up()
            event.prevent_default()
            event.stop()

    @on(Button.Pressed, "#session-picker-close")
    def action_cancel(self) -> None:
        self.dismiss(None)


class HandoffReviewModal(ModalScreen[str | None]):
    """Editable handoff review modal before `/rotate` with live estimated token counter."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+enter", "confirm", "Confirm", show=False, priority=True),
    ]

    def __init__(self, draft_markdown: str, *, lang: str = "fr") -> None:
        super().__init__()
        self._draft = draft_markdown
        self.lang = lang

    def compose(self) -> ComposeResult:
        est_tokens = max(1, len(self._draft) // 4)
        with Container(classes="modal-dialog", id="handoff-dialog"):
            yield Static(t(self.lang, "handoff_modal_title"), classes="modal-title", markup=False)
            yield TextArea(self._draft, id="handoff-editor", soft_wrap=True)
            yield Static(t(self.lang, "handoff_tokens", tokens=est_tokens), id="handoff-token-count", markup=False)
            with Horizontal(classes="modal-buttons"):
                yield Button(t(self.lang, "handoff_confirm"), id="handoff-confirm-btn", variant="success")
                yield Button(t(self.lang, "elicit_cancel"), id="handoff-cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#handoff-editor", TextArea).focus()

    @on(TextArea.Changed, "#handoff-editor")
    def _on_editor_changed(self, event: TextArea.Changed) -> None:
        est_tokens = max(1, len(event.text_area.text) // 4)
        self.query_one("#handoff-token-count", Static).update(
            t(self.lang, "handoff_tokens", tokens=est_tokens)
        )

    @on(Button.Pressed, "#handoff-confirm-btn")
    def action_confirm(self) -> None:
        text = self.query_one("#handoff-editor", TextArea).text.strip()
        self.dismiss(text if text else self._draft)

    @on(Button.Pressed, "#handoff-cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)


class DelegationModal(ModalScreen[dict[str, Any] | None]):
    """User-driven `[D] > 60K` delegation modal (ToS Section 6 compliant).

    Offers two explicit actions:
    - `insert_instruction`: insert a structured instruction inviting `agy` to call `ogy_research_repo`.
    - `run_parallel`: run read-only `ogy_research_repo` in parallel and attach its sourced report to the next prompt.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+enter", "insert_instruction", "Insert", show=False, priority=True),
    ]

    def __init__(self, initial_question: str = "", *, lang: str = "fr") -> None:
        super().__init__()
        self._initial_question = initial_question.strip()
        self.lang = lang

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog", id="delegate-dialog"):
            yield Static(t(self.lang, "delegate_modal_title"), classes="modal-title", markup=False)
            yield Label(t(self.lang, "delegate_question_label"), markup=False)
            yield TextArea(self._initial_question, id="delegate-question", soft_wrap=True)
            yield Label(t(self.lang, "delegate_scope_label"), markup=False)
            yield Input(placeholder=t(self.lang, "delegate_scope_placeholder"), id="delegate-scope")
            with Horizontal(classes="modal-buttons"):
                yield Button(t(self.lang, "delegate_insert_btn"), id="delegate-insert-btn", variant="primary")
                yield Button(t(self.lang, "delegate_run_btn"), id="delegate-run-btn", variant="success")
                yield Button(t(self.lang, "elicit_cancel"), id="delegate-cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#delegate-question", TextArea).focus()

    def _payload(self, mode: str) -> dict[str, Any] | None:
        question = self.query_one("#delegate-question", TextArea).text.strip()
        if not question:
            return None
        raw_scope = self.query_one("#delegate-scope", Input).value.strip()
        scope_paths = [p.strip() for p in raw_scope.split(",") if p.strip()] if raw_scope else []
        return {"mode": mode, "question": question, "scope_paths": scope_paths}

    @on(Button.Pressed, "#delegate-insert-btn")
    def action_insert_instruction(self) -> None:
        self.dismiss(self._payload("insert_instruction"))

    @on(Button.Pressed, "#delegate-run-btn")
    def action_run_parallel(self) -> None:
        self.dismiss(self._payload("run_parallel"))

    @on(Button.Pressed, "#delegate-cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpGlossaryModal(ModalScreen[None]):
    """Modal explaining Optigravity metrics, zones, quotas (`3p`), and shortcuts."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("f1", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False),
    ]

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
    def action_close(self) -> None:
        self.dismiss(None)


GlossaryModal = HelpGlossaryModal
