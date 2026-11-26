"""Composer Pro: multiline PromptTextArea, SQLite history, paste-collapse, @file & /command popovers."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import schema
from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ogy_frontends.acp_tui.i18n import t
from ogy_frontends.acp_tui.state.sessions import (
    load_prompt_history_readonly,
    save_prompt_history_entry,
)

PASTE_LINE_THRESHOLD = 8
PASTE_CHAR_THRESHOLD = 600
MAX_VISIBLE_LINES = 8

_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"})
_DOC_EXTS = frozenset({".md", ".rst", ".txt", ".toml", ".yaml", ".yml"})
_DATA_EXTS = frozenset({".log", ".json", ".lock", ".csv", ".min.js"})
_LOCK_NAMES = frozenset({"package-lock.json", "cargo.lock", "uv.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml"})
_PASTE_CHIP_RE = re.compile(r"\[Pasted #(\d+) · \d+ (?:lignes|lines)\]")
_AT_TOKEN_RE = re.compile(r"(?:^|\s)@([^\s]*)$")


@dataclass(frozen=True, slots=True)
class FileIndexEntry:
    rel_path: str
    size_bytes: int
    is_modified: bool = False


def _format_size(size_bytes: int, lang: str = "fr") -> str:
    unit_k = "Ko" if lang == "fr" else "KB"
    unit_m = "Mo" if lang == "fr" else "MB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} {unit_m}"
    if size_bytes >= 1024:
        return f"{max(1, round(size_bytes / 1024))} {unit_k}"
    return f"{size_bytes} B"


def _exceeds_firebreak(rel_path: str, size_bytes: int, *, file_max: int = 24_576, doc_max: int = 49_152, data_max: int = 8_192) -> bool:
    p = Path(rel_path)
    name = p.name.lower()
    ext = p.suffix.lower()
    if name in _LOCK_NAMES or ext in _DATA_EXTS:
        return size_bytes > data_max
    if ext in _DOC_EXTS:
        return size_bytes > doc_max
    return size_bytes > file_max


def scan_workspace_files(cwd: str, max_files: int = 3000) -> list[FileIndexEntry]:
    """Enumerate workspace files via `git ls-files` with `os.walk` fallback (run in worker thread)."""
    root = Path(cwd).resolve()
    modified: set[str] = set()
    rel_paths: list[str] = []

    try:
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if len(line) >= 4:
                    path_part = line[3:].strip().strip('"').replace("\\", "/")
                    if " -> " in path_part:
                        path_part = path_part.split(" -> ", 1)[1]
                    modified.add(path_part)

        ls = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if ls.returncode == 0 and ls.stdout.strip():
            rel_paths = [
                ln.strip().replace("\\", "/")
                for ln in ls.stdout.splitlines()
                if ln.strip()
            ][:max_files]
    except Exception:
        rel_paths = []

    if not rel_paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if len(rel_paths) >= max_files:
                    break
                full = Path(dirpath) / fn
                try:
                    rel = full.relative_to(root).as_posix()
                except ValueError:
                    continue
                rel_paths.append(rel)
            if len(rel_paths) >= max_files:
                break

    entries: list[FileIndexEntry] = []
    for rel in rel_paths:
        try:
            sz = (root / rel).stat().st_size
        except OSError:
            sz = 0
        entries.append(FileIndexEntry(rel_path=rel, size_bytes=sz, is_modified=(rel in modified)))
    return entries


def fuzzy_rank_files(entries: list[FileIndexEntry], query: str, limit: int = 12) -> list[FileIndexEntry]:
    """Rank workspace files prioritizing exact basenames, git-modified files, and subsequence matches."""
    q = query.strip().lower()
    if not q:
        ordered = sorted(entries, key=lambda e: (not e.is_modified, e.rel_path))
        return ordered[:limit]

    scored: list[tuple[int, FileIndexEntry]] = []
    for entry in entries:
        low_path = entry.rel_path.lower()
        base = Path(low_path).name
        score = 0
        if base == q:
            score += 1000
        elif base.startswith(q):
            score += 700
        elif q in base:
            score += 450
        elif q in low_path:
            score += 300
        else:
            # Ordered character subsequence match
            it = iter(low_path)
            if all(ch in it for ch in q):
                score += 150
        if score > 0:
            if entry.is_modified:
                score += 120
            score -= min(100, len(low_path))
            scored.append((score, entry))

    scored.sort(key=lambda pair: (-pair[0], pair[1].rel_path))
    return [entry for _, entry in scored[:limit]]


class PromptTextArea(TextArea):
    """Multiline composer input with defensive Enter/newline matrix, paste-collapse, and history."""

    class Submitted(Message):
        def __init__(self, text: str, display_text: str, extra_blocks: list[Any]) -> None:
            super().__init__()
            self.text = text
            self.display_text = display_text
            self.extra_blocks = extra_blocks

    class OpenReverseSearch(Message):
        pass

    class OpenExternalEditor(Message):
        pass

    class AttachmentsChanged(Message):
        pass

    def __init__(
        self,
        *,
        enter_behavior: str = "submit",
        lang: str = "fr",
        placeholder: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__("", soft_wrap=True, tab_behavior="focus", show_line_numbers=False, placeholder=placeholder, id=id)
        self.enter_behavior = enter_behavior if enter_behavior in ("submit", "newline") else "submit"
        self.lang = lang
        self._pasted_blocks: dict[int, tuple[str, str]] = {}  # id -> (chip, raw_content)
        self._paste_counter: int = 0
        self._image_blocks: dict[int, tuple[str, Any]] = {}  # id -> (chip, ImageContentBlock)
        self._image_counter: int = 0

        # History navigation state
        self._history: list[str] = []
        self._history_idx: int | None = None
        self._draft_buffer: str = ""

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new_val: str) -> None:
        self.load_text(new_val)
        self.move_cursor(self.document.end)
        self._adjust_height()

    def set_history(self, entries: list[str]) -> None:
        self._history = list(entries)
        self._history_idx = None

    def append_history(self, entry: str) -> None:
        cleaned = entry.strip()
        if cleaned and (not self._history or self._history[-1] != cleaned):
            self._history.append(cleaned)
        self._history_idx = None
        self._draft_buffer = ""

    def _adjust_height(self) -> None:
        lines = max(1, min(MAX_VISIBLE_LINES, self.text.count("\n") + 1))
        self.styles.height = lines + 2  # +2 for top/bottom border

    def _on_text_area_changed(self) -> None:
        self._adjust_height()

    def on_paste(self, event: events.Paste) -> None:
        raw = event.text or ""
        line_count = raw.count("\n") + 1
        if line_count >= PASTE_LINE_THRESHOLD or len(raw) >= PASTE_CHAR_THRESHOLD:
            event.prevent_default()
            event.stop()
            self._paste_counter += 1
            idx = self._paste_counter
            chip = t(self.lang, "pasted_chip", idx=idx, lines=line_count)
            self._pasted_blocks[idx] = (chip, raw)
            self.insert(chip)
            self._adjust_height()
            self.post_message(self.AttachmentsChanged())

    def attach_image(self, data: bytes, mime_type: str = "image/png") -> str:
        """Attach an image as an ACP ImageContentBlock and insert an `[Image #N]` chip."""
        self._image_counter += 1
        idx = self._image_counter
        chip = t(self.lang, "image_chip", idx=idx)
        b64 = base64.b64encode(data).decode("ascii")
        block = schema.ImageContentBlock(type="image", data=b64, mime_type=mime_type)
        self._image_blocks[idx] = (chip, block)
        self.insert(chip)
        self._adjust_height()
        self.post_message(self.AttachmentsChanged())
        return chip

    def expand_payload(self, display_text: str) -> tuple[str, list[Any]]:
        """Re-inject collapsed paste blocks verbatim and collect active image blocks."""
        expanded = display_text
        for idx, (chip, raw) in self._pasted_blocks.items():
            if chip in expanded:
                expanded = expanded.replace(chip, raw)
        extra_blocks: list[Any] = []
        for idx, (chip, blk) in self._image_blocks.items():
            if chip in display_text:
                extra_blocks.append(blk)
        return expanded, extra_blocks

    def clear_attachments(self) -> None:
        self._pasted_blocks.clear()
        self._image_blocks.clear()
        self.post_message(self.AttachmentsChanged())

    def get_attachment_summary(self) -> str:
        parts: list[str] = []
        for idx, (chip, raw) in self._pasted_blocks.items():
            if chip in self.text:
                parts.append(f"{chip} ({len(raw):,} chars)")
        for idx, (chip, _) in self._image_blocks.items():
            if chip in self.text:
                parts.append(chip)
        return "  ·  ".join(parts)

    def _try_delete_chip_before_cursor(self) -> bool:
        """If cursor is right after a `[Pasted #N · X lignes]` chip, delete the whole chip."""
        row, col = self.cursor_location
        line_text = self.document.get_line(row)
        prefix = line_text[:col]
        match = None
        for m in _PASTE_CHIP_RE.finditer(prefix):
            if m.end() == col:
                match = m
        if match is None:
            return False
        chip_id = int(match.group(1))
        self._pasted_blocks.pop(chip_id, None)
        self.delete((row, match.start()), (row, col))
        self._adjust_height()
        self.post_message(self.AttachmentsChanged())
        return True

    async def action_submit(self) -> None:
        display = self.text.strip()
        if not display:
            return
        expanded, extra_blocks = self.expand_payload(display)
        self.load_text("")
        self.clear_attachments()
        self._adjust_height()
        self.post_message(self.Submitted(expanded, display, extra_blocks))

    def _navigate_history(self, direction: int) -> bool:
        if not self._history:
            return False
        if self._history_idx is None:
            if direction < 0:
                self._draft_buffer = self.text
                self._history_idx = len(self._history) - 1
            else:
                return False
        else:
            new_idx = self._history_idx + direction
            if new_idx < 0:
                self._history_idx = 0
            elif new_idx >= len(self._history):
                self._history_idx = None
                self.load_text(self._draft_buffer)
                self.move_cursor(self.document.end)
                self._adjust_height()
                return True
            else:
                self._history_idx = new_idx

        self.load_text(self._history[self._history_idx])
        self.move_cursor(self.document.end)
        self._adjust_height()
        return True

    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        # Let parent PromptComposer intercept popup navigation/selection keys first
        for ancestor in self.ancestors:
            if isinstance(ancestor, PromptComposer):
                if ancestor.handle_popup_key(event):
                    return
                break

        # Ctrl+R -> reverse history search
        if key == "ctrl+r":
            event.prevent_default()
            event.stop()
            self.post_message(self.OpenReverseSearch())
            return

        # Ctrl+G -> external $EDITOR
        if key == "ctrl+g":
            event.prevent_default()
            event.stop()
            self.post_message(self.OpenExternalEditor())
            return

        # Backspace on collapsed paste chip deletes entire chip
        if key == "backspace" and self._pasted_blocks:
            if self._try_delete_chip_before_cursor():
                event.prevent_default()
                event.stop()
                return

        # Defensive newline matrix: Shift+Enter, Ctrl+J, Alt+Enter
        if key in ("shift+enter", "ctrl+j", "alt+enter"):
            if self.enter_behavior == "newline" and key == "alt+enter":
                event.prevent_default()
                event.stop()
                await self.action_submit()
                return
            event.prevent_default()
            event.stop()
            self.insert("\n")
            self._adjust_height()
            return

        if key == "ctrl+enter":
            event.prevent_default()
            event.stop()
            await self.action_submit()
            return

        if key == "enter":
            # Backslash continuation `\` + Enter inserts newline in submit mode
            row, col = self.cursor_location
            line_text = self.document.get_line(row)
            if self.enter_behavior == "submit":
                if col > 0 and line_text[col - 1] == "\\":
                    event.prevent_default()
                    event.stop()
                    self.delete((row, col - 1), (row, col))
                    self.insert("\n")
                    self._adjust_height()
                    return
                event.prevent_default()
                event.stop()
                await self.action_submit()
                return
            # enter_behavior == "newline": let TextArea insert newline
            await super()._on_key(event)
            self._adjust_height()
            return

        # Cursor-first Up/Down before SQLite history navigation
        if key == "up" and self.cursor_location[0] == 0:
            if self._navigate_history(-1):
                event.prevent_default()
                event.stop()
                return
        elif key == "down" and self.cursor_location[0] >= self.document.line_count - 1:
            if self._navigate_history(1):
                event.prevent_default()
                event.stop()
                return

        await super()._on_key(event)
        self._adjust_height()


class PromptComposer(Vertical):
    """Composite composer widget: @file & /command popover, Ctrl+R reverse search, attachments, PromptTextArea."""

    class PromptSubmitted(Message):
        def __init__(self, text: str, display_text: str, extra_blocks: list[Any]) -> None:
            super().__init__()
            self.text = text
            self.display_text = display_text
            self.extra_blocks = extra_blocks

    def __init__(
        self,
        *,
        cwd: str,
        db_path: Path,
        slash_provider: Callable[[str], list[tuple[str, str]]],
        enter_behavior: str = "submit",
        lang: str = "fr",
        firebreak_limits: tuple[int, int, int] = (24_576, 49_152, 8_192),
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.cwd = cwd
        self.db_path = db_path
        self._slash_provider = slash_provider
        self.enter_behavior = enter_behavior
        self.lang = lang
        self._firebreak_limits = firebreak_limits

        self._file_index: list[FileIndexEntry] = []
        self._popup_mode: str = ""  # "slash" | "file" | ""
        self._file_tree_mode: bool = False
        self._popup_values: list[str] = []
        self._reverse_matches: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="composer-popup-hint", markup=False)
        yield OptionList(id="composer-popup")
        yield Input(
            placeholder=t(self.lang, "reverse_search_placeholder"),
            id="reverse-search-bar",
        )
        yield Static("", id="attachment-bar", markup=False)
        with Horizontal(id="input-row"):
            yield PromptTextArea(
                enter_behavior=self.enter_behavior,
                lang=self.lang,
                placeholder=t(self.lang, "input_placeholder"),
                id="prompt-input",
            )
            yield Button(t(self.lang, "send"), id="send-btn")

    def on_mount(self) -> None:
        self.refresh_file_index()
        self._load_history_worker()

    @property
    def input_widget(self) -> PromptTextArea:
        return self.query_one("#prompt-input", PromptTextArea)

    def apply_lang(self, lang: str) -> None:
        self.lang = lang
        ta = self.input_widget
        ta.lang = lang
        ta.placeholder = t(lang, "input_placeholder")
        self.query_one("#send-btn", Button).label = t(lang, "send")
        self.query_one("#reverse-search-bar", Input).placeholder = t(lang, "reverse_search_placeholder")

    @work(thread=True, exclusive=True, group="file-index")
    def refresh_file_index(self) -> None:
        entries = scan_workspace_files(self.cwd)
        self.app.call_from_thread(self._set_file_index, entries)

    def _set_file_index(self, entries: list[FileIndexEntry]) -> None:
        self._file_index = entries

    @work(thread=True, exclusive=True, group="prompt-history-load")
    def _load_history_worker(self) -> None:
        entries = load_prompt_history_readonly(self.db_path, repo_root=str(Path(self.cwd).resolve()))
        self.app.call_from_thread(self._apply_loaded_history, entries)

    def _apply_loaded_history(self, entries: list[str]) -> None:
        self.input_widget.set_history(entries)

    @work(thread=True, group="prompt-history-save")
    def persist_prompt(self, prompt_text: str, session_id: str | None = None) -> None:
        save_prompt_history_entry(
            self.db_path,
            repo_root=str(Path(self.cwd).resolve()),
            prompt_text=prompt_text,
            session_id=session_id,
        )

    def _hide_popup(self) -> None:
        popup = self.query_one("#composer-popup", OptionList)
        hint = self.query_one("#composer-popup-hint", Static)
        self._popup_mode = ""
        self._popup_values = []
        popup.clear_options()
        popup.remove_class("visible")
        hint.remove_class("visible")

    def _show_slash_popup(self, text: str) -> None:
        candidates = self._slash_provider(text)
        if not candidates:
            self._hide_popup()
            return
        popup = self.query_one("#composer-popup", OptionList)
        hint = self.query_one("#composer-popup-hint", Static)
        self._popup_mode = "slash"
        self._popup_values = [ins for ins, _ in candidates]
        popup.clear_options()
        for idx, (_, label) in enumerate(candidates):
            popup.add_option(Option(label, id=f"slash-{idx}"))
        popup.highlighted = 0
        popup.add_class("visible")
        hint.remove_class("visible")

    def _show_file_popup(self, query: str) -> None:
        matches = fuzzy_rank_files(self._file_index, query, limit=12)
        if not matches:
            self._hide_popup()
            return
        popup = self.query_one("#composer-popup", OptionList)
        hint = self.query_one("#composer-popup-hint", Static)
        self._popup_mode = "file"
        self._popup_values = [e.rel_path for e in matches]
        popup.clear_options()
        file_max, doc_max, data_max = self._firebreak_limits
        for idx, entry in enumerate(matches):
            sz_str = _format_size(entry.size_bytes, self.lang)
            warn = " ⚠" if _exceeds_firebreak(entry.rel_path, entry.size_bytes, file_max=file_max, doc_max=doc_max, data_max=data_max) else ""
            mod = " [M]" if entry.is_modified else ""
            if self._file_tree_mode:
                parts = entry.rel_path.split("/")
                indent = "  " * (len(parts) - 1)
                prefix = "└─ " if len(parts) > 1 else ""
                display = f"{indent}{prefix}{parts[-1]}  · {sz_str}{warn}{mod}"
            else:
                display = f"@{entry.rel_path}  · {sz_str}{warn}{mod}"
            popup.add_option(Option(display, id=f"file-{idx}"))
        popup.highlighted = 0
        popup.add_class("visible")
        hint_key = "file_flat_hint" if self._file_tree_mode else "file_tree_hint"
        hint.update(t(self.lang, hint_key))
        hint.add_class("visible")

    def _update_popups_for_text(self, text: str) -> None:
        if text.startswith("/") and "\n" not in text:
            self._show_slash_popup(text)
            return
        m = _AT_TOKEN_RE.search(text)
        if m is not None:
            self._show_file_popup(m.group(1))
            return
        self._hide_popup()

    def _apply_popup_selection(self, idx: int) -> None:
        if not (0 <= idx < len(self._popup_values)):
            return
        chosen = self._popup_values[idx]
        ta = self.input_widget
        if self._popup_mode == "slash":
            ta.value = chosen
            ta.focus()
            self._update_popups_for_text(chosen)
        elif self._popup_mode == "file":
            # Invariant #1: insert ONLY relative path `@rel_path` (never file contents)
            cur = ta.text
            new_text = _AT_TOKEN_RE.sub(
                lambda m: (m.group(0)[0] if m.group(0)[0].isspace() else "") + f"@{chosen} ",
                cur,
                count=1,
            )
            ta.value = new_text
            ta.focus()
            self._hide_popup()

    @on(TextArea.Changed, "#prompt-input")
    def _on_ta_changed(self, event: TextArea.Changed) -> None:
        self._update_popups_for_text(event.text_area.text)

    @on(PromptTextArea.AttachmentsChanged)
    def _on_attachments_changed(self) -> None:
        bar = self.query_one("#attachment-bar", Static)
        summary = self.input_widget.get_attachment_summary()
        if summary:
            bar.update(summary)
            bar.add_class("visible")
        else:
            bar.update("")
            bar.remove_class("visible")

    @on(OptionList.OptionSelected, "#composer-popup")
    def _on_popup_selected(self, event: OptionList.OptionSelected) -> None:
        self._apply_popup_selection(event.option_index)

    @on(Button.Pressed, "#send-btn")
    async def _on_send_pressed(self) -> None:
        await self.input_widget.action_submit()

    @on(PromptTextArea.Submitted)
    def _on_ta_submitted(self, event: PromptTextArea.Submitted) -> None:
        self._hide_popup()
        self.input_widget.append_history(event.display_text)
        self.post_message(self.PromptSubmitted(event.text, event.display_text, event.extra_blocks))

    @on(PromptTextArea.OpenReverseSearch)
    def _on_open_reverse_search(self) -> None:
        self._hide_popup()
        bar = self.query_one("#reverse-search-bar", Input)
        bar.value = ""
        bar.add_class("visible")
        bar.focus()

    @on(Input.Changed, "#reverse-search-bar")
    def _on_reverse_search_changed(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        history = self.input_widget._history
        matches = [h for h in reversed(history) if not q or q in h.lower()][:8]
        self._reverse_matches = matches
        popup = self.query_one("#composer-popup", OptionList)
        self._popup_mode = "history"
        self._popup_values = matches
        popup.clear_options()
        for idx, entry in enumerate(matches):
            one_line = entry.replace("\n", " ↵ ")[:96]
            popup.add_option(Option(one_line, id=f"hist-{idx}"))
        if matches:
            popup.highlighted = 0
            popup.add_class("visible")
        else:
            popup.remove_class("visible")

    @on(Input.Submitted, "#reverse-search-bar")
    def _on_reverse_search_submitted(self) -> None:
        bar = self.query_one("#reverse-search-bar", Input)
        popup = self.query_one("#composer-popup", OptionList)
        idx = popup.highlighted if popup.highlighted is not None else 0
        if 0 <= idx < len(self._reverse_matches):
            self.input_widget.value = self._reverse_matches[idx]
        bar.remove_class("visible")
        self._hide_popup()
        self.input_widget.focus()

    @on(PromptTextArea.OpenExternalEditor)
    def _on_open_external_editor(self) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            return
        ta = self.input_widget
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(ta.text)
            tmp_path = Path(tf.name)
        try:
            with self.app.suspend():
                subprocess.run([editor, str(tmp_path)], check=False)
            updated = tmp_path.read_text(encoding="utf-8")
            ta.value = updated
        except Exception:
            pass
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def handle_popup_key(self, event: events.Key) -> bool:
        """Intercept Tab / Enter / Up / Down / Esc while the slash or @file popup is visible."""
        popup = self.query_one("#composer-popup", OptionList)
        if not popup.has_class("visible") or not self._popup_values:
            return False

        if event.key == "tab":
            if self._popup_mode == "file":
                # Pattern Toad: Tab toggles between fuzzy list and tree view
                self._file_tree_mode = not self._file_tree_mode
                m = _AT_TOKEN_RE.search(self.input_widget.text)
                self._show_file_popup(m.group(1) if m else "")
            else:
                idx = popup.highlighted if popup.highlighted is not None else 0
                self._apply_popup_selection(idx)
            event.prevent_default()
            event.stop()
            return True
        if event.key == "enter" and self._popup_mode == "file":
            idx = popup.highlighted if popup.highlighted is not None else 0
            self._apply_popup_selection(idx)
            event.prevent_default()
            event.stop()
            return True
        if event.key == "down":
            popup.action_cursor_down()
            event.prevent_default()
            event.stop()
            return True
        if event.key == "up":
            popup.action_cursor_up()
            event.prevent_default()
            event.stop()
            return True
        if event.key == "escape":
            self._hide_popup()
            event.prevent_default()
            event.stop()
            return True
        return False

    def on_key(self, event: events.Key) -> None:
        bar = self.query_one("#reverse-search-bar", Input)
        if bar.has_class("visible") and bar.has_focus:
            if event.key == "escape":
                bar.remove_class("visible")
                self._hide_popup()
                self.input_widget.focus()
                event.prevent_default()
                event.stop()
            elif event.key == "down":
                self.query_one("#composer-popup", OptionList).action_cursor_down()
                event.prevent_default()
                event.stop()
            elif event.key == "up":
                self.query_one("#composer-popup", OptionList).action_cursor_up()
                event.prevent_default()
                event.stop()
            return

        if self.input_widget.has_focus:
            self.handle_popup_key(event)
