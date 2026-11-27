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
from ogy_core.ingest import _pick_float, _pick_int, ingest_statusline_payload
from ogy_core.storage import INIT_PRAGMAS, connect_readonly, ensure_schema


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


def _read_db_fallback(db_path: Path, session_id: str) -> tuple[int, int, int, int, float, int]:
    """Return (in_tok, cr_tok, cc_tok, ctx_used, total_usd, turn_count) from ogy.db."""
    if not db_path.is_file():
        return 0, 0, 0, 0, 0.0, 0
    try:
        conn = connect_readonly(db_path, timeout=0.25)
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
                           turns_cost_usd, total_cost_usd, turn_count, peak_context_tokens, session_id
                    FROM v_session_kpis WHERE status = 'active' ORDER BY started_at DESC LIMIT 1
                    """
                ).fetchone()
                trow = None
                if row and row[7]:
                    trow = conn.execute(
                        """
                        SELECT input_tokens, cache_read_tokens, cache_creation_tokens, context_window_used
                        FROM turns WHERE session_id = ? ORDER BY step_index DESC LIMIT 1
                        """,
                        (row[7],),
                    ).fetchone()
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


def _bar(fraction: float, width: int = 6) -> str:
    """Render a compact Unicode progress bar (e.g. '███░░░')."""
    clamped = max(0.0, min(1.0, fraction))
    filled = round(clamped * width)
    return ("█" * filled) + ("░" * (width - filled))


def _c(text: str, code: str, *, no_color: bool) -> str:
    if no_color or not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _pick_quota_buckets(quota: Any, model_name: str) -> tuple[float | None, float | None]:
    """Return (5h_remaining_fraction, weekly_remaining_fraction) matching active model."""
    if not isinstance(quota, dict):
        return None, None
    is_3p = any(k in model_name.lower() for k in ("claude", "gpt", "opus", "sonnet", "haiku"))
    order_5h = ("3p-5h", "claude-5h", "gemini-5h") if is_3p else ("gemini-5h", "3p-5h", "claude-5h")
    order_wk = ("3p-weekly", "claude-weekly", "gemini-weekly") if is_3p else ("gemini-weekly", "3p-weekly", "claude-weekly")

    q5h = next(
        (float(quota[b]["remaining_fraction"]) for b in order_5h if isinstance(quota.get(b), dict) and quota[b].get("remaining_fraction") is not None),
        None,
    )
    qwk = next(
        (float(quota[b]["remaining_fraction"]) for b in order_wk if isinstance(quota.get(b), dict) and quota[b].get("remaining_fraction") is not None),
        None,
    )
    return q5h, qwk


def _quota_color(frac: float) -> str:
    if frac >= 0.50:
        return "32"  # green
    if frac >= 0.20:
        return "33"  # yellow
    return "31"      # red


def render_statusline(
    payload: dict[str, Any],
    *,
    terminal_width: int | None = None,
    persist: bool = True,
    cfg: Any = None,
) -> str:
    """Render a compact 1-line ANSI HUD string from statusLine JSON payload."""
    resolved_cfg = cfg or load_config()
    if resolved_cfg.isolate_hooks and os.environ.get("OGY_ACTIVE") != "1":
        return ""
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

    if persist and denom == 0 and total_usd == 0.0 and payload.get("agent_state") != "authenticating":
        sample_path = resolved_cfg.stash_dir.parent / "statusline_sample.json"
        if not sample_path.is_file():
            try:
                sample_path.parent.mkdir(parents=True, exist_ok=True)
                sample_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError:
                pass
        fb_in, fb_cr, fb_cc, fb_ctx, fb_cost, fb_turns = _read_db_fallback(resolved_cfg.db_path, session_id)
        if fb_in + fb_cr + fb_cc > 0 or fb_cost > 0.0:
            in_tok, cr_tok, cc_tok = fb_in, fb_cr, fb_cc
            denom = in_tok + cr_tok + cc_tok
            ctx_used = fb_ctx or denom
            total_usd = fb_cost
            turn_count = turn_count or fb_turns

    cache_ratio = (cr_tok / denom) if denom > 0 else None
    cache_pct_str = f"{round(cache_ratio * 100)}%" if cache_ratio is not None else "--"
    ctx_frac = (ctx_used / ctx_max) if ctx_max > 0 else 0.0
    ctx_pct = round(ctx_frac * 100)

    health = assess_context_health(
        context_tokens=ctx_used,
        turn_count=turn_count,
        cache_hit_ratio=cache_ratio,
        idle_gap_s=idle_gap_s,
        exceeds_200k=exceeds_200k,
        model_id=model_name,
    )

    q5h, qwk = _pick_quota_buckets(payload.get("quota"), model_name)

    if persist:
        if session_id:
            state_file = resolved_cfg.state_dir / f"sl_{session_id}.ts"
            if _should_persist_snapshot(state_file, resolved_cfg.statusline_debounce_s):
                try:
                    resolved_cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(resolved_cfg.db_path), timeout=0.45, isolation_level=None)
                    try:
                        for p in INIT_PRAGMAS:
                            conn.execute(p)
                        ensure_schema(conn)
                        ingest_statusline_payload(conn, payload, enterprise_cache=resolved_cfg.enterprise_cache_pricing)
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass

    width = terminal_width or shutil.get_terminal_size((120, 24)).columns
    no_color = bool(os.environ.get("NO_COLOR"))
    sep = _c(" │ ", "90", no_color=no_color)

    zone_label = {"green": "● OK", "amber": "▲ WARN", "red": "■ RED"}.get(health.zone, "● OK")
    if health.idle_warning and health.zone == "red":
        zone_label = f"■ {health.idle_warning}"
    zone_code = {"green": "1;32", "amber": "1;33", "red": "1;31"}.get(health.zone, "32")
    zone_disp = _c(zone_label, zone_code, no_color=no_color)

    brand = _c("ogy", "1;36", no_color=no_color)
    cache_code = "90" if cache_ratio is None else ("32" if cache_ratio >= 0.70 else ("33" if cache_ratio >= 0.40 else "31"))
    ctx_code = "32" if ctx_frac < 0.30 else ("33" if ctx_frac < 0.50 else "31")

    if width < 115:
        parts = [
            brand,
            _c(f"c:{cache_pct_str}", cache_code, no_color=no_color),
            _c(f"{_bar(ctx_frac, 4)} {_format_tokens(ctx_used)}", ctx_code, no_color=no_color),
            _c(f"${total_usd:.2f}", "37", no_color=no_color),
        ]
        if q5h is not None:
            parts.append(_c(f"5h:{round(q5h * 100)}%", _quota_color(q5h), no_color=no_color))
        if qwk is not None:
            parts.append(_c(f"wk:{round(qwk * 100)}%", _quota_color(qwk), no_color=no_color))
        parts.append(zone_disp)
        return sep.join(parts)

    turn_tag = f"t{turn_count} · " if turn_count > 0 else ""
    parts = [
        brand,
        f"cache {_c(cache_pct_str, cache_code, no_color=no_color)}",
        f"ctx {_c(_bar(ctx_frac, 6), ctx_code, no_color=no_color)} {_format_tokens(ctx_used)} ({ctx_pct}%)",
        _c(f"{turn_tag}${total_usd:.2f}", "37", no_color=no_color),
    ]
    if q5h is not None:
        parts.append(f"5h {_c(_bar(q5h, 5), _quota_color(q5h), no_color=no_color)} {round(q5h * 100)}%")
    if qwk is not None:
        parts.append(f"wk {_c(_bar(qwk, 5), _quota_color(qwk), no_color=no_color)} {round(qwk * 100)}%")
    parts.append(zone_disp)
    return sep.join(parts)


def run_statusline_from_stdin() -> int:
    """Fail-open stdin entrypoint for `ogy-hook statusline`."""
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        line = render_statusline(payload)
        if line:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BaseException:
        try:
            sys.stdout.write("ogy | OK\n")
            sys.stdout.flush()
        except BaseException:
            pass
    return 0
