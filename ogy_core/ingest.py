"""Hybrid multi-source telemetry ingestor (100% stdlib)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ogy_core.pricing import estimate_turn_cost, normalize_model_id
from ogy_core.storage import (
    canonical_args_hash,
    ensure_session,
    query_all,
    upsert_tool_call,
    upsert_turn,
    utc_now_iso,
    write_tx,
)

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def strip_user_request_envelope(text: str) -> str:
    """Strip <USER_REQUEST>...</USER_REQUEST> wrapper if present."""
    if not text:
        return ""
    m = USER_REQUEST_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def categorize_tool(tool_name: str) -> tuple[str, str | None]:
    """Map tool name to (tool_category, mcp_server)."""
    name = (tool_name or "").strip()
    if name in ("run_command", "bash", "shell", "execute_command"):
        return "shell", None
    if name in ("view_file", "read_file", "view_file_outline", "view_code_item"):
        return "file_read", None
    if name in ("write_to_file", "write_file", "replace_file_content", "multi_replace_file_content", "edit_file"):
        return "file_write", None
    if name in ("grep_search", "find_by_name", "list_dir", "search_files"):
        return "search", None
    if name in ("search_web", "read_url_content"):
        return "web", None
    if name in ("invoke_subagent", "send_message"):
        return "subagent", None
    if name.startswith("mcp_") or "__" in name:
        server = name.split("__", 1)[0] if "__" in name else name.split("_", 2)[1] if name.count("_") >= 2 else "mcp"
        return "mcp", server
    return "other", None


def ingest_statusline_payload(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    enterprise_cache: bool = False,
) -> None:
    """Ingest real-time statusLine JSON payload into sessions, turns, and quota_snapshots."""
    session_id = str(payload.get("conversationId") or payload.get("session_id") or "").strip()
    if not session_id:
        return

    ws_paths = payload.get("workspacePaths")
    workspace = ws_paths[0] if isinstance(ws_paths, list) and ws_paths else str(payload.get("workspace_path") or "")
    model_raw = str(payload.get("modelName") or payload.get("model") or "gemini-4-hydrogen")
    model_id = normalize_model_id(model_raw)

    ensure_session(
        conn,
        session_id=session_id,
        workspace_path=workspace,
        initial_model=model_id,
        transcript_path=payload.get("transcriptPath"),
    )

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)

    ctx_win = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    ctx_used = ctx_win.get("used") or payload.get("context_window_used")
    if ctx_used is None and (input_tokens + cache_read + cache_create) > 0:
        ctx_used = input_tokens + cache_read + cache_create
    ctx_max = ctx_win.get("max") or payload.get("context_window_max")
    exceeds_200k = 1 if payload.get("exceeds_200k_tokens") else 0

    cost_obj = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    total_usd = float(cost_obj.get("total_usd") or 0.0)
    subagent_usd = float(cost_obj.get("subagent_usd") or 0.0)

    step_index = int(payload.get("stepIdx") or payload.get("step_index") or 0)
    est_cost = estimate_turn_cost(
        model_id,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
        output_tokens=output_tokens,
        enterprise=enterprise_cache,
    )

    if any((input_tokens, cache_read, cache_create, output_tokens)):
        upsert_turn(
            conn,
            session_id=session_id,
            step_index=step_index,
            model_id=model_id,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
            output_tokens=output_tokens,
            context_window_used=int(ctx_used) if ctx_used is not None else None,
            context_window_max=int(ctx_max) if ctx_max is not None else None,
            exceeds_200k=exceeds_200k,
            estimated_cost_usd=est_cost,
            data_source="statusline",
        )

    if total_usd > 0 or subagent_usd > 0:
        with write_tx(conn):
            conn.execute(
                """
                UPDATE sessions
                SET total_cost_usd = MAX(total_cost_usd, ?),
                    subagent_cost_usd = MAX(subagent_cost_usd, ?)
                WHERE session_id = ?
                """,
                (total_usd, subagent_usd, session_id),
            )

    quota = payload.get("quota")
    if isinstance(quota, dict):
        now = utc_now_iso()
        with write_tx(conn):
            for bucket_id, qinfo in quota.items():
                if not isinstance(qinfo, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO quota_snapshots (
                      session_id, bucket_id, remaining_fraction, reset_time, reset_in_seconds, plan_tier, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        str(bucket_id),
                        qinfo.get("remaining_fraction"),
                        qinfo.get("reset_time"),
                        qinfo.get("reset_in_seconds"),
                        payload.get("plan_tier"),
                        now,
                    ),
                )


def resolve_transcript_full_path(transcript_path: str | Path) -> Path | None:
    """Prefer transcript_full.jsonl over transcript.jsonl when available."""
    p = Path(transcript_path)
    if p.name == "transcript.jsonl":
        full = p.with_name("transcript_full.jsonl")
        if full.is_file():
            return full
    if p.is_file():
        return p
    return None


def ingest_transcript_incremental(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str | Path,
) -> int:
    """Read new JSONL lines from transcript_byte_offset and UPSERT turns/tool_calls.
    Returns number of new records ingested.
    """
    target = resolve_transcript_full_path(transcript_path)
    if target is None:
        return 0

    rows = query_all(
        conn,
        "SELECT transcript_byte_offset, last_ingested_step, initial_model FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    offset = int(rows[0]["transcript_byte_offset"]) if rows else 0
    last_step = int(rows[0]["last_ingested_step"]) if rows else -1
    default_model = rows[0]["initial_model"] if rows and rows[0]["initial_model"] else "gemini-4-hydrogen"

    file_size = target.stat().st_size
    if offset >= file_size:
        return 0

    ingested = 0
    with target.open("rb") as fh:
        fh.seek(offset)
        raw_chunk = fh.read()
        new_offset = fh.tell()

    for line_bytes in raw_chunk.splitlines():
        line_str = line_bytes.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            rec = json.loads(line_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue

        step_idx = rec.get("stepIdx")
        if step_idx is None:
            step_idx = rec.get("step_index")
        if step_idx is None:
            last_step += 1
            step_idx = last_step
        else:
            step_idx = int(step_idx)
            if step_idx > last_step:
                last_step = step_idx

        status = str(rec.get("status") or "").upper()
        if status and status not in ("DONE", "COMPLETED", "ERROR", "FAILED", ""):
            # Skip intermediate streaming snapshots if status is explicitly in-progress
            if status in ("RUNNING", "STREAMING", "PENDING"):
                continue

        model_id = normalize_model_id(str(rec.get("modelName") or rec.get("model") or default_model))
        usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
        in_tok = int(usage.get("input_tokens") or rec.get("input_tokens") or 0)
        cr_tok = int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
        cc_tok = int(usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or rec.get("output_tokens") or 0)
        think_tok = int(usage.get("thinking_tokens") or rec.get("thinking_tokens") or 0)

        if any((in_tok, cr_tok, cc_tok, out_tok, think_tok)):
            upsert_turn(
                conn,
                session_id=session_id,
                step_index=step_idx,
                model_id=model_id,
                response_id=rec.get("responseId") or rec.get("response_id"),
                input_tokens=in_tok,
                cache_read_tokens=cr_tok,
                cache_creation_tokens=cc_tok,
                output_tokens=out_tok,
                thinking_tokens=think_tok,
                thinking_is_estimated=1 if think_tok == 0 else 0,
                estimated_cost_usd=estimate_turn_cost(
                    model_id,
                    input_tokens=in_tok,
                    cache_read_tokens=cr_tok,
                    cache_creation_tokens=cc_tok,
                    output_tokens=out_tok,
                    thinking_tokens=think_tok,
                ),
                data_source="transcript_est",
            )
            ingested += 1

        tool_calls = rec.get("toolCalls") or rec.get("tool_calls") or []
        if isinstance(rec.get("toolCall"), dict):
            tool_calls = [rec["toolCall"]]
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tname = str(tc.get("name") or tc.get("tool_name") or "unknown")
                targs = tc.get("args") or tc.get("arguments") or {}
                tcat, mcp_srv = categorize_tool(tname)
                raw_out = tc.get("rawOutput") or tc.get("output") or ""
                raw_bytes = len(raw_out.encode("utf-8", errors="replace")) if isinstance(raw_out, str) else int(tc.get("raw_output_bytes") or 0)
                target_val = None
                if isinstance(targs, dict):
                    target_val = targs.get("Path") or targs.get("TargetFile") or targs.get("CommandLine") or targs.get("path")
                upsert_tool_call(
                    conn,
                    session_id=session_id,
                    step_index=step_idx,
                    tool_name=tname,
                    tool_category=tcat,
                    mcp_server=mcp_srv,
                    target_path_or_cmd=str(target_val) if target_val else None,
                    args_hash=canonical_args_hash(targs),
                    args_bytes=len(json.dumps(targs, ensure_ascii=False).encode("utf-8")),
                    raw_output_bytes=raw_bytes,
                    est_output_tokens=max(0, raw_bytes // 4),
                    is_error=1 if tc.get("error") or tc.get("is_error") else 0,
                    error_message=str(tc.get("error")) if tc.get("error") else None,
                )
                ingested += 1

    with write_tx(conn):
        conn.execute(
            """
            UPDATE sessions
            SET transcript_byte_offset = ?,
                last_ingested_step = MAX(last_ingested_step, ?)
            WHERE session_id = ?
            """,
            (new_offset, last_step, session_id),
        )
    return ingested


def open_agy_db_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open an Antigravity internal SQLite DB strictly read-only (immutable=1 + query_only)."""
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        return conn
    except sqlite3.Error:
        return None


def enrich_from_agy_dbs(
    conn: sqlite3.Connection,
    session_id: str,
    agy_cli_dir: Path,
) -> dict[str, Any]:
    """Best-effort read-only enrichment from conversations/<id>.db and conversation_summaries.db."""
    result: dict[str, Any] = {"summary_linked": False, "conv_db_found": False}

    summaries_db = agy_cli_dir / "conversation_summaries.db"
    s_conn = open_agy_db_readonly(summaries_db)
    if s_conn is not None:
        try:
            tbl = "conversation_summaries"
            cols_info = s_conn.execute(f"PRAGMA table_info({tbl});").fetchall()
            if not cols_info:
                tbl = "conversations"
                cols_info = s_conn.execute(f"PRAGMA table_info({tbl});").fetchall()
            col_names = {r["name"] for r in cols_info}
            if col_names:
                id_col = "conversation_id" if "conversation_id" in col_names else "id" if "id" in col_names else None
                rows = (
                    s_conn.execute(f"SELECT * FROM {tbl} WHERE {id_col} = ?", (session_id,)).fetchall()
                    if id_col
                    else []
                )
                if rows:
                    row = dict(rows[0])
                    parent_id = row.get("parent_conversation_id") or row.get("parent_session_id")
                    if parent_id:
                        with write_tx(conn):
                            conn.execute(
                                "UPDATE sessions SET parent_session_id = ?, root_session_id = ? WHERE session_id = ?",
                                (parent_id, parent_id, session_id),
                            )
                        result["summary_linked"] = True
        except sqlite3.Error:
            pass
        finally:
            s_conn.close()

    conv_db = agy_cli_dir / "conversations" / f"{session_id}.db"
    c_conn = open_agy_db_readonly(conv_db)
    if c_conn is not None:
        result["conv_db_found"] = True
        c_conn.close()

    return result


def ingest_acp_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    step_index: int,
    workspace_path: str,
    model_id: str,
    effort_level: str | None = None,
    execution_mode: str | None = None,
    input_tokens: int = 0,
    cached_read_tokens: int = 0,
    cached_write_tokens: int = 0,
    output_tokens: int = 0,
    thought_tokens: int = 0,
    context_window_used: int | None = None,
    context_window_max: int | None = None,
    cost_usd: float | None = None,
    gap_since_prev_turn_s: float | None = None,
    enterprise_cache: bool = False,
) -> float:
    """Persist an ACP v1 logical turn (with exact thoughtTokens) into SQLite WAL."""
    norm_model = normalize_model_id(model_id)
    ensure_session(
        conn,
        session_id=session_id,
        workspace_path=workspace_path,
        surface="cli_acp",
        initial_model=norm_model,
        initial_effort=effort_level,
        initial_mode=execution_mode,
    )
    est_cost = (
        float(cost_usd)
        if cost_usd is not None
        else estimate_turn_cost(
            norm_model,
            input_tokens=input_tokens,
            cache_read_tokens=cached_read_tokens,
            cache_creation_tokens=cached_write_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thought_tokens,
            enterprise=enterprise_cache,
        )
    )
    ctx_used = context_window_used
    if ctx_used is None and (input_tokens + cached_read_tokens + cached_write_tokens) > 0:
        ctx_used = input_tokens + cached_read_tokens + cached_write_tokens

    upsert_turn(
        conn,
        session_id=session_id,
        step_index=step_index,
        model_id=norm_model,
        effort_level=effort_level,
        execution_mode=execution_mode,
        input_tokens=input_tokens,
        cache_read_tokens=cached_read_tokens,
        cache_creation_tokens=cached_write_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thought_tokens,
        thinking_is_estimated=0,
        context_window_used=ctx_used,
        context_window_max=context_window_max,
        exceeds_200k=1 if (ctx_used or 0) > 200_000 else 0,
        gap_since_prev_turn_s=gap_since_prev_turn_s,
        estimated_cost_usd=est_cost,
        data_source="acp",
    )
    return est_cost


def get_latest_quota_snapshots(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Return the most recent quota snapshot per bucket_id (5h / weekly) without polling agy."""
    rows = query_all(
        conn,
        """
        SELECT q.bucket_id, q.remaining_fraction, q.reset_time, q.reset_in_seconds, q.plan_tier, q.captured_at
        FROM quota_snapshots q
        INNER JOIN (
            SELECT bucket_id, MAX(snapshot_id) AS max_id
            FROM quota_snapshots
            GROUP BY bucket_id
        ) latest ON latest.max_id = q.snapshot_id
        ORDER BY q.bucket_id ASC
        """,
    )
    return {str(r["bucket_id"]): dict(r) for r in rows}

