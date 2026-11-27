"""Transcript widgets: MarkdownStream, smart scroll anchor(), clean copy, ThoughtBlock, ActivityGroup, ToolCard."""

from __future__ import annotations

from collections.abc import Callable
import difflib
import json
import re
import time
from typing import Any

from markdown_it import MarkdownIt
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
_LINK_BUF_MAX_CHARS = 512

_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```)")
_FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(<?file://([^)>\n]+)>?\)")
_LINE_ANCHOR_RE = re.compile(r"#L(\d+)(?:-L?(\d+))?$")

_READ_TOOLS = frozenset({"read_file", "view_file", "view_file_outline", "view_code_item", "list_dir", "read"})
_SEARCH_TOOLS = frozenset({"grep_search", "find_by_name", "search", "ripgrep", "glob", "search_web"})
_FETCH_TOOLS = frozenset({"fetch", "web_fetch", "read_url_content", "http_get"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content", "edit", "write"})


def create_safe_markdown_parser() -> MarkdownIt:
    """GFM-like parser with `linkify=False` so filenames (`README.md`, `app.py`) never become clickable HTTP URLs."""
    return MarkdownIt("gfm-like", {"linkify": False})


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


class _StreamingLinkCleaner:
    """Incrementally cleans `[label](file:///...)` links even when split across streaming chunks."""

    def __init__(self) -> None:
        self._buf: str = ""
        self._in_fence: bool = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []

        while self._buf:
            # Handle fenced code block markers (` ``` `)
            fence_idx = self._buf.find("```")
            if self._in_fence:
                if fence_idx != -1:
                    out.append(self._buf[: fence_idx + 3])
                    self._buf = self._buf[fence_idx + 3 :]
                    self._in_fence = False
                    continue
                # Hold up to 2 trailing backticks in case a closing fence is split
                hold = 2 if self._buf.endswith("``") else (1 if self._buf.endswith("`") else 0)
                if hold:
                    if len(self._buf) > hold:
                        out.append(self._buf[:-hold])
                        self._buf = self._buf[-hold:]
                    break
                out.append(self._buf)
                self._buf = ""
                break

            # Outside fence: find earliest `[` or ` ``` `
            bracket_idx = self._buf.find("[")
            if fence_idx != -1 and (bracket_idx == -1 or fence_idx < bracket_idx):
                out.append(self._buf[: fence_idx + 3])
                self._buf = self._buf[fence_idx + 3 :]
                self._in_fence = True
                continue

            if bracket_idx == -1:
                hold = 2 if self._buf.endswith("``") else (1 if self._buf.endswith("`") else 0)
                if hold:
                    if len(self._buf) > hold:
                        out.append(self._buf[:-hold])
                        self._buf = self._buf[-hold:]
                    break
                out.append(self._buf)
                self._buf = ""
                break

            # Emit safe text before `[`
            if bracket_idx > 0:
                out.append(self._buf[:bracket_idx])
                self._buf = self._buf[bracket_idx:]

            # Now self._buf starts with `[`
            # Check if a newline occurred before closing `)` -> cannot be an inline file link
            nl_idx = self._buf.find("\n")
            close_bracket = self._buf.find("]")

            if close_bracket == -1:
                if nl_idx != -1 or len(self._buf) > _LINK_BUF_MAX_CHARS:
                    cut = nl_idx + 1 if nl_idx != -1 else len(self._buf)
                    out.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    continue
                break

            if nl_idx != -1 and nl_idx < close_bracket:
                out.append(self._buf[: nl_idx + 1])
                self._buf = self._buf[nl_idx + 1 :]
                continue

            # We have `[...]` — inspect what follows `]`
            after_idx = close_bracket + 1
            if after_idx >= len(self._buf):
                # Wait for next char to see if `(` follows
                break

            if self._buf[after_idx] != "(":
                # Not a markdown link `[label](...)` — emit up to `]` and continue
                out.append(self._buf[:after_idx])
                self._buf = self._buf[after_idx:]
                continue

            # We have `[...](` — check if URL prefix could be `file://` or `<file://`
            url_start = after_idx + 1
            url_part = self._buf[url_start:]
            prefix_ok = any(
                url_part.startswith(p) or p.startswith(url_part)
                for p in ("file://", "<file://")
            )
            if not prefix_ok:
                # Regular link like `[Docs](https://...)` — emit `[...](` immediately
                out.append(self._buf[:url_start])
                self._buf = self._buf[url_start:]
                continue

            close_paren = self._buf.find(")", url_start)
            if close_paren == -1:
                if nl_idx != -1 or len(self._buf) > _LINK_BUF_MAX_CHARS:
                    cut = nl_idx + 1 if nl_idx != -1 else len(self._buf)
                    out.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    continue
                break

            # Complete `[label](file://...)` candidate
            candidate = self._buf[: close_paren + 1]
            out.append(_clean_file_links(candidate))
            self._buf = self._buf[close_paren + 1 :]

        return "".join(out)

    def flush(self) -> str:
        if not self._buf:
            return ""
        rem = self._buf
        self._buf = ""
        return rem if self._in_fence else _clean_file_links(rem)


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


class WelcomeCard(Vertical):
    """Compact empty-state onboarding card shown in a fresh session until the first message arrives."""

    def __init__(
        self,
        *,
        version: str = "0.2.0",
        workspace: str = ".",
        branch: str = "",
        profile: str = "shared",
        subagents: str = "tier1_only",
        lang: str = "fr",
    ) -> None:
        super().__init__(classes="welcome-card")
        self.lang = lang
        self._version = version
        self._workspace = workspace
        self._branch = branch
        self._profile = profile
        self._subagents = subagents

    def compose(self) -> ComposeResult:
        branch_tag = f" ({self._branch})" if self._branch else ""
        yield Static(
            t(self.lang, "welcome_title", version=self._version),
            classes="welcome-title",
            markup=False,
        )
        yield Static(
            t(
                self.lang,
                "welcome_meta",
                workspace=self._workspace,
                branch=branch_tag,
                profile=self._profile,
                subagents=self._subagents,
            ),
            classes="welcome-meta",
            markup=False,
        )
        yield Static(
            t(self.lang, "welcome_tips"),
            classes="welcome-tips",
            markup=False,
        )

    def update_meta(
        self,
        *,
        branch: str | None = None,
        lang: str | None = None,
    ) -> None:
        if branch is not None:
            self._branch = branch
        if lang is not None:
            self.lang = lang
        if not self.is_mounted:
            return
        branch_tag = f" ({self._branch})" if self._branch else ""
        self.query_one(".welcome-title", Static).update(
            t(self.lang, "welcome_title", version=self._version)
        )
        self.query_one(".welcome-meta", Static).update(
            t(
                self.lang,
                "welcome_meta",
                workspace=self._workspace,
                branch=branch_tag,
                profile=self._profile,
                subagents=self._subagents,
            )
        )
        self.query_one(".welcome-tips", Static).update(t(self.lang, "welcome_tips"))


class AssistantMessage(Vertical):
    """Streamed assistant Markdown response with 200K plain-text guardrail and raw Markdown/code copy."""

    class CopyRequested(Message):
        def __init__(self, raw_text: str) -> None:
            super().__init__()
            self.raw_text = raw_text

    class CopyCodeRequested(Message):
        def __init__(self, raw_code: str) -> None:
            super().__init__()
            self.raw_code = raw_code

    def __init__(self, *, lang: str = "fr") -> None:
        super().__init__()
        self.lang = lang
        self.raw_markdown: str = ""
        self._started_at: float = time.time()
        self._md_widget: Markdown | None = None
        self._stream: MarkdownStream | None = None
        self._link_cleaner = _StreamingLinkCleaner()
        self._fallback_static: Static | None = None
        self._truncated: bool = False
        self._copy_code_btn: Button | None = None
        self._meta_label: Static | None = None

    def compose(self) -> ComposeResult:
        self._md_widget = Markdown("", parser_factory=create_safe_markdown_parser)
        yield self._md_widget
        with Horizontal(classes="msg-toolbar"):
            self._meta_label = Static("", classes="msg-meta", markup=False)
            self._meta_label.display = False
            yield self._meta_label
            self._copy_code_btn = Button(t(self.lang, "copy_code_btn"), classes="copy-code-btn")
            self._copy_code_btn.display = False
            yield self._copy_code_btn
            yield Button(t(self.lang, "copy_btn"), classes="copy-md-btn")

    def set_meta(self, text: str) -> None:
        if self._meta_label is not None:
            self._meta_label.update(text)
            self._meta_label.display = bool(text)

    def _sync_code_button(self) -> None:
        if self._copy_code_btn is not None:
            self._copy_code_btn.display = bool(self.extract_code_blocks())

    def apply_lang(self, lang: str) -> None:
        self.lang = lang
        if self.is_mounted:
            self.query_one(".copy-md-btn", Button).label = t(lang, "copy_btn")
            if self._copy_code_btn is not None:
                self._copy_code_btn.label = t(lang, "copy_code_btn")

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
            self._sync_code_button()
            return

        if self._md_widget is None:
            return
        if self._stream is None:
            self._stream = Markdown.get_stream(self._md_widget)
        cleaned = self._link_cleaner.feed(chunk)
        if cleaned:
            await self._stream.write(cleaned)
        if "```" in chunk:
            self._sync_code_button()

    async def finish_stream(self) -> None:
        tail = self._link_cleaner.flush()
        if tail and not self._truncated and self._md_widget is not None:
            if self._stream is None:
                self._stream = Markdown.get_stream(self._md_widget)
            try:
                await self._stream.write(tail)
            except Exception:
                pass
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            try:
                await stream.stop()
            except Exception:
                pass
        elapsed = max(0.0, time.time() - self._started_at)
        if elapsed >= 0.05 and self._meta_label is not None and not self._meta_label.display:
            self.set_meta(f"{elapsed:.1f}s")
        self._sync_code_button()

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

    @on(Button.Pressed, ".copy-code-btn")
    def _on_copy_code_pressed(self) -> None:
        blocks = self.extract_code_blocks()
        if blocks:
            self.post_message(self.CopyCodeRequested(blocks[-1]))


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


class PlanCard(Collapsible):
    """Live-updating ACP agent plan / task checklist (`plan` session_update)."""

    def __init__(self, entries: list[dict[str, str]], *, lang: str = "fr") -> None:
        self.lang = lang
        self.entries: list[dict[str, str]] = []
        self._body = Static("", classes="plan-body")
        super().__init__(self._body, title="Plan", collapsed=False)
        self.update_entries(entries)

    def update_entries(self, entries: list[dict[str, str]]) -> None:
        self.entries = list(entries)
        total = len(self.entries)
        done = sum(1 for e in self.entries if str(e.get("status", "")).lower() == "completed")
        pct = int(round((done / total) * 100)) if total else 0
        self.title = t(self.lang, "plan_title", done=done, total=total, pct=pct)
        out = Text()
        for idx, ent in enumerate(self.entries):
            status = str(ent.get("status") or "pending").lower()
            prio = str(ent.get("priority") or "medium").lower()
            content = str(ent.get("content") or "")
            if status == "completed":
                icon, style = "✓", "#3fb950"
            elif status in ("in_progress", "running"):
                icon, style = "●", "bold #6ea8fe"
            else:
                icon, style = "○", "#8b949e"
            out.append(f"{icon} ", style=style)
            out.append(content, style=style if status == "in_progress" else "")
            if prio == "high" and status != "completed":
                out.append(" [high]", style="bold #d29922")
            if idx < total - 1:
                out.append("\n")
        self._body.update(out)


class ShellCard(Collapsible):
    """Inline local shell execution card (`!cmd` / `!w cmd`) with one-click attach-to-prompt."""

    class AttachRequested(Message):
        def __init__(self, command: str, output: str, exit_code: int) -> None:
            super().__init__()
            self.command = command
            self.output = output
            self.exit_code = exit_code

    def __init__(self, command: str, *, wrapped: bool = False, lang: str = "fr") -> None:
        self.lang = lang
        self.command = command
        self.wrapped = wrapped
        self.output_text: str = ""
        self.exit_code: int | None = None
        self._detail = Static("…", classes="tool-detail", markup=False)
        self._attach_btn = Button(t(lang, "shell_attach_btn"), classes="shell-attach-btn")
        self._attach_btn.display = False
        body = Vertical(self._detail, Horizontal(self._attach_btn, classes="diff-summary-row"))
        prefix = "!w" if wrapped else "$"
        super().__init__(body, title=f"● {prefix} {command}", collapsed=False)
        self.add_class("shell-card", "tool-running")

    def set_result(self, *, exit_code: int, output: str, elapsed_s: float) -> None:
        self.exit_code = exit_code
        self.output_text = output
        self.remove_class("tool-running", "tool-ok", "tool-err")
        prefix = "!w" if self.wrapped else "$"
        if exit_code == 0:
            self.add_class("tool-ok")
            self.title = f"✓ {prefix} {self.command}  ·  {elapsed_s:.2f}s"
        else:
            self.add_class("tool-err")
            self.title = f"✗ {prefix} {self.command}  ·  exit {exit_code} · {elapsed_s:.2f}s"
        preview = output.strip() or "(no output)"
        if len(preview) > 12_000:
            preview = preview[:12_000] + f"\n\n[… truncated from {len(output):,} chars]"
        self._detail.update(preview)
        self._attach_btn.display = bool(output.strip())

    def apply_lang(self, lang: str) -> None:
        self.lang = lang
        if self.is_mounted:
            self._attach_btn.label = t(lang, "shell_attach_btn")

    @on(Button.Pressed, ".shell-attach-btn")
    def _on_attach_pressed(self) -> None:
        self.post_message(self.AttachRequested(self.command, self.output_text, int(self.exit_code or 0)))


class SessionTranscript(VerticalScroll):
    """Isolated per-session transcript with MarkdownStream, smart scroll anchor(), and tool cards."""

    class ScrollAnchorChanged(Message):
        def __init__(self, session_id: str, anchored: bool, has_unread: bool) -> None:
            super().__init__()
            self.session_id = session_id
            self.anchored = anchored
            self.has_unread = has_unread

    def __init__(
        self,
        session_id: str,
        *,
        lang: str = "fr",
        verbosity: str = "low",
        on_persist: Callable[[str, str, str], None] | None = None,
        welcome_info: dict[str, str] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.session_id = session_id
        self.lang = lang
        self.verbosity = verbosity  # "low" | "high"
        self._on_persist = on_persist
        self._welcome_info = welcome_info
        self._welcome_card: WelcomeCard | None = None

        self._has_unread: bool = False
        self._has_conversation_content: bool = False
        self._active_msg: AssistantMessage | None = None
        self._active_msg_id: str | None = None
        self._active_thought: ThoughtBlock | None = None
        self._active_user_widget: Static | None = None
        self._active_user_text: str = ""
        self._active_user_id: str | None = None
        self._active_group: ActivityGroup | None = None
        self._active_plan: PlanCard | None = None
        self._thoughts_expanded: bool = False
        self._tool_cards: dict[str, ToolCard] = {}
        self._grouped_tools: dict[str, ActivityGroup] = {}
        self._search_highlighted: Any | None = None

    def compose(self) -> ComposeResult:
        if self._welcome_info is not None:
            self._welcome_card = WelcomeCard(
                version=self._welcome_info.get("version", "0.2.0"),
                workspace=self._welcome_info.get("workspace", "."),
                branch=self._welcome_info.get("branch", ""),
                profile=self._welcome_info.get("profile", "shared"),
                subagents=self._welcome_info.get("subagents", "tier1_only"),
                lang=self.lang,
            )
            yield self._welcome_card

    def on_mount(self) -> None:
        self.anchor(True)

    def _hide_welcome(self) -> None:
        if self._welcome_card is not None:
            self._welcome_card.display = False

    def update_welcome_meta(self, *, branch: str | None = None, lang: str | None = None) -> None:
        if lang is not None:
            self.lang = lang
        if self._welcome_card is not None:
            self._welcome_card.update_meta(branch=branch, lang=lang)
        if lang is not None:
            for child in self.children:
                if isinstance(child, AssistantMessage):
                    child.apply_lang(lang)

    @property
    def has_conversation_content(self) -> bool:
        return self._has_conversation_content

    def pin_to_bottom(self) -> None:
        """Re-enable smart scroll anchor and jump to bottom (e.g. End key or pill click)."""
        self._has_unread = False
        self._anchor_released = False
        self.anchor(True)
        self.post_message(self.ScrollAnchorChanged(self.session_id, True, False))

    def detach_anchor(self) -> None:
        """Release smart scroll anchor when user scrolls up manually (0 scroll hijacking)."""
        if self.is_anchored:
            self.release_anchor()
            self.anchor(False)
            self.post_message(self.ScrollAnchorChanged(self.session_id, False, self._has_unread))

    def _mark_new_content(self) -> None:
        if not self.is_anchored:
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
        super().watch_scroll_y(old_value, new_value)
        if new_value < old_value and not self.is_vertical_scroll_end:
            self.detach_anchor()
        elif self.is_vertical_scroll_end and not self.is_anchored:
            self._has_unread = False
            self._anchor_released = False
            self.anchor(True)
            self.post_message(self.ScrollAnchorChanged(self.session_id, True, False))

    def _notify_persist(self, role: str, text: str) -> None:
        if self._on_persist is not None and text.strip():
            self._on_persist(self.session_id, role, text)

    async def close_active_message(self) -> None:
        if self._active_msg is not None:
            msg = self._active_msg
            self._active_msg = None
            self._active_msg_id = None
            await msg.finish_stream()
            self._notify_persist("assistant", msg.raw_markdown)

    def close_active_thought(self) -> None:
        if self._active_thought is not None:
            tb = self._active_thought
            self._active_thought = None
            tb.auto_collapse()
            self._notify_persist("thought", tb.raw_text)

    def close_active_user_message(self) -> None:
        if self._active_user_widget is not None:
            text = self._active_user_text
            self._active_user_widget = None
            self._active_user_text = ""
            self._active_user_id = None
            self._notify_persist("user", text)

    def _close_activity_group(self) -> None:
        self._active_group = None

    async def append_user_message(self, display_text: str) -> None:
        await self.close_active_message()
        self.close_active_thought()
        self.close_active_user_message()
        self._close_activity_group()
        self._has_conversation_content = True
        self._hide_welcome()
        await self.mount(Static(f"› {display_text}", classes="user-msg", markup=False))
        self._notify_persist("user", display_text)
        self._mark_new_content()

    async def append_user_chunk(self, text: str, *, message_id: str | None = None) -> None:
        """Append a streamed user message chunk (used during ACP `session/load` history replay)."""
        await self.close_active_message()
        self.close_active_thought()
        self._close_activity_group()
        if (
            self._active_user_widget is not None
            and message_id is not None
            and self._active_user_id is not None
            and message_id != self._active_user_id
        ):
            self.close_active_user_message()
        self._has_conversation_content = True
        self._hide_welcome()
        if self._active_user_widget is None:
            self._active_user_text = text
            self._active_user_id = message_id
            self._active_user_widget = Static(f"› {self._active_user_text}", classes="user-msg", markup=False)
            await self.mount(self._active_user_widget)
        else:
            self._active_user_text += text
            self._active_user_widget.update(f"› {self._active_user_text}")
        self._mark_new_content()

    async def append_notice(self, message: str, *, level: str = "info") -> None:
        await self.close_active_message()
        self.close_active_thought()
        self.close_active_user_message()
        self._close_activity_group()
        css_cls = "notice-line"
        if level == "warn":
            css_cls = "notice-line notice-warn"
        elif level == "err":
            css_cls = "notice-line notice-err"
        await self.mount(Static(f"ogy · {message}", classes=css_cls, markup=False))
        self._mark_new_content()

    async def append_assistant_chunk(self, text: str, *, message_id: str | None = None) -> None:
        self.close_active_thought()
        self.close_active_user_message()
        self._close_activity_group()
        if (
            self._active_msg is not None
            and message_id is not None
            and self._active_msg_id is not None
            and message_id != self._active_msg_id
        ):
            await self.close_active_message()
        self._has_conversation_content = True
        self._hide_welcome()
        if self._active_msg is None:
            msg = AssistantMessage(lang=self.lang)
            self._active_msg = msg
            self._active_msg_id = message_id
            await self.mount(msg)
        await self._active_msg.append_chunk(text)
        self._mark_new_content()

    async def append_thought_chunk(self, text: str) -> None:
        await self.close_active_message()
        self.close_active_user_message()
        self._close_activity_group()
        self._has_conversation_content = True
        self._hide_welcome()
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
        self.close_active_user_message()
        self._has_conversation_content = True
        self._hide_welcome()

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

    async def update_plan(self, entries: list[dict[str, str]]) -> None:
        """Mount or update the live ACP agent plan/checklist card in place."""
        if not entries:
            return
        self._has_conversation_content = True
        self._hide_welcome()
        if self._active_plan is None:
            card = PlanCard(entries, lang=self.lang)
            self._active_plan = card
            await self.mount(card)
        else:
            self._active_plan.update_entries(entries)
        self._mark_new_content()

    async def mount_shell_card(self, command: str, *, wrapped: bool = False) -> ShellCard:
        """Mount an inline local shell execution card (`!cmd` / `!w cmd`)."""
        await self.close_active_message()
        self.close_active_thought()
        self.close_active_user_message()
        self._close_activity_group()
        self._has_conversation_content = True
        self._hide_welcome()
        card = ShellCard(command, wrapped=wrapped, lang=self.lang)
        await self.mount(card)
        self._mark_new_content()
        return card

    def toggle_all_thoughts(self) -> bool:
        """Toggle expanded/collapsed state of all ThoughtBlock drawers in this transcript."""
        self._thoughts_expanded = not self._thoughts_expanded
        for child in self.children:
            if isinstance(child, ThoughtBlock):
                child.set_verbosity(self._thoughts_expanded)
        return self._thoughts_expanded

    def set_verbosity(self, verbosity: str) -> None:
        self.verbosity = verbosity
        high = verbosity == "high"
        for child in self.children:
            if isinstance(child, (ActivityGroup, ThoughtBlock, ToolCard)):
                child.set_verbosity(high)

    def get_last_assistant_message(self) -> AssistantMessage | None:
        for child in reversed(list(self.children)):
            if isinstance(child, AssistantMessage) and child.raw_markdown.strip():
                return child
        return None

    def get_last_code_block(self) -> str | None:
        for child in reversed(list(self.children)):
            if isinstance(child, AssistantMessage):
                blocks = child.extract_code_blocks()
                if blocks:
                    return blocks[-1]
        return None

    def _widget_searchable_text(self, widget: Any) -> str:
        if isinstance(widget, WelcomeCard):
            return ""
        if isinstance(widget, AssistantMessage):
            return widget.raw_markdown
        if isinstance(widget, ThoughtBlock):
            return widget.raw_text
        if isinstance(widget, PlanCard):
            return f"{widget.title}\n" + "\n".join(str(e.get("content") or "") for e in widget.entries)
        if isinstance(widget, ShellCard):
            return f"{widget.title}\n{widget.command}\n{widget.output_text}"
        if isinstance(widget, ToolCard):
            return f"{widget.title}\n{widget.event.name}\n{widget.event.raw_input}\n{widget.event.raw_output}"
        if isinstance(widget, ActivityGroup):
            return f"{widget.title}\n" + "\n".join(line for _, line in widget._entries.values())
        if isinstance(widget, Static):
            return str(widget.render())
        return ""

    def search_widgets(self, query: str) -> list[Any]:
        """Return transcript child widgets matching case-insensitive `query`."""
        q = query.strip().lower()
        if not q:
            return []
        matches: list[Any] = []
        for child in self.children:
            if not child.display:
                continue
            if q in self._widget_searchable_text(child).lower():
                matches.append(child)
        return matches

    def highlight_search_match(self, widget: Any | None) -> None:
        """Highlight the active search match widget and scroll it into view."""
        if self._search_highlighted is not None:
            try:
                self._search_highlighted.remove_class("search-match-active")
            except Exception:
                pass
            self._search_highlighted = None
        if widget is not None:
            self._search_highlighted = widget
            widget.add_class("search-match-active")
            try:
                self.detach_anchor()
                widget.scroll_visible(animate=False)
            except Exception:
                pass

    def export_markdown(self, header_meta: dict[str, Any] | None = None) -> str:
        """Export user & assistant conversation messages to a clean Markdown document."""
        meta = header_meta or {}
        lines: list[str] = [
            f"# Optigravity Session `{self.session_id}`",
            "",
        ]
        if meta:
            meta_parts = [f"- **{k}**: `{v}`" for k, v in meta.items() if v is not None and str(v) != ""]
            if meta_parts:
                lines.extend(meta_parts)
                lines.append("")
        lines.append("---")
        lines.append("")

        msg_count = 0
        for child in self.children:
            if isinstance(child, WelcomeCard):
                continue
            if isinstance(child, Static) and child.has_class("user-msg"):
                txt = str(child.render()).removeprefix("› ").strip()
                if txt:
                    msg_count += 1
                    lines.extend(["## User", "", txt, ""])
            elif isinstance(child, AssistantMessage) and child.raw_markdown.strip():
                msg_count += 1
                lines.extend(["## Assistant", "", child.raw_markdown.strip(), ""])

        if msg_count == 0:
            return ""
        return "\n".join(lines).rstrip() + "\n"
