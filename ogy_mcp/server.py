"""Minimal stdio MCP server (`mcp>=2,<3`, zero stdout prints)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ogy_core.config import load_config
from ogy_core.context_health import assess_context_health, write_handoff
from ogy_core.firebreaks import recall_stashed_output
from ogy_core.storage import connect, detect_waste, query_all

mcp = MCPServer("optigravity")


@mcp.tool()
def ogy_session_budget(session_id: str | None = None) -> str:
    """Return active context tokens, warm cache hit %, idle gap, top waste, and recommended action."""
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        if session_id:
            rows = query_all(conn, "SELECT * FROM v_session_kpis WHERE session_id = ?", (session_id,))
        else:
            rows = query_all(conn, "SELECT * FROM v_session_kpis ORDER BY started_at DESC LIMIT 1")
        if not rows:
            return json.dumps({"status": "no_sessions", "advice": "keep_warm"})

        row = dict(rows[0])
        sid = row["session_id"]
        turn_rows = query_all(
            conn,
            "SELECT gap_since_prev_turn_s, exceeds_200k FROM turns WHERE session_id = ? ORDER BY step_index DESC LIMIT 1",
            (sid,),
        )
        idle_gap = float(turn_rows[0]["gap_since_prev_turn_s"] or 0.0) if turn_rows else 0.0
        exceeds_200k = bool(turn_rows[0]["exceeds_200k"]) if turn_rows else False

        health = assess_context_health(
            context_tokens=int(row["peak_context_tokens"] or 0),
            turn_count=int(row["turn_count"] or 0),
            cache_hit_ratio=row["cache_hit_ratio"],
            idle_gap_s=idle_gap,
            exceeds_200k=exceeds_200k,
            model_id=row["initial_model"] or "gemini-4-hydrogen",
        )
        waste = detect_waste(conn, session_id=sid)
        return json.dumps(
            {
                "session_id": sid,
                "active_context_tokens": int(row["peak_context_tokens"] or 0),
                "warm_cache_hit_pct": health.cache_hit_pct,
                "idle_gap_s": idle_gap,
                "zone": health.zone,
                "advice": health.action,
                "reason": health.reason,
                "top_waste": waste[:3],
            },
            ensure_ascii=False,
        )
    finally:
        conn.close()


@mcp.tool()
def ogy_recall(hash_prefix: str, start_line: int = 1, end_line: int = 100) -> str:
    """Bounded read (by hash prefix and 1-based line range) of raw shell output stashed by ogy wrap."""
    return recall_stashed_output(hash_prefix, lines_spec=f"{start_line}:{end_line}")


@mcp.tool()
def ogy_write_handoff(
    goal: str,
    modified_files: list[str],
    checks: str,
    negative_constraints: list[str],
    next_step: str,
    workspace_path: str = ".",
) -> str:
    """Generate a compact normalized .ogy/handoff.md (<= 30 lines) before /clear."""
    res = write_handoff(
        goal=goal,
        modified_files=modified_files,
        checks=checks,
        negative_constraints=negative_constraints,
        next_step=next_step,
        workspace_path=workspace_path,
    )
    return json.dumps({"path": res["path"], "lines": res["lines"]})


def main() -> None:
    """Run MCP stdio server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
