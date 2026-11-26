"""Transcript widgets: MarkdownStream, smart scroll anchor(), clean copy, ThoughtBlock, ActivityGroup, ToolCard."""

from __future__ import annotations

import difflib
import json
import re
import time
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Collapsible, Markdown, Static
from textual.widgets._markdown import MarkdownStream

from ogy_frontends.acp_tui.client import ToolCallEvent
from ogy_frontends.acp_tui.i18n import t

MAX_MARKDOWN_STREAM_CHARS = 200_000
INLINE_DIFF_MAX_CHANGED_LINES = 25

_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```)")
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://([^)\s]+)\)")
_LINE_ANCHOR_RE = re.compile(r"#L(\d+)(?:-L?(\d+))?$")

_READ_TOOLS = frozenset({"read_file", "view_file", "view_file_outline", "view_code_item", "list_dir", "read"})
_SEARCH_TOOLS = frozenset({"grep_search", "find_by_name", "search", "ripgrep", "glob", "search_web"})
_FETCH_TOOLS = frozenset({"fetch", "web_fetch", "read_url_content", "http_get"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content", "edit", "write"})


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


def extract_diff_blocks(content: list[Any] | None, raw_input: Any = None) -> list[tuple[str, str, str]]:
    """Extract `(path, old_text, new_text)` tuples from ACP `content` blocks or `raw_input`."""
    diffs: list[tuple[str, str, str]] = []
    for blk in content or []:
        if isinstance(blk, dict):
            btype = str(blk.get("type") or "")
            if btype == "diff" or ("oldText" in blk or "old_text" in blk or "newText" in blk or "new_text" in blk):
                path = str(blk.get("path") or "file")
                old_t = str(blk.get("oldText") if "oldText" in blk else blk.get("old_text") or "")
                new_t = str(blk.get("newText") if "newText" in blk else blk.get("new_text") or "")
                diffs.append((path, old_t, new_t))
        else:
            btype = str(getattr(blk, "type", "") or "")
            if btype == "diff" or hasattr(blk, "old_text") or hasattr(blk, "new_text"):
                path = str(getattr(blk, "path", "file") or "file")
                old_t = str(getattr(blk, "old_text", None) or getattr(blk, "oldText", None) or "")
                new_t = str(getattr(blk, "new_text", None) or getattr(blk, "newText", None) or "")
                diffs.append((path, old_t, new_t))

    if not diffs and isinstance(raw_input, dict):
        if "old_text" in raw_input or "new_text" in raw_input or "oldText" in raw_input or "newText" in raw_input:
            path = str(raw_input.get("path") or raw_input.get("TargetFile") or "file")
            old_t = str(raw_input.get("oldText") if "oldText" in raw_input else raw_input.get("old_text") or "")
            new_t = str(raw_input.get("newText") if "newText" in raw_input else raw_input.get("new_text") or "")
            diffs.append((path, old_t, new_t))
        elif "Target" in raw_input and "Replacement" in raw_input:
            path = str(raw_input.get("Path") or raw_input.get("path") or "file")
            diffs.append((path, str(raw_input["Target"]), str(raw_input["Replacement"])))
    return diffs


def build_colored_unified_diff(path: str, old_text: str, new_text: str) -> tuple[Text, int, int, str]:
    """Build a Rich colored unified diff plus `(added_lines, removed_lines, raw_diff_str)`."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    rich_out = Text()
    added = 0
    removed = 0
    raw_lines: list[str] = []
    for line in diff_lines:
        clean = line.rstrip("\r\n")
        raw_lines.append(clean)
        if clean.startswith("+++") or clean.startswith("---"):
            rich_out.append(clean + "\n", style="bold #8b949e")
        elif clean.startswith("@@"):
            rich_out.append(clean + "\n", style="bold #6ea8fe")
        elif clean.startswith("+"):
            added += 1
            rich_out.append(clean + "\n", style="#3fb950 on #12261e")
        elif clean.startswith("-"):
            removed += 1
            rich_out.append(clean + "\n", style="#f85149 on #2b1417")
        else:
            rich_out.append(clean + "\n", style="#c9d1d9")
    return rich_out, added, removed, "\n".join(raw_lines)


def classify_exploration_kind(event: ToolCallEvent) -> str | None:
    """Return 'read' | 'search' | 'fetch' if tool is a pure exploration call, else None."""
    kind = (event.kind or "").lower()
    name = (event.name or "").lower()
    if kind in ("read", "search", "fetch"):
        return kind
    if name in _READ_TOOLS:
        return "read"
    if name in _SEARCH_TOOLS:
        return "search"
    if name in _FETCH_TOOLS:
        return "fetch"
    return None


class AssistantMessage(Vertical):
    """Streamed assistant Markdown response with 200K plain-text guardrail and raw Markdown copy."""

    class CopyRequested(Message):
        def __init__(self, raw_text: str) -> None:
            super().__init__()
            self.raw_text = raw_text

    def __init__(self, *, lang: str = "fr") -> None:
        super().__init__()
        self.lang = lang
        self.raw_markdown: str = ""
        self._md_widget: Markdown | None = None
        self._stream: MarkdownStream | None = None
        self._fallback_static: Static | None = None
        self._truncated: bool = False

    def compose(self) -> ComposeResult:
        self._md_widget = Markdown("")
        yield self._md_widget
        with Horizontal(classes="msg-toolbar"):
            yield Button(t(self.lang, "copy_btn"), classes="copy-md-btn")

    async def append_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self.raw_markdown += chunk
        if self._truncated:
            return

        if len(self.raw_markdown) > MAX_MARKDOWN_STREAM_CHARS:
            self._truncated = True
            await self.finish_stream()
            if self._md_widget is not None:
                self._md_widget.display = False
            preview = (
                self.raw_markdown[:MAX_MARKDOWN_STREAM_CHARS]
                + f"\n\n[… truncated at {MAX_MARKDOWN_STREAM_CHARS:,} chars — use Copy for raw text]"
            )
            self._fallback_static = Static(preview, classes="raw-text-fallback", markup=False)
            await self.mount(self._fallback_static, before=0)
            return

        if self._md_widget is None:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md_widget)
        cleaned = _clean_file_links(chunk)
        await self._stream.write(cleaned)

    async def finish_stream(self) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            try:
                await stream.stop()
            except Exception:
                pass

    def extract_code_blocks(self) -> list[str]:
        """Extract raw un-wrapped fenced code block bodies from `raw_markdown`."""
        blocks: list[str] = []
        for match in _FENCED_CODE_RE.finditer(self.raw_markdown):
            raw = match.group(1)
            lines = raw.splitlines()
            if len(lines) >= 2:
                blocks.append("\n".join(lines[1:-1]))
        return blocks

    @on(Button.Pressed, ".copy-md-btn")
    def _on_copy_pressed(self) -> None:
        self.post_message(self.CopyRequested(self.raw_markdown))


class ThoughtBlock(Collapsible):
    """Discreet 2-3 line dim italic reasoning drawer with stopwatch; auto-collapses on response/tool."""

    def __init__(self, initial_text: str = "", *, lang: str = "fr") -> None:
        self.lang = lang
        self.raw_text = initial_text
        self._started_at = time.monotonic()
        self._body = Static(initial_text, classes="thought-body", markup=False)
        super().__init__(
            self._body,
            title=t(lang, "thought_drawer", elapsed=0.0, chars=len(initial_text)),
            collapsed=False,
        )

    def append_chunk(self, chunk: str) -> None:
        self.raw_text += chunk
        elapsed = max(0.0, time.monotonic() - self._started_at)
        self.title = t(self.lang, "thought_drawer", elapsed=elapsed, chars=len(self.raw_text))
        # Show last 3 lines while streaming to stay compact (2-3 lines dim italic)
        tail_lines = self.raw_text.splitlines()[-3:]
        self._body.update("\n".join(tail_lines) if tail_lines else self.raw_text[-240:])

    def auto_collapse(self) -> None:
        self._body.update(self.raw_text)
        self.collapsed = True

    def set_verbosity(self, high: bool) -> None:
        self._body.update(self.raw_text)
        self.set_class(high, "expanded-full")
        self.collapsed = not high


class ActivityGroup(Collapsible):
    """Groups consecutive error-free exploration calls (read/search/fetch) into 1 clickable line."""

    def __init__(self, *, lang: str = "fr", verbose: bool = False) -> None:
        self.lang = lang
        self._started_at = time.monotonic()
        self._counts: dict[str, int] = {"read": 0, "search": 0, "fetch": 0}
        self._entries: dict[str, tuple[str, str]] = {}  # tool_call_id -> (kind, line)
        self._body = Static("", classes="tool-detail", markup=False)
        super().__init__(self._body, title="…", collapsed=not verbose)

    def upsert_call(self, event: ToolCallEvent, explore_kind: str) -> None:
        is_new = event.tool_call_id not in self._entries
        if is_new:
            self._counts[explore_kind] = self._counts.get(explore_kind, 0) + 1
        icon = "✓" if event.status == "completed" else "●"
        title_part = f" · {event.title}" if event.title and event.title != event.name else ""
        self._entries[event.tool_call_id] = (explore_kind, f"{icon} {event.name}{title_part}")
        self._refresh_display()

    def _refresh_display(self) -> None:
        elapsed = max(0.0, time.monotonic() - self._started_at)
        parts: list[str] = []
        if self._counts["read"]:
            parts.append(t(self.lang, "activity_reads", n=self._counts["read"]))
        if self._counts["search"]:
            parts.append(t(self.lang, "activity_searches", n=self._counts["search"]))
        if self._counts["fetch"]:
            parts.append(t(self.lang, "activity_fetches", n=self._counts["fetch"]))
        summary = ", ".join(parts) if parts else "activity"
        self.title = f"{summary} · {elapsed:.1f}s"
        self._body.update("\n".join(line for _, line in self._entries.values()))

    def set_verbosity(self, high: bool) -> None:
        self.collapsed = not high


class ToolCard(Collapsible):
    """Always-expandable tool call card with inline colored diffs and auto-expanded errors."""

    class FullscreenDiffRequested(Message):
        def __init__(self, title: str, diff_text: Text) -> None:
            super().__init__()
            self.title = title
            self.diff_text = diff_text

    def __init__(self, event: ToolCallEvent, *, lang: str = "fr", verbose: bool = False) -> None:
        self.lang = lang
        self.event = event
        self._verbose = verbose
        self._full_diff_text: Text | None = None
        self._detail_widget = Static("", classes="tool-detail")
        self._diff_btn = Button(t(lang, "diff_fullscreen"), classes="diff-fullscreen-btn")
        self._diff_btn.display = False

        body = Vertical(self._detail_widget, Horizontal(self._diff_btn, classes="diff-summary-row"))
        super().__init__(body, title=self._build_title(event), collapsed=not (verbose or event.is_error))
        self.update_from_event(event)

    def _build_title(self, event: ToolCallEvent) -> str:
        if event.is_error or event.status in ("failed", "error"):
            icon = "✗"
        elif event.status == "completed":
            icon = "✓"
        else:
            icon = "●"
        title_part = f" · {event.title}" if event.title and event.title != event.name else ""
        return f"{icon} {event.name}{title_part}"

    def update_from_event(self, event: ToolCallEvent) -> None:
        self.event = event
        self.title = self._build_title(event)
        self.remove_class("tool-running", "tool-ok", "tool-err")

        is_failed = event.is_error or event.status in ("failed", "error")
        if is_failed:
            self.add_class("tool-err")
        elif event.status == "completed":
            self.add_class("tool-ok")
        else:
            self.add_class("tool-running")

        diffs = extract_diff_blocks(event.content, event.raw_input)
        if diffs:
            combined = Text()
            total_add = 0
            total_del = 0
            first_path = diffs[0][0]
            for path, old_t, new_t in diffs:
                r_diff, add_n, del_n, _ = build_colored_unified_diff(path, old_t, new_t)
                combined.append_text(r_diff)
                total_add += add_n
                total_del += del_n
            self._full_diff_text = combined
            total_changed = total_add + total_del
            if total_changed > INLINE_DIFF_MAX_CHANGED_LINES:
                self.title = f"{self._build_title(event)}  (+{total_add} / -{total_del} · {first_path})"
                self._detail_widget.update(combined)
                self._diff_btn.display = True
                self.collapsed = not (is_failed or self._verbose)
            else:
                self._detail_widget.update(combined)
                self._diff_btn.display = False
                self.collapsed = False
            return

        # Always expandable even without rawOutput -> reveal rawInput (avoids toad#146)
        parts: list[str] = []
        if event.raw_input is not None:
            if isinstance(event.raw_input, (dict, list)):
                parts.append("Input:\n" + json.dumps(event.raw_input, indent=2, ensure_ascii=False)[:1200])
            else:
                parts.append(f"Input: {str(event.raw_input)[:1200]}")
        if event.locations:
            loc_strs = [str(loc.get("path") if isinstance(loc, dict) else loc) for loc in event.locations[:5]]
            parts.append("Locations: " + ", ".join(loc_strs))
        if event.raw_output is not None and str(event.raw_output).strip():
            parts.append("Output:\n" + str(event.raw_output)[:2000])
        if not parts:
            parts.append(f"{event.name} ({event.status})")

        self._detail_widget.update(Text("\n".join(parts)))
        self._diff_btn.display = False
        if is_failed:
            self.collapsed = False

    def set_verbosity(self, high: bool) -> None:
        self._verbose = high
        if self.event.is_error or self.event.status in ("failed", "error"):
            self.collapsed = False
        elif high:
            self.collapsed = False

    @on(Button.Pressed, ".diff-fullscreen-btn")
    def _on_fullscreen_diff(self) -> None:
        if self._full_diff_text is not None:
            self.post_message(self.FullscreenDiffRequested(self.title, self._full_diff_text))


class SessionTranscript(VerticalScroll):
    """Isolated per-session transcript with MarkdownStream, smart scroll anchor(), and tool cards."""

    class ScrollAnchorChanged(Message):
        def __init__(self, session_id: str, anchored: bool, has_unread: bool) -> None:
            super().__init__()
            self.session_id = session_id
            self.anchored = anchored
            self.has_unread = has_unread

    def __init__(self, session_id: str, *, lang: str = "fr", verbosity: str = "low", id: str | None = None) -> None:
        super().__init__(id=id)
        self.session_id = session_id
        self.lang = lang
        self.verbosity = verbosity  # "low" | "high"

        self._anchored: bool = True
        self._has_unread: bool = False
        self._active_msg: AssistantMessage | None = None
        self._active_thought: ThoughtBlock | None = None
        self._active_group: ActivityGroup | None = None
        self._tool_cards: dict[str, ToolCard] = {}
        self._grouped_tools: dict[str, ActivityGroup] = {}

    def on_mount(self) -> None:
        self.anchor()
        self._anchored = True

    @property
    def is_anchored(self) -> bool:
        return self._anchored

    def pin_to_bottom(self) -> None:
        """Re-enable smart scroll anchor and jump to bottom (e.g. End key or pill click)."""
        self._anchored = True
        self._has_unread = False
        self.scroll_end(animate=False)
        self.anchor()
        self.post_message(self.ScrollAnchorChanged(self.session_id, True, False))

    def detach_anchor(self) -> None:
        """Release smart scroll anchor when user scrolls up manually (0 scroll hijacking)."""
        if self._anchored:
            self._anchored = False
            self.release_anchor()
            self.post_message(self.ScrollAnchorChanged(self.session_id, False, self._has_unread))

    def _mark_new_content(self) -> None:
        if not self._anchored:
            self._has_unread = True
            self.post_message(self.ScrollAnchorChanged(self.session_id, False, True))

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.detach_anchor()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("pageup", "up"):
            self.detach_anchor()
        elif event.key == "end":
            self.pin_to_bottom()
            event.prevent_default()
            event.stop()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        if new_value < old_value and not self.is_vertical_scroll_end:
            self.detach_anchor()
        elif self.is_vertical_scroll_end and not self._anchored:
            self._anchored = True
            self._has_unread = False
            self.anchor()
            self.post_message(self.ScrollAnchorChanged(self.session_id, True, False))

    async def close_active_message(self) -> None:
        if self._active_msg is not None:
            msg = self._active_msg
            self._active_msg = None
            await msg.finish_stream()

    def close_active_thought(self) -> None:
        if self._active_thought is not None:
            self._active_thought.auto_collapse()
            self._active_thought = None

    def _close_activity_group(self) -> None:
        self._active_group = None

    async def append_user_message(self, display_text: str) -> None:
        await self.close_active_message()
        self.close_active_thought()
        self._close_activity_group()
        await self.mount(Static(f"› {display_text}", classes="user-msg", markup=False))
        self._mark_new_content()

    async def append_notice(self, message: str, *, level: str = "info") -> None:
        await self.close_active_message()
        self.close_active_thought()
        self._close_activity_group()
        css_cls = "notice-line"
        if level == "warn":
            css_cls = "notice-line notice-warn"
        elif level == "err":
            css_cls = "notice-line notice-err"
        await self.mount(Static(f"ogy · {message}", classes=css_cls, markup=False))
        self._mark_new_content()

    async def append_assistant_chunk(self, text: str) -> None:
        self.close_active_thought()
        self._close_activity_group()
        if self._active_msg is None:
            msg = AssistantMessage(lang=self.lang)
            self._active_msg = msg
            await self.mount(msg)
        await self._active_msg.append_chunk(text)
        self._mark_new_content()

    async def append_thought_chunk(self, text: str) -> None:
        await self.close_active_message()
        self._close_activity_group()
        if self._active_thought is None:
            tb = ThoughtBlock(text, lang=self.lang)
            self._active_thought = tb
            await self.mount(tb)
        else:
            self._active_thought.append_chunk(text)
        self._mark_new_content()

    async def handle_tool_call(self, event: ToolCallEvent) -> None:
        await self.close_active_message()
        self.close_active_thought()

        is_failed = event.is_error or event.status in ("failed", "error")
        explore_kind = classify_exploration_kind(event)

        # If this tool call was previously grouped and now failed, promote to a visible error ToolCard
        if explore_kind and not is_failed and event.tool_call_id not in self._tool_cards:
            grp = self._grouped_tools.get(event.tool_call_id)
            if grp is None:
                if self._active_group is None:
                    grp = ActivityGroup(lang=self.lang, verbose=(self.verbosity == "high"))
                    self._active_group = grp
                    await self.mount(grp)
                else:
                    grp = self._active_group
                self._grouped_tools[event.tool_call_id] = grp
            grp.upsert_call(event, explore_kind)
            self._mark_new_content()
            return

        # Non-exploration or failed tool call -> dedicated ToolCard
        self._close_activity_group()
        card = self._tool_cards.get(event.tool_call_id)
        if card is None:
            card = ToolCard(event, lang=self.lang, verbose=(self.verbosity == "high"))
            self._tool_cards[event.tool_call_id] = card
            await self.mount(card)
        else:
            card.update_from_event(event)
        self._mark_new_content()

    def set_verbosity(self, verbosity: str) -> None:
        self.verbosity = verbosity
        high = verbosity == "high"
        for child in self.children:
            if isinstance(child, (ActivityGroup, ThoughtBlock, ToolCard)):
                child.set_verbosity(high)
