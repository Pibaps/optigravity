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


def _pick_int(d: dict[str, Any], *keys: str) -> int:
    for k in keys:
        val = d.get(k)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return 0


def _pick_float(d: dict[str, Any], *keys: str) -> float:
    for k in keys:
        val = d.get(k)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def _read_db_fallback(db_path: Path, session_id: str) -> tuple[int, int, int, int, float, int]:
    """Return (in_tok, cr_tok, cc_tok, ctx_used, total_usd, turn_count) from ogy.db."""
    if not db_path.is_file():
        return 0, 0, 0, 0, 0.0, 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
        try:
            if session_id:
                row = conn.execute(
                    """
                    SELECT total_input_tokens, total_cache_read_tokens, total_cache_creation_tokens,
                           turns_cost_usd, total_cost_usd, turn_count, peak_context_tokens
                    FROM v_session_kpis WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                trow = conn.execute(
                    """
                    SELECT input_tokens, cache_read_tokens, cache_creation_tokens, context_window_used
                    FROM turns WHERE session_id = ? ORDER BY step_index DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT total_input_tokens, total_cache_read_tokens, total_cache_creation_tokens,
                           turns_cost_usd, total_cost_usd, turn_count, peak_context_tokens
                    FROM v_session_kpis ORDER BY started_at DESC LIMIT 1
                    """
                ).fetchone()
                trow = None
            if not row:
                return 0, 0, 0, 0, 0.0, 0
            in_tok = int(row[0] or 0)
            cr_tok = int(row[1] or 0)
            cc_tok = int(row[2] or 0)
            cost_usd = float(row[4] or 0.0) or float(row[3] or 0.0)
            turns = int(row[5] or 0)
            ctx = int(row[6] or 0)
            if trow:
                last_ctx = int(trow[3] or 0) or (int(trow[0] or 0) + int(trow[1] or 0) + int(trow[2] or 0))
                if last_ctx > 0:
                    ctx = last_ctx
            elif ctx == 0:
                ctx = in_tok + cr_tok + cc_tok
            return in_tok, cr_tok, cc_tok, ctx, cost_usd, turns
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0, 0, 0, 0.0, 0


def render_statusline(payload: dict[str, Any], *, terminal_width: int | None = None, persist: bool = True) -> str:
    """Render a compact 1-line HUD string from statusLine JSON payload."""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    in_tok = _pick_int(usage, "input_tokens", "inputTokens")
    cr_tok = _pick_int(usage, "cache_read_input_tokens", "cache_read_tokens", "cacheReadInputTokens", "cacheReadTokens")
    cc_tok = _pick_int(usage, "cache_creation_input_tokens", "cache_creation_tokens", "cacheCreationInputTokens", "cacheCreationTokens")

    denom = in_tok + cr_tok + cc_tok

    ctx_win = (
        payload.get("context_window")
        if isinstance(payload.get("context_window"), dict)
        else (payload.get("contextWindow") if isinstance(payload.get("contextWindow"), dict) else {})
    )
    ctx_used = _pick_int(ctx_win, "used", "usedTokens", "used_tokens") or _pick_int(payload, "context_window_used", "contextWindowUsed") or denom
    ctx_max = _pick_int(ctx_win, "max", "maxTokens", "max_tokens") or _pick_int(payload, "context_window_max", "contextWindowMax") or 200_000

    cost_obj = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    total_usd = _pick_float(cost_obj, "total_usd", "totalUsd", "total_cost_usd", "totalCostUsd") or _pick_float(payload, "total_cost_usd", "totalCostUsd", "total_usd")

    idle_gap_s = _pick_float(payload, "idle_gap_s", "idleGapS")
    turn_count = _pick_int(payload, "turn_count", "turnCount", "stepIdx")
    exceeds_200k = bool(payload.get("exceeds_200k_tokens") or payload.get("exceeds200kTokens"))
    model_name = str(payload.get("modelName") or payload.get("model") or "gemini-4-hydrogen")

    session_id = str(payload.get("conversationId") or payload.get("session_id") or payload.get("conversation_id") or "").strip()

    if persist and denom == 0 and total_usd == 0.0:
        cfg = load_config()
        sample_path = cfg.stash_dir.parent / "statusline_sample.json"
        if not sample_path.is_file():
            try:
                sample_path.parent.mkdir(parents=True, exist_ok=True)
                sample_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError:
                pass
        fb_in, fb_cr, fb_cc, fb_ctx, fb_cost, fb_turns = _read_db_fallback(cfg.db_path, session_id)
        if fb_in + fb_cr + fb_cc > 0 or fb_cost > 0.0:
            in_tok, cr_tok, cc_tok = fb_in, fb_cr, fb_cc
            denom = in_tok + cr_tok + cc_tok
            ctx_used = fb_ctx or denom
            total_usd = fb_cost
            turn_count = turn_count or fb_turns

    cache_ratio = (cr_tok / denom) if denom > 0 else None
    cache_pct_str = f"{round(cache_ratio * 100)}%" if cache_ratio is not None else "--"
    ctx_pct = round((ctx_used / ctx_max) * 100) if ctx_max > 0 else 0

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
