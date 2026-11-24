"""Fail-open 1-line ANSI statusline renderer (100% stdlib, < 40 ms)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from ogy_core.config import load_config
from ogy_core.context_health import assess_context_health
from ogy_core.ingest import ingest_statusline_payload
from ogy_core.storage import INIT_PRAGMAS, ensure_schema


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return str(n)


def _should_persist_snapshot(state_file: Path, debounce_s: float) -> bool:
    now = time.time()
    try:
        if state_file.is_file():
            last = float(state_file.read_text(encoding="utf-8").strip() or "0")
            if (now - last) < debounce_s:
                return False
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(f"{now:.3f}", encoding="utf-8")
        return True
    except OSError:
        return False


def render_statusline(payload: dict[str, Any], *, terminal_width: int | None = None, persist: bool = True) -> str:
    """Render a compact 1-line HUD string from statusLine JSON payload."""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    in_tok = int(usage.get("input_tokens") or 0)
    cr_tok = int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
    cc_tok = int(usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0)

    denom = in_tok + cr_tok + cc_tok
    cache_ratio = (cr_tok / denom) if denom > 0 else None
    cache_pct_str = f"{round(cache_ratio * 100)}%" if cache_ratio is not None else "--"

    ctx_win = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    ctx_used = int(ctx_win.get("used") or payload.get("context_window_used") or denom)
    ctx_max = int(ctx_win.get("max") or payload.get("context_window_max") or 200_000)
    ctx_pct = round((ctx_used / ctx_max) * 100) if ctx_max > 0 else 0

    cost_obj = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    total_usd = float(cost_obj.get("total_usd") or 0.0)

    idle_gap_s = float(payload.get("idle_gap_s") or 0.0)
    turn_count = int(payload.get("turn_count") or payload.get("stepIdx") or 0)
    exceeds_200k = bool(payload.get("exceeds_200k_tokens"))
    model_name = str(payload.get("modelName") or payload.get("model") or "gemini-4-hydrogen")

    health = assess_context_health(
        context_tokens=ctx_used,
        turn_count=turn_count,
        cache_hit_ratio=cache_ratio,
        idle_gap_s=idle_gap_s,
        exceeds_200k=exceeds_200k,
        model_id=model_name,
    )

    quota_str = ""
    quota = payload.get("quota")
    if isinstance(quota, dict):
        for bucket in ("gemini-5h", "claude-5h"):
            q = quota.get(bucket)
            if isinstance(q, dict) and q.get("remaining_fraction") is not None:
                quota_str = f"5h:{round(float(q['remaining_fraction']) * 100)}%"
                break

    if persist:
        session_id = str(payload.get("conversationId") or payload.get("session_id") or "").strip()
        if session_id:
            cfg = load_config()
            state_file = cfg.state_dir / f"sl_{session_id}.ts"
            if _should_persist_snapshot(state_file, cfg.statusline_debounce_s):
                try:
                    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(cfg.db_path), timeout=0.45, isolation_level=None)
                    try:
                        for p in INIT_PRAGMAS:
                            conn.execute(p)
                        ensure_schema(conn)
                        ingest_statusline_payload(conn, payload, enterprise_cache=cfg.enterprise_cache_pricing)
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass

    width = terminal_width or shutil.get_terminal_size((120, 24)).columns
    no_color = bool(os.environ.get("NO_COLOR"))

    zone_label = {"green": "OK", "amber": "WARN", "red": "RED"}.get(health.zone, "OK")
    if health.idle_warning and health.zone == "red":
        zone_label = health.idle_warning

    if not no_color:
        color_code = {"green": "\x1b[32m", "amber": "\x1b[33m", "red": "\x1b[31m"}.get(health.zone, "")
        reset = "\x1b[0m"
        zone_disp = f"{color_code}{zone_label}{reset}"
    else:
        zone_disp = zone_label

    if width < 120:
        parts = [f"ogy", f"c:{cache_pct_str}", f"{_format_tokens(ctx_used)}", f"${total_usd:.2f}"]
        if quota_str:
            parts.append(quota_str)
        parts.append(zone_disp)
        return " | ".join(parts)

    parts = [
        "ogy",
        f"cache {cache_pct_str}",
        f"ctx {_format_tokens(ctx_used)} ({ctx_pct}%)",
        f"${total_usd:.2f}",
    ]
    if quota_str:
        parts.append(quota_str)
    parts.append(zone_disp)
    return " | ".join(parts)


def run_statusline_from_stdin() -> int:
    """Fail-open stdin entrypoint for `ogy-hook statusline`."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        line = render_statusline(payload)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BaseException:
        try:
            sys.stdout.write("ogy | OK\n")
            sys.stdout.flush()
        except BaseException:
            pass
    return 0
