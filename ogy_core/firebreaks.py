"""Deterministic firebreaks, shell compression (`ogy wrap`), and stash recall (100% stdlib)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from ogy_core.config import OgyConfig, load_config
from ogy_core.storage import canonical_args_hash, query_all

LOCKFILE_NAMES = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "uv.lock",
    "poetry.lock",
    "composer.lock",
    "gemfile.lock",
})

DOC_EXTENSIONS = frozenset({
    ".md",
    ".rst",
    ".toml",
    ".yaml",
    ".yml",
    ".txt",
    ".ini",
    ".cfg",
    ".editorconfig",
})

DATA_LOG_EXTENSIONS = frozenset({
    ".log",
    ".json",
    ".jsonl",
    ".lock",
    ".csv",
    ".tsv",
    ".xml",
    ".sql",
    ".map",
})

VERBOSE_CMD_RE = re.compile(
    r"^\s*(?:(?:uv\s+run|poetry\s+run|python3?(?:\.exe)?\s+-m)\s+)?(pytest|py\.test)\b"
    r"|^\s*(cargo\s+test|npm\s+test|pnpm\s+test|vitest|jest|go\s+test|git\s+log|git\s+diff|git\s+status)\b",
    re.IGNORECASE,
)

EXCLUDED_MUTATING_RE = re.compile(
    r"\b(git\s+(commit|push|rebase|merge|cherry-pick|reset|tag)|rm\b|del\b|remove-item|rmdir)\b",
    re.IGNORECASE,
)

BOUNDING_CONSUMERS_RE = re.compile(
    r"^\s*(head|tail|wc|grep|egrep|fgrep|rg|awk|sed|jq|cut|sort|uniq|Select-Object|Select-String|Measure-Object|Out-Null)\b",
    re.IGNORECASE,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

SMALL_OUTPUT_MAX_LINES = 40
SMALL_OUTPUT_MAX_BYTES = 3_072

STASH_GC_MAX_AGE_DAYS = 7.0
STASH_GC_MAX_BYTES = 200 * 1024 * 1024
STASH_GC_INTERVAL_S = 3600.0


# ---------------------------------------------------------------------------
# Tool-aware i18n messages (EN default, FR optional; never hardcodes grep_search)
# ---------------------------------------------------------------------------

_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "loop_detected": "[ogy] Error loop detected on this call; change approach before retrying.",
        "lockfile_blocked": (
            "[ogy] Reading lockfile '{name}' is blocked. "
            "Use targeted search or specify {range_params} if a small slice is needed."
        ),
        "large_file_blocked": (
            "[ogy] Large {kind} file '{name}' ({size_kb:.1f} KB, {lines} lines > {limit_kb:.0f} KB limit). "
            "Read a bounded slice using {range_params} (e.g. {example})."
        ),
        "verbose_cmd": "[ogy] Verbose command detected. Re-run with: {wrapped}",
    },
    "fr": {
        "loop_detected": "[ogy] Boucle d'erreur detectee sur cet appel, change d'approche.",
        "lockfile_blocked": (
            "[ogy] Lecture de lockfile bloquee ({name}). "
            "Utilise une recherche ciblee ou specifie {range_params}."
        ),
        "large_file_blocked": (
            "[ogy] Fichier {kind} volumineux '{name}' ({size_kb:.1f} Ko, {lines} lignes > seuil {limit_kb:.0f} Ko). "
            "Specifie une tranche bornee avec {range_params} (ex. {example})."
        ),
        "verbose_cmd": "[ogy] Commande verbeuse detectee. Relance avec: {wrapped}",
    },
}


def _msg(lang: str, key: str, **kwargs: Any) -> str:
    table = _MESSAGES.get(lang.lower(), _MESSAGES["en"])
    tmpl = table.get(key) or _MESSAGES["en"][key]
    return tmpl.format(**kwargs)


# ---------------------------------------------------------------------------
# Lightweight stdlib shell tokenizer (PowerShell & Bash compatible)
# ---------------------------------------------------------------------------


def tokenize_shell_command(cmd: str) -> tuple[list[tuple[str, str]], bool]:
    """Split a command on top-level operators (`&&`, `||`, `;`, `|`) outside quotes/parens.

    Returns `(segments, has_advanced_syntax)` where `segments` is a list of
    `(segment_text, following_operator)` pairs and `has_advanced_syntax` is True when
    redirections (`>`, `>>`, `<<`, `|&`), subshells (`$(...)`), or PowerShell blocks
    (`{ ... }`) are present.
    """
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    paren_depth = 0
    brace_depth = 0
    has_advanced = False

    n = len(cmd)
    i = 0
    while i < n:
        ch = cmd[i]
        nxt = cmd[i + 1] if i + 1 < n else ""

        # Escape handling inside double quotes (bash \ or PowerShell `)
        if in_double:
            if ch in ("\\", "`") and nxt:
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue
            if ch == "$" and nxt == "(":
                has_advanced = True
            if ch == '"':
                in_double = False
            buf.append(ch)
            i += 1
            continue

        if in_single:
            if ch == "'":
                # PowerShell escaped single quote ''
                if nxt == "'":
                    buf.append("''")
                    i += 2
                    continue
                in_single = False
            buf.append(ch)
            i += 1
            continue

        # Outside quotes
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue

        # Escape outside quotes
        if ch == "`" and nxt:
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue

        # Subexpression / command substitution $(...)
        if ch == "$" and nxt == "(":
            has_advanced = True
            paren_depth += 1
            buf.append("$(")
            i += 2
            continue

        if ch == "(":
            paren_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(ch)
            i += 1
            continue

        # PowerShell / bash script blocks { ... }
        if ch == "{":
            has_advanced = True
            brace_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == "}":
            has_advanced = True
            brace_depth = max(0, brace_depth - 1)
            buf.append(ch)
            i += 1
            continue

        # Redirections & |&
        if ch == "|" and nxt == "&":
            has_advanced = True
            buf.append("|&")
            i += 2
            continue
        if ch == ">":
            has_advanced = True
            if nxt == ">":
                buf.append(">>")
                i += 2
            else:
                buf.append(">")
                i += 1
            continue
        if ch == "<" and nxt == "<":
            has_advanced = True
            buf.append("<<")
            i += 2
            continue

        # Top-level operators
        if paren_depth == 0 and brace_depth == 0:
            if ch == "&" and nxt == "&":
                segments.append(("".join(buf).strip(), "&&"))
                buf.clear()
                i += 2
                continue
            if ch == "|" and nxt == "|":
                segments.append(("".join(buf).strip(), "||"))
                buf.clear()
                i += 2
                continue
            if ch == ";":
                segments.append(("".join(buf).strip(), ";"))
                buf.clear()
                i += 1
                continue
            if ch == "|":
                segments.append(("".join(buf).strip(), "|"))
                buf.clear()
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail or not segments:
        segments.append((tail, ""))

    # Unclosed quotes/parens -> treat as advanced to stay strictly fail-open
    if in_single or in_double or paren_depth != 0 or brace_depth != 0:
        has_advanced = True

    return segments, has_advanced


# ---------------------------------------------------------------------------
# Bounded-aware command detector & surgical rewriter
# ---------------------------------------------------------------------------


def is_command_bounded(seg: str) -> bool:
    """Return True if a shell command segment already bounds its output."""
    s = seg.strip()
    low = s.lower()

    if low.startswith(("ogy wrap", "ogy raw", "ogy_no_wrap=1 ")):
        return True
    if re.match(r"^\s*\$env:OGY_NO_WRAP\s*=\s*['\"]?1['\"]?", s, re.IGNORECASE):
        return True

    # pytest family
    if re.search(r"\b(pytest|py\.test)\b", low):
        if re.search(r"(^|\s)(-q|--quiet|--tb=(short|line|no)|--maxfail(=|\s|$)|-x\b|--exitfirst|-k\s|--co\b|--collect-only)", s):
            return True
        return False

    # git log
    if re.match(r"^\s*git\s+log\b", low):
        if re.search(r"(^|\s)(-n\s*\d+|-n\d+|-\d+\b|--max-count(=|\s)\d+|--oneline|--stat|--shortstat|--name-only|--name-status)", s):
            return True
        return False

    # git diff
    if re.match(r"^\s*git\s+diff\b", low):
        if re.search(r"(^|\s)(--stat|--shortstat|--numstat|--name-only|--name-status|--summary|--quiet|--exit-code)", s):
            return True
        return False

    # git status
    if re.match(r"^\s*git\s+status\b", low):
        if re.search(r"(^|\s)(-s\b|--short|--porcelain)", s):
            return True
        return False

    # cargo / npm / pnpm / vitest / jest / go test
    if re.match(r"^\s*cargo\s+test\b", low):
        return bool(re.search(r"(^|\s)(-q\b|--quiet|--list)", s))
    if re.match(r"^\s*go\s+test\b", low):
        # `go test` without `-v` is already concise
        return not bool(re.search(r"(^|\s)-v\b", s))
    if re.match(r"^\s*(npm\s+test|pnpm\s+test|vitest|jest)\b", low):
        return bool(re.search(r"(^|\s)(--silent|--bail|-t\s)", s))

    return False


def _rewrite_shell_command(cmd: str) -> str | None:
    """Surgically wrap unbounded verbose segments in a shell command.

    Returns the rewritten command string, or None if the command should pass through.
    """
    stripped = cmd.strip()
    if not stripped:
        return None
    if os.environ.get("OGY_NO_WRAP") == "1":
        return None
    if re.match(r"^(OGY_NO_WRAP=1\s+|ogy\s+raw\s+(--\s+)?|ogy\s+wrap\b)", stripped, re.IGNORECASE):
        return None
    if EXCLUDED_MUTATING_RE.search(stripped):
        return None

    segments, has_advanced = tokenize_shell_command(stripped)
    if has_advanced:
        return None

    # Group segments into pipelines separated by sequence operators (&&, ||, ;)
    groups: list[tuple[list[str], str]] = []
    cur_pipe: list[str] = []
    for seg_text, op in segments:
        cur_pipe.append(seg_text)
        if op != "|":
            groups.append((cur_pipe, op))
            cur_pipe = []
    if cur_pipe:
        groups.append((cur_pipe, ""))

    changed = False
    rebuilt_parts: list[str] = []
    for pipe_stages, seq_op in groups:
        if len(pipe_stages) > 1:
            # Never mutate intermediate producers in a pipeline; pass through intact.
            # (Whether the final consumer bounds output or transforms it, wrapping
            # an intermediate stage would corrupt the downstream pipe.)
            rebuilt_parts.append(" | ".join(pipe_stages))
        else:
            seg = pipe_stages[0]
            if (
                seg
                and not EXCLUDED_MUTATING_RE.search(seg)
                and VERBOSE_CMD_RE.search(seg)
                and not is_command_bounded(seg)
            ):
                rebuilt_parts.append(f"ogy wrap -- {seg}")
                changed = True
            else:
                rebuilt_parts.append(seg)
        if seq_op:
            rebuilt_parts.append(f" {seq_op} " if seq_op != ";" else "; ")

    if not changed:
        return None
    return "".join(rebuilt_parts).strip()


# ---------------------------------------------------------------------------
# Adaptive file-size thresholds & exact line counting
# ---------------------------------------------------------------------------


def _classify_file_limit(path: Path, cfg: OgyConfig) -> tuple[str, int]:
    """Return `(category_label, max_bytes)` for a target file."""
    name_low = path.name.lower()
    suffix_low = path.suffix.lower()
    if name_low.endswith((".min.js", ".min.css")) or suffix_low in DATA_LOG_EXTENSIONS:
        return "data/log", cfg.firebreak_data_max_bytes
    if suffix_low in DOC_EXTENSIONS:
        return "doc/config", cfg.firebreak_doc_max_bytes
    return "source", cfg.firebreak_file_max_bytes


def _count_file_lines(path: Path) -> int:
    """Fast stdlib exact line count in binary mode."""
    try:
        total = 0
        last_byte = b""
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(65_536)
                if not chunk:
                    break
                total += chunk.count(b"\n")
                last_byte = chunk[-1:]
        if total == 0 and last_byte:
            return 1
        if last_byte and last_byte != b"\n":
            total += 1
        return total
    except OSError:
        return 0


def _range_param_names(tool_args: dict[str, Any]) -> tuple[str, str]:
    """Infer tool-aware line range parameter names from tool_args conventions."""
    if any(k in tool_args for k in ("path", "start_line", "end_line")):
        return "start_line/end_line", "start_line=1, end_line=120"
    return "StartLine/EndLine", "StartLine=1, EndLine=120"


# ---------------------------------------------------------------------------
# PreToolUse firebreak evaluator (agy 1.5.16 protocol: always {"decision": "allow"} on pass-through)
# ---------------------------------------------------------------------------


def evaluate_pre_tool_firebreaks(
    conn: sqlite3.Connection | None,
    *,
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    cfg: OgyConfig,
) -> dict[str, Any]:
    """Evaluate PreToolUse firebreaks. Nominal allow response is ALWAYS {'decision': 'allow'}."""
    name = (tool_name or "").strip()
    args_hash = canonical_args_hash(tool_args)
    lang = cfg.language

    # Level 3 — Firebreak 1: Anti-loop (same args_hash failed >= threshold times in session)
    if conn is not None and session_id:
        rows = query_all(
            conn,
            """
            SELECT SUM(is_error) AS err_cnt
            FROM tool_calls
            WHERE session_id = ? AND args_hash = ?
            """,
            (session_id, args_hash),
        )
        if rows and (rows[0]["err_cnt"] or 0) >= cfg.firebreak_loop_threshold:
            return {
                "decision": "deny",
                "reason": _msg(lang, "loop_detected"),
            }

    # Level 3 — Firebreak 2: Adaptive file read thresholds on view_file / read_file
    if name in ("view_file", "read_file"):
        target = str(tool_args.get("Path") or tool_args.get("AbsolutePath") or tool_args.get("path") or "")
        start_line = tool_args.get("StartLine") if tool_args.get("StartLine") is not None else tool_args.get("start_line")
        end_line = tool_args.get("EndLine") if tool_args.get("EndLine") is not None else tool_args.get("end_line")
        range_params, example = _range_param_names(tool_args)
        if target:
            p = Path(target)
            if p.name.lower() in LOCKFILE_NAMES:
                return {
                    "decision": "deny",
                    "reason": _msg(lang, "lockfile_blocked", name=p.name, range_params=range_params),
                }
            if start_line is None and end_line is None and p.is_file():
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                kind, limit_bytes = _classify_file_limit(p, cfg)
                if size > limit_bytes:
                    line_count = _count_file_lines(p)
                    return {
                        "decision": "deny",
                        "reason": _msg(
                            lang,
                            "large_file_blocked",
                            kind=kind,
                            name=p.name,
                            size_kb=size / 1024.0,
                            lines=line_count,
                            limit_kb=limit_bytes / 1024.0,
                            range_params=range_params,
                            example=example,
                        ),
                    }

    # Level 1 — Firebreak 3: Surgical shell compression (ogy wrap)
    if name in ("run_command", "bash", "shell", "execute_command"):
        cmd_key = "CommandLine" if "CommandLine" in tool_args else ("command" if "command" in tool_args else "CommandLine")
        cmd = str(tool_args.get("CommandLine") or tool_args.get("command") or "").strip()
        if cmd:
            rewritten = _rewrite_shell_command(cmd)
            if rewritten is not None:
                if cfg.overwrite_verified_ok:
                    return {
                        "decision": "allow",
                        "overwrite": {cmd_key: rewritten},
                    }
                return {
                    "decision": "deny",
                    "reason": _msg(lang, "verbose_cmd", wrapped=rewritten),
                }

    return {"decision": "allow"}


# ---------------------------------------------------------------------------
# Shell output compression, split stdout/stderr stash, recall & LRU GC (Lot 1.3)
# ---------------------------------------------------------------------------


def compress_shell_output(raw_text: str, *, exit_code: int) -> str:
    """Deterministic shell output compressor preserving errors, tracebacks, and summaries."""
    cleaned = ANSI_RE.sub("", raw_text or "")
    lines = cleaned.splitlines()
    if len(lines) <= 25:
        return cleaned

    kept: list[str] = []
    important_re = re.compile(
        r"(ERROR|FAILED|FAIL|WARN|Exception|Traceback|AssertionError|^\s*E\s+|passed|failed|skipped|warning|^\s*at\s+)",
        re.IGNORECASE,
    )
    for ln in lines:
        if important_re.search(ln):
            kept.append(ln)

    if exit_code != 0 and len(kept) < 10:
        kept = lines[-30:]
    elif not kept:
        kept = lines[:5] + [f"... [{len(lines) - 10} lines omitted] ..."] + lines[-5:]

    compressed = "\n".join(kept)
    if len(compressed) >= int(len(cleaned) * 0.90):
        return cleaned
    return compressed


def _entry_size_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _remove_stash_entry(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def gc_stash_dir(
    stash_dir: Path | None = None,
    *,
    max_age_days: float = STASH_GC_MAX_AGE_DAYS,
    max_total_bytes: int = STASH_GC_MAX_BYTES,
    min_interval_s: float = STASH_GC_INTERVAL_S,
    force: bool = False,
) -> dict[str, int]:
    """Opportunistic LRU garbage collector for stash_dir (max 1x/hour via .last_gc)."""
    target_dir = stash_dir or load_config().stash_dir
    if not target_dir.is_dir():
        return {"removed_count": 0, "freed_bytes": 0}

    marker = target_dir / ".last_gc"
    now = time.time()
    if not force and marker.is_file():
        try:
            if (now - marker.stat().st_mtime) < min_interval_s:
                return {"removed_count": 0, "freed_bytes": 0}
        except OSError:
            pass

    try:
        marker.write_text(f"{now:.3f}", encoding="utf-8")
    except OSError:
        pass

    entries: list[tuple[Path, float, int]] = []
    try:
        for child in target_dir.iterdir():
            if child.name == ".last_gc":
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            entries.append((child, mtime, _entry_size_bytes(child)))
    except OSError:
        return {"removed_count": 0, "freed_bytes": 0}

    removed_count = 0
    freed_bytes = 0
    cutoff = now - (max_age_days * 86400.0)
    remaining: list[tuple[Path, float, int]] = []

    for entry_path, mtime, size_b in entries:
        if mtime < cutoff:
            _remove_stash_entry(entry_path)
            removed_count += 1
            freed_bytes += size_b
        else:
            remaining.append((entry_path, mtime, size_b))

    total_bytes = sum(item[2] for item in remaining)
    if total_bytes > max_total_bytes:
        # Evict oldest (LRU by mtime) first
        remaining.sort(key=lambda item: item[1])
        for entry_path, _mtime, size_b in remaining:
            if total_bytes <= max_total_bytes:
                break
            _remove_stash_entry(entry_path)
            total_bytes -= size_b
            removed_count += 1
            freed_bytes += size_b

    return {"removed_count": removed_count, "freed_bytes": freed_bytes}


def stash_raw_output(raw_text: str, *, stash_dir: Path | None = None) -> str:
    """Save legacy single-stream raw output to stash_dir/<sha256>.log and return digest."""
    target_dir = stash_dir or load_config().stash_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
    out_file = target_dir / f"{digest}.log"
    if not out_file.exists():
        out_file.write_text(raw_text, encoding="utf-8", errors="replace")
    gc_stash_dir(target_dir)
    return digest


def stash_wrapped_output(
    stdout_text: str,
    stderr_text: str,
    *,
    cmd: Sequence[str],
    exit_code: int,
    duration_s: float,
    stash_dir: Path | None = None,
) -> str:
    """Archive separated stdout.log, stderr.log, and meta.json under stash_dir/<hash>/."""
    target_dir = stash_dir or load_config().stash_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    payload_for_hash = f"{stdout_text}\n---STDERR---\n{stderr_text}"
    digest = hashlib.sha256(payload_for_hash.encode("utf-8", errors="replace")).hexdigest()
    entry_dir = target_dir / digest
    entry_dir.mkdir(parents=True, exist_ok=True)

    (entry_dir / "stdout.log").write_text(stdout_text, encoding="utf-8", errors="replace", newline="\n")
    (entry_dir / "stderr.log").write_text(stderr_text, encoding="utf-8", errors="replace", newline="\n")
    meta = {
        "command": list(cmd),
        "exit_code": int(exit_code),
        "stdout_bytes": len(stdout_text.encode("utf-8", errors="replace")),
        "stderr_bytes": len(stderr_text.encode("utf-8", errors="replace")),
        "stdout_lines": len(stdout_text.splitlines()),
        "stderr_lines": len(stderr_text.splitlines()),
        "duration_s": round(duration_s, 3),
        "created_at": time.time(),
    }
    (entry_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    gc_stash_dir(target_dir)
    return digest


def recall_stashed_output(
    hash_prefix: str,
    *,
    lines_spec: str | None = None,
    stream: str = "stdout",
    stash_dir: Path | None = None,
) -> str:
    """Read stashed raw output by hash prefix (supports v2 dir format and legacy .log files)."""
    target_dir = stash_dir or load_config().stash_dir
    prefix = (hash_prefix or "").strip().lower()
    if not prefix or not target_dir.is_dir():
        return f"[ogy recall] Hash not found: {hash_prefix}"

    text: str | None = None

    # 1. Prefer v2 directory entries: stash_dir/<hash>/{stdout.log, stderr.log, meta.json}
    dir_matches = sorted(p for p in target_dir.glob(f"{prefix}*") if p.is_dir())
    if dir_matches:
        entry = dir_matches[0]
        # Touch mtime for LRU tracking
        try:
            os.utime(entry, None)
        except OSError:
            pass
        out_p = entry / "stdout.log"
        err_p = entry / "stderr.log"
        out_txt = out_p.read_text(encoding="utf-8", errors="replace") if out_p.is_file() else ""
        err_txt = err_p.read_text(encoding="utf-8", errors="replace") if err_p.is_file() else ""
        if stream == "stderr":
            text = err_txt
        elif stream == "both":
            text = f"{out_txt}\n--- stderr ---\n{err_txt}" if (out_txt and err_txt) else (out_txt or err_txt)
        else:
            text = out_txt if out_txt else err_txt

    # 2. Backwards-compatible fallback: legacy flat stash_dir/<hash>.log
    if text is None:
        log_matches = sorted(target_dir.glob(f"{prefix}*.log"))
        if not log_matches:
            return f"[ogy recall] No stashed output matching '{hash_prefix}'"
        text = log_matches[0].read_text(encoding="utf-8", errors="replace")

    if not lines_spec:
        return text

    parts = lines_spec.split(":", 1)
    try:
        start = max(1, int(parts[0])) if parts[0] else 1
        end = int(parts[1]) if len(parts) > 1 and parts[1] else None
    except ValueError:
        return text

    all_lines = text.splitlines()
    sliced = all_lines[start - 1 : end]
    return "\n".join(sliced)


def _drain_pipe(
    stream: Any,
    chunks: list[str],
    state: dict[str, int],
    lock: threading.Lock,
) -> None:
    """Drain a Popen binary pipe in a daemon thread (Windows & POSIX deadlock-free)."""
    try:
        for raw_line in iter(stream.readline, b""):
            decoded = raw_line.decode("utf-8", errors="replace").replace("\r\n", "\n")
            chunks.append(decoded)
            with lock:
                state["lines"] += 1
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_wrapped_command(cmd: Sequence[str], *, stash_dir: Path | None = None) -> int:
    """Execute command with 2 daemon reader threads, TTY-only spinner on stderr,
    small-output pass-through (<40 lines & <3KB), separated stdout/stderr, and LRU stash.
    """
    if not cmd:
        print("[ogy wrap] No command provided.", file=sys.stderr)
        return 2

    cmd_str = " ".join(cmd)
    if EXCLUDED_MUTATING_RE.search(cmd_str):
        proc = subprocess.run(list(cmd))
        return int(proc.returncode)

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print(f"[ogy wrap] Command not found: {cmd[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"[ogy wrap] Failed to execute command: {exc}", file=sys.stderr)
        return 1

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    state = {"lines": 0}
    lock = threading.Lock()

    t_out = threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_chunks, state, lock), daemon=True)
    t_err = threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_chunks, state, lock), daemon=True)
    t_out.start()
    t_err.start()

    is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
    short_cmd = cmd_str if len(cmd_str) <= 36 else cmd_str[:33] + "..."
    frame_idx = 0
    spinner_shown = False
    last_spin_len = 0

    try:
        while proc.poll() is None:
            if is_tty:
                elapsed = int(time.monotonic() - t0)
                with lock:
                    line_cnt = state["lines"]
                frame = SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]
                frame_idx += 1
                msg = f"\r{frame} ogy wrap · {short_cmd} · {elapsed}s · {line_cnt} lines"
                last_spin_len = max(last_spin_len, len(msg))
                try:
                    sys.stderr.write(msg)
                    sys.stderr.flush()
                    spinner_shown = True
                except OSError:
                    is_tty = False
            time.sleep(0.25 if is_tty else 0.05)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130
    finally:
        if spinner_shown:
            try:
                sys.stderr.write("\r" + (" " * last_spin_len) + "\r")
                sys.stderr.flush()
            except OSError:
                pass

    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)
    duration_s = time.monotonic() - t0
    rc = int(proc.returncode or 0)

    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)

    total_lines = len(stdout_text.splitlines()) + len(stderr_text.splitlines())
    total_bytes = len(stdout_text.encode("utf-8", errors="replace")) + len(
        stderr_text.encode("utf-8", errors="replace")
    )

    # Small output pass-through (< 40 lines AND < 3 KB): zero overhead, no stash created
    if total_lines < SMALL_OUTPUT_MAX_LINES and total_bytes < SMALL_OUTPUT_MAX_BYTES:
        if stdout_text:
            sys.stdout.write(stdout_text)
            sys.stdout.flush()
        if stderr_text:
            sys.stderr.write(stderr_text)
            sys.stderr.flush()
        return rc

    compressed_out = compress_shell_output(stdout_text, exit_code=rc)
    compressed_err = (
        compress_shell_output(stderr_text, exit_code=rc)
        if len(stderr_text.splitlines()) >= SMALL_OUTPUT_MAX_LINES
        else stderr_text
    )

    did_compress = (compressed_out != stdout_text) or (compressed_err != stderr_text)
    if did_compress or rc != 0:
        digest = stash_wrapped_output(
            stdout_text,
            stderr_text,
            cmd=cmd,
            exit_code=rc,
            duration_s=duration_s,
            stash_dir=stash_dir,
        )
        if compressed_out:
            sys.stdout.write(compressed_out)
            if not compressed_out.endswith("\n"):
                sys.stdout.write("\n")
        if compressed_err:
            sys.stderr.write(compressed_err)
            if not compressed_err.endswith("\n"):
                sys.stderr.write("\n")
        raw_total = len(stdout_text) + len(stderr_text)
        out_total = len(compressed_out) + len(compressed_err)
        if out_total < raw_total or rc != 0:
            sys.stdout.write(
                f"[ogy wrap] exit={rc} raw={raw_total}B out={out_total}B recall={digest[:12]}\n"
            )
    else:
        if stdout_text:
            sys.stdout.write(stdout_text)
        if stderr_text:
            sys.stderr.write(stderr_text)

    sys.stdout.flush()
    sys.stderr.flush()
    return rc
