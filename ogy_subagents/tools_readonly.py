"""Strictly read-only local tools for Level 2 MCP subagents (ToS Section 6 & anti-conflict).

Every tool is scoped to `workspace_root` (no path traversal) and never mutates the filesystem.
All outputs are redacted through `redact_secrets()` before returning to the model.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from ogy_subagents.secrets import redact_secrets

SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
})

MAX_SLICE_LINES = 200
MAX_TOOL_OUTPUT_CHARS = 12_000


def _resolve_within_workspace(workspace_root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` strictly inside `workspace_root`, raising ValueError on escape."""
    root = workspace_root.resolve()
    candidate = (root / (rel_path or ".")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Access denied outside workspace root: {rel_path}") from exc
    return candidate


def _truncate_output(text: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    redacted = redact_secrets(text)
    if len(redacted) <= max_chars:
        return redacted
    return redacted[:max_chars] + f"\n... [truncated {len(redacted) - max_chars} chars]"


def rg_search(
    workspace_root: Path,
    pattern: str,
    *,
    scope_paths: list[str] | None = None,
    max_matches: int = 40,
) -> str:
    """Search for regex `pattern` in `workspace_root` (uses `rg` if available, else Python fallback)."""
    root = workspace_root.resolve()
    if not pattern or not pattern.strip():
        return "Error: empty search pattern."
    cap = max(1, min(int(max_matches), 100))

    targets: list[Path] = []
    if scope_paths:
        for sp in scope_paths:
            try:
                p = _resolve_within_workspace(root, sp)
                if p.exists():
                    targets.append(p)
            except ValueError:
                continue
    if not targets:
        targets = [root]

    rg_bin = shutil.which("rg")
    if rg_bin:
        cmd = [
            rg_bin,
            "--line-number",
            "--no-heading",
            "--color=never",
            "--max-count",
            "10",
            "--max-filesize",
            "512K",
            pattern,
            *[str(t) for t in targets],
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if proc.returncode in (0, 1):
                lines = [ln for ln in proc.stdout.splitlines() if ln.strip()][:cap]
                if not lines:
                    return f"No matches for /{pattern}/."
                # Relativize paths for compact output
                root_prefix = str(root) + os.sep
                rel_lines = [
                    ln[len(root_prefix):].replace("\\", "/") if ln.startswith(root_prefix) else ln.replace("\\", "/")
                    for ln in lines
                ]
                return _truncate_output("\n".join(rel_lines))
        except Exception:
            pass

    # Pure-Python regex fallback
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Invalid regex '{pattern}': {exc}"

    matches: list[str] = []
    for target in targets:
        files_iter: list[Path] = []
        if target.is_file():
            files_iter = [target]
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for fn in sorted(filenames):
                    files_iter.append(Path(dirpath) / fn)
        for fpath in files_iter:
            if len(matches) >= cap:
                break
            try:
                if fpath.stat().st_size > 512_000:
                    continue
                raw = fpath.read_bytes()
                if b"\x00" in raw[:1024]:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            rel = fpath.relative_to(root).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}:{line[:200]}")
                    if len(matches) >= cap:
                        break
    if not matches:
        return f"No matches for /{pattern}/."
    return _truncate_output("\n".join(matches))


def read_slice(
    workspace_root: Path,
    path: str,
    *,
    start_line: int = 1,
    end_line: int = 120,
) -> str:
    """Read a bounded 1-based line range from a file inside `workspace_root` (read-only)."""
    try:
        target = _resolve_within_workspace(workspace_root, path)
    except ValueError as exc:
        return str(exc)
    if not target.is_file():
        return f"File not found: {path}"
    start = max(1, int(start_line))
    end = max(start, min(int(end_line), start + MAX_SLICE_LINES - 1))
    try:
        raw = target.read_bytes()
        if b"\x00" in raw[:1024]:
            return f"Binary file skipped: {path}"
        lines = raw.decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Cannot read {path}: {exc}"

    total = len(lines)
    if start > total:
        return f"{path}: requested start_line={start} > total_lines={total}."
    actual_end = min(end, total)
    rel = target.relative_to(workspace_root.resolve()).as_posix()
    out = [f"=== {rel} (lines {start}-{actual_end} of {total}) ==="]
    for idx in range(start, actual_end + 1):
        out.append(f"L{idx}: {lines[idx - 1]}")
    return _truncate_output("\n".join(out))


def list_files(
    workspace_root: Path,
    subdir: str = ".",
    *,
    max_entries: int = 100,
) -> str:
    """List files inside `workspace_root/subdir` (read-only, skips ignored dirs)."""
    try:
        target = _resolve_within_workspace(workspace_root, subdir)
    except ValueError as exc:
        return str(exc)
    if not target.exists():
        return f"Directory not found: {subdir}"
    cap = max(1, min(int(max_entries), 300))
    root = workspace_root.resolve()

    if target == root:
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                items = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                if items:
                    suffix = f"\n... ({len(items) - cap} more)" if len(items) > cap else ""
                    return _truncate_output("\n".join(items[:cap]) + suffix)
        except Exception:
            pass

    results: list[str] = []
    if target.is_file():
        return target.relative_to(root).as_posix()
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            results.append(rel)
            if len(results) >= cap:
                return _truncate_output("\n".join(results) + "\n... [capped]")
    if not results:
        return f"No files in {subdir}."
    return _truncate_output("\n".join(results))


def git_status(workspace_root: Path) -> str:
    """Return `git status --short --branch` in `workspace_root` (read-only)."""
    root = workspace_root.resolve()
    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return _truncate_output(f"git status failed (rc={proc.returncode}): {proc.stderr.strip()}")
        return _truncate_output(proc.stdout.strip() or "(clean working tree)")
    except Exception as exc:
        return f"git status unavailable: {exc}"


# OpenAPI / Gemini FunctionDeclaration schemas for Level 2 read-only tools
READONLY_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "rg_search",
        "description": "Search for a regex pattern in the repository (read-only). Returns file:line:text matches.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "pattern": {"type": "STRING", "description": "Regex or literal pattern to search for."},
                "max_matches": {"type": "INTEGER", "description": "Maximum matches to return (default 40)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_slice",
        "description": "Read a bounded 1-based line range of a repository file (max 200 lines, read-only).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Relative file path inside the workspace."},
                "start_line": {"type": "INTEGER", "description": "1-based start line (inclusive)."},
                "end_line": {"type": "INTEGER", "description": "1-based end line (inclusive)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List repository files in a subdirectory (read-only).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subdir": {"type": "STRING", "description": "Relative directory path (default '.')."},
                "max_entries": {"type": "INTEGER", "description": "Maximum file paths to return."},
            },
        },
    },
    {
        "name": "git_status",
        "description": "Show `git status --short --branch` for the repository (read-only).",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
    },
]


def build_readonly_tool_dispatcher(
    workspace_root: Path,
    *,
    scope_paths: list[str] | None = None,
) -> dict[str, Callable[[dict[str, Any]], str]]:
    """Return a name -> read-only handler map bound to `workspace_root`."""
    root = workspace_root.resolve()
    return {
        "rg_search": lambda args: rg_search(
            root,
            str(args.get("pattern", "")),
            scope_paths=scope_paths,
            max_matches=int(args.get("max_matches", 40) or 40),
        ),
        "read_slice": lambda args: read_slice(
            root,
            str(args.get("path", "")),
            start_line=int(args.get("start_line", 1) or 1),
            end_line=int(args.get("end_line", 120) or 120),
        ),
        "list_files": lambda args: list_files(
            root,
            str(args.get("subdir", ".") or "."),
            max_entries=int(args.get("max_entries", 100) or 100),
        ),
        "git_status": lambda _args: git_status(root),
    }
