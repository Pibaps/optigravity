"""Readable ROI tables for `ogy stats` and actionable `ogy waste` (100% stdlib)."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from ogy_core.storage import query_all

# Actionable guidance + impact priority for W1..W8
WASTE_PLAYBOOK: dict[int, dict[str, Any]] = {
    1: {
        "severity": 90,
        "label": "Low Prompt Cache Hit",
        "advice": "Avoid mid-session model switches and long idle gaps (>3m); keep system prompt stable.",
        "command": "ogy config show",
    },
    2: {
        "severity": 95,
        "label": "Tool Retry Loop",
        "advice": "Enable anti-loop firebreaks so repeated failing calls stop at attempt 3.",
        "command": "ogy init --enable-firebreaks",
    },
    3: {
        "severity": 85,
        "label": "Obese Tool Output",
        "advice": "Wrap verbose test/git commands so raw logs are stashed and compressed.",
        "command": "ogy wrap -- pytest",
    },
    4: {
        "severity": 75,
        "label": "Reasoning Over-Effort",
        "advice": "Route routine/trivial tasks with `ogy run` so effort stays low/medium.",
        "command": "ogy run --effort low \"<task>\"",
    },
    5: {
        "severity": 88,
        "label": "Context Rot Risk",
        "advice": "Rotate long sessions (>120K tokens or >=25 turns) with a clean handoff.",
        "command": "ogy tui  # then press [R] or run /rotate",
    },
    6: {
        "severity": 65,
        "label": "Duplicate File Reads",
        "advice": "Use bounded StartLine/EndLine slices instead of re-reading full files.",
        "command": "ogy init --enable-firebreaks",
    },
    7: {
        "severity": 80,
        "label": "MCP Schema Bloat",
        "advice": "Set eager=false on heavy MCP servers and disable UI-only tools in CLI.",
        "command": "ogy audit mcp",
    },
    8: {
        "severity": 70,
        "label": "Subagent Drift",
        "advice": "Cap subagent cost and prefer read-only Flash-Lite delegation.",
        "command": "ogy config set subagents.privacy_mode tier1_only",
    },
}


def parse_since_iso(since: str | None) -> str | None:
    """Convert `--since` ('7d', '24h', '2026-11-01', ISO-8601) into UTC ISO string."""
    if not since or not since.strip():
        return None
    s = since.strip()
    m = re.match(r"^(\d+)([dhw])$", s, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(days=amount) if unit == "d" else (timedelta(hours=amount) if unit == "h" else timedelta(weeks=amount))
        dt = datetime.now(timezone.utc) - delta
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # Accept YYYY-MM-DD or full ISO timestamp
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return f"{s}T00:00:00.000Z"
    return s


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _c(text: str, code: str) -> str:
    if not _use_color() or not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _render_table(headers: list[str], rows: list[list[str]], *, right_align: set[int] | None = None) -> str:
    right_align = right_align or set()
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if i in right_align else cell.ljust(widths[i]))
        return "│ " + " │ ".join(parts) + " │"

    top = "┌─" + "─┬─".join("─" * w for w in widths) + "─┐"
    mid = "├─" + "─┼─".join("─" * w for w in widths) + "─┤"
    bot = "└─" + "─┴─".join("─" * w for w in widths) + "─┘"

    out = [top, fmt_row(headers), mid]
    for r in rows:
        out.append(fmt_row(r))
    out.append(bot)
    return "\n".join(out)


def query_filtered_stats(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    last: int = 20,
    since: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """Query `v_session_kpis` with optional `--session`, `--last`, `--since`, `--repo` filters."""
    where: list[str] = []
    params: list[Any] = []
    if session_id:
        where.append("sk.session_id = ?")
        params.append(session_id)
    since_iso = parse_since_iso(since)
    if since_iso:
        where.append("sk.started_at >= ?")
        params.append(since_iso)
    if repo:
        where.append("LOWER(s.workspace_path) LIKE ?")
        params.append(f"%{repo.lower()}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, int(last))
    params.append(limit)
    rows = query_all(
        conn,
        f"""
        SELECT sk.*, s.workspace_path
        FROM v_session_kpis sk
        JOIN sessions s ON s.session_id = sk.session_id
        {where_sql}
        ORDER BY sk.started_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [dict(r) for r in rows]


def _query_roi_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    iso_7d = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_30d = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def window_stats(since_iso: str) -> dict[str, Any]:
        rows = query_all(
            conn,
            """
            SELECT
              COUNT(DISTINCT session_id) AS sessions,
              COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
              COALESCE(SUM(input_tokens + cache_read_tokens + cache_creation_tokens), 0) AS total_in,
              COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
            FROM turns
            WHERE created_at >= ?
            """,
            (since_iso,),
        )
        r = rows[0] if rows else None
        h_rows = query_all(
            conn,
            "SELECT COALESCE(SUM(tokens_saved_est), 0) AS saved FROM hook_events WHERE created_at >= ?",
            (since_iso,),
        )
        saved_hooks = int(h_rows[0]["saved"] or 0) if h_rows else 0
        return {
            "sessions": int(r["sessions"] or 0) if r else 0,
            "cache_read": int(r["cache_read"] or 0) if r else 0,
            "total_in": int(r["total_in"] or 0) if r else 0,
            "cost_usd": float(r["cost_usd"] or 0.0) if r else 0.0,
            "hook_tokens_saved": saved_hooks,
        }

    sub_rows = query_all(
        conn,
        """
        SELECT
          COUNT(*) AS runs,
          COALESCE(SUM(input_tokens + output_tokens + thought_tokens), 0) AS tokens,
          COALESCE(SUM(thought_tokens), 0) AS thought_tokens,
          COALESCE(SUM(CASE WHEN tier = 'free' THEN 1 ELSE 0 END), 0) AS free_runs,
          COALESCE(SUM(CASE WHEN tier != 'free' THEN 1 ELSE 0 END), 0) AS tier1_runs,
          COALESCE(SUM(cost_usd), 0.0) AS cost_usd
        FROM subagent_runs
        """,
    )
    sr = sub_rows[0] if sub_rows else None
    return {
        "d7": window_stats(iso_7d),
        "d30": window_stats(iso_30d),
        "subagents": {
            "runs": int(sr["runs"] or 0) if sr else 0,
            "tokens": int(sr["tokens"] or 0) if sr else 0,
            "thought_tokens": int(sr["thought_tokens"] or 0) if sr else 0,
            "free_runs": int(sr["free_runs"] or 0) if sr else 0,
            "tier1_runs": int(sr["tier1_runs"] or 0) if sr else 0,
            "cost_usd": float(sr["cost_usd"] or 0.0) if sr else 0.0,
        },
    }


def render_stats_report(conn: sqlite3.Connection, sessions_data: list[dict[str, Any]]) -> str:
    """Render ROI header + separated OAuth sessions and MCP Subagents tables."""
    if not sessions_data:
        return "No sessions recorded yet."

    roi = _query_roi_summary(conn)
    d7 = roi["d7"]
    d30 = roi["d30"]
    sub = roi["subagents"]

    total_cache_avoided = sum(int(r.get("total_cache_read_tokens") or 0) for r in sessions_data)
    # Conservative 90% cache-read discount estimate using per-model pricing
    est_cache_savings_usd = 0.0
    try:
        from ogy_core.pricing import get_model_pricing

        for r in sessions_data:
            cr = int(r.get("total_cache_read_tokens") or 0)
            if cr > 0:
                p = get_model_pricing(str(r.get("initial_model") or "gemini-4-hydrogen"))
                est_cache_savings_usd += (cr / 1_000_000.0) * max(0.0, p.input_per_m - p.cache_read_per_m)
    except Exception:
        est_cache_savings_usd = (total_cache_avoided / 1_000_000.0) * 0.90

    hit7 = (d7["cache_read"] / d7["total_in"] * 100.0) if d7["total_in"] > 0 else 0.0
    hit30 = (d30["cache_read"] / d30["total_in"] * 100.0) if d30["total_in"] > 0 else 0.0

    lines: list[str] = []
    lines.append(_c("══ Optigravity ROI Summary ══", "1;36"))
    lines.append(
        f"  Cache tokens avoided (shown): {_c(_fmt_tokens(total_cache_avoided), '1;32')} "
        f"(est. saved {_c(f'${est_cache_savings_usd:.4f}', '1;32')})  |  "
        f"Hook/wrap saved (7d): {_fmt_tokens(d7['hook_tokens_saved'])}"
    )
    lines.append(
        f"  Trend 7d : {d7['sessions']} sessions · cache {hit7:.1f}% · est ${d7['cost_usd']:.4f}"
    )
    lines.append(
        f"  Trend 30d: {d30['sessions']} sessions · cache {hit30:.1f}% · est ${d30['cost_usd']:.4f}"
    )
    lines.append("")

    # Block 1: Sessions agy (OAuth)
    lines.append(_c("Sessions agy (OAuth)", "1;37"))
    headers = ["Session", "Model", "Turns", "Cache%", "Peak Ctx", "Est USD", "Status"]
    table_rows: list[list[str]] = []
    for r in sessions_data:
        hit_pct = f"{round((r.get('cache_hit_ratio') or 0.0) * 100.0, 1)}%"
        table_rows.append([
            str(r["session_id"])[:16],
            str(r.get("initial_model") or "-")[:20],
            str(r.get("turn_count") or 0),
            hit_pct,
            _fmt_tokens(int(r.get("peak_context_tokens") or 0)),
            f"${float(r.get('turns_cost_usd') or 0.0):.4f}",
            str(r.get("status") or "-"),
        ])
    lines.append(_render_table(headers, table_rows, right_align={2, 3, 4, 5}))
    lines.append("")

    # Block 2: Subagents MCP (API USD — strictly separate from OAuth quota)
    lines.append(_c("Subagents MCP (API USD)", "1;37"))
    if sub["runs"] == 0:
        lines.append("  No MCP subagent runs recorded yet (OAuth quota and Gemini API spend are tracked separately).")
    else:
        sub_headers = ["Runs", "Free Tier", "Tier 1", "Thought Tok", "Total Tokens", "API Cost (USD)"]
        sub_rows = [[
            str(sub["runs"]),
            str(sub["free_runs"]),
            str(sub["tier1_runs"]),
            _fmt_tokens(sub["thought_tokens"]),
            _fmt_tokens(sub["tokens"]),
            f"${sub['cost_usd']:.4f}",
        ]]
        lines.append(_render_table(sub_headers, sub_rows, right_align={0, 1, 2, 3, 4, 5}))

    return "\n".join(lines)


def render_waste_report(findings: list[dict[str, Any]]) -> str:
    """Render W1..W8 findings sorted by impact with actionable remediation commands."""
    if not findings:
        return "No waste detected."

    enriched: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for f in findings:
        wid = int(f.get("waste_id") or 0)
        pb = WASTE_PLAYBOOK.get(
            wid,
            {"severity": 50, "label": f.get("kind", "waste"), "advice": "Review session telemetry.", "command": "ogy stats"},
        )
        enriched.append((int(pb["severity"]), f, pb))

    enriched.sort(key=lambda item: (-item[0], int(item[1].get("waste_id") or 0)))

    lines: list[str] = [_c("══ Optigravity Waste Analysis (sorted by impact) ══", "1;33")]
    headers = ["ID", "Category", "Session", "Evidence", "Fix Command"]
    table_rows: list[list[str]] = []
    for _sev, f, pb in enriched:
        table_rows.append([
            f"W{f['waste_id']}",
            str(pb["label"]),
            str(f["session_id"])[:12],
            str(f["detail"])[:42],
            str(pb["command"]),
        ])
    lines.append(_render_table(headers, table_rows))
    lines.append("")
    seen_ids: set[int] = set()
    for _sev, f, pb in enriched:
        wid = int(f["waste_id"])
        if wid in seen_ids:
            continue
        seen_ids.add(wid)
        lines.append(f"  [W{wid}] {pb['label']}: {pb['advice']}  ->  {pb['command']}")
    return "\n".join(lines)
