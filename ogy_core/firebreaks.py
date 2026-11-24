"""Deterministic firebreaks, shell compression (`ogy wrap`), and stash recall (100% stdlib)."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
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

VERBOSE_CMD_RE = re.compile(
    r"^\s*(pytest|py\.test|python\s+-m\s+pytest|uv\s+run\s+pytest|cargo\s+test|npm\s+test|pnpm\s+test|vitest|jest|go\s+test|git\s+log|git\s+diff|git\s+status)\b",
    re.IGNORECASE,
)

EXCLUDED_MUTATING_RE = re.compile(
    r"\b(git\s+(commit|push|rebase|merge|cherry-pick|reset|tag)|rm\b|del\b|remove-item|rmdir)\b",
    re.IGNORECASE,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def evaluate_pre_tool_firebreaks(
    conn: sqlite3.Connection | None,
    *,
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    cfg: OgyConfig,
) -> dict[str, Any]:
    """Evaluate PreToolUse firebreaks. Never returns {} (nominal is {'decision': ''})."""
    name = (tool_name or "").strip()
    args_hash = canonical_args_hash(tool_args)

    # Firebreak 1: Anti-loop (same args_hash failed >= threshold times in session)
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
                "reason": "[ogy] Boucle d'erreur detectee sur cet appel, change d'approche.",
            }

    # Firebreak 2: Obese file read (lockfiles or > 16KB without StartLine/EndLine)
    if name in ("view_file", "read_file"):
        target = str(tool_args.get("Path") or tool_args.get("AbsolutePath") or tool_args.get("path") or "")
        start_line = tool_args.get("StartLine") or tool_args.get("start_line")
        end_line = tool_args.get("EndLine") or tool_args.get("end_line")
        if target:
            p = Path(target)
            if p.name.lower() in LOCKFILE_NAMES:
                return {
                    "decision": "deny",
                    "reason": f"[ogy] Lecture de lockfile bloquee ({p.name}). Utilise grep_search.",
                }
            if start_line is None and end_line is None and p.is_file():
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                if size > cfg.firebreak_file_max_bytes:
                    return {
                        "decision": "deny",
                        "reason": (
                            f"[ogy] Fichier volumineux ({size} octets > {cfg.firebreak_file_max_bytes}). "
                            "Utilise grep_search ou specifie StartLine/EndLine."
                        ),
                    }

    # Firebreak 3: Shell compression (ogy wrap)
    if name in ("run_command", "bash", "shell", "execute_command"):
        cmd = str(tool_args.get("CommandLine") or tool_args.get("command") or "").strip()
        if cmd and not cmd.startswith("ogy wrap") and not EXCLUDED_MUTATING_RE.search(cmd):
            if VERBOSE_CMD_RE.search(cmd):
                wrapped = f"ogy wrap -- {cmd}"
                if cfg.overwrite_verified_ok:
                    return {
                        "decision": "",
                        "overwrite": {"CommandLine": wrapped},
                    }
                return {
                    "decision": "deny",
                    "reason": f"[ogy] Commande verbeuse detectee. Relance avec: {wrapped}",
                }

    return {"decision": ""}


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
        # Keep tail context on failure
        kept = lines[-30:]
    elif not kept:
        kept = lines[:5] + [f"... [{len(lines) - 10} lines omitted] ..."] + lines[-5:]

    compressed = "\n".join(kept)
    # Net-win gate: if no meaningful reduction, return raw
    if len(compressed) >= int(len(cleaned) * 0.90):
        return cleaned
    return compressed


def stash_raw_output(raw_text: str, *, stash_dir: Path | None = None) -> str:
    """Save raw output to stash_dir/<sha256>.log and return the sha256 hex digest."""
    target_dir = stash_dir or load_config().stash_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
    out_file = target_dir / f"{digest}.log"
    if not out_file.exists():
        out_file.write_text(raw_text, encoding="utf-8", errors="replace")
    return digest


def recall_stashed_output(
    hash_prefix: str,
    *,
    lines_spec: str | None = None,
    stash_dir: Path | None = None,
) -> str:
    """Read stashed raw output by hash prefix and optional 1-based inclusive A:B line range."""
    target_dir = stash_dir or load_config().stash_dir
    prefix = (hash_prefix or "").strip().lower()
    if not prefix or not target_dir.is_dir():
        return f"[ogy recall] Hash not found: {hash_prefix}"

    matches = sorted(target_dir.glob(f"{prefix}*.log"))
    if not matches:
        return f"[ogy recall] No stashed output matching '{hash_prefix}'"

    text = matches[0].read_text(encoding="utf-8", errors="replace")
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


def run_wrapped_command(cmd: Sequence[str], *, stash_dir: Path | None = None) -> int:
    """Execute command, apply deterministic compression + net-win gate + tee-on-failure, preserve exit code."""
    if not cmd:
        print("[ogy wrap] No command provided.", file=sys.stderr)
        return 2

    cmd_str = " ".join(cmd)
    if EXCLUDED_MUTATING_RE.search(cmd_str):
        # Never intercept/alter mutating or cryptographic git/rm commands
        proc = subprocess.run(list(cmd))
        return int(proc.returncode)

    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    raw = proc.stdout or ""
    if proc.stderr:
        raw = f"{raw}\n{proc.stderr}" if raw else proc.stderr

    compressed = compress_shell_output(raw, exit_code=proc.returncode)
    if compressed != raw or proc.returncode != 0:
        digest = stash_raw_output(raw, stash_dir=stash_dir)
        sys.stdout.write(compressed)
        if compressed and not compressed.endswith("\n"):
            sys.stdout.write("\n")
        if len(compressed) < len(raw) or proc.returncode != 0:
            sys.stdout.write(
                f"[ogy wrap] exit={proc.returncode} raw={len(raw)}B out={len(compressed)}B recall={digest[:12]}\n"
            )
    else:
        sys.stdout.write(raw)
    sys.stdout.flush()
    return int(proc.returncode)
