"""Fail-open stdlib-only hook dispatcher (`ogy-hook <event>`)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from ogy_core.config import OgyConfig, load_config
from ogy_core.firebreaks import evaluate_pre_tool_firebreaks
from ogy_core.ingest import categorize_tool, enrich_from_agy_dbs, ingest_transcript_incremental
from ogy_core.pricing import normalize_model_id
from ogy_core.router import compute_reward
from ogy_core.storage import (
    canonical_args_hash,
    connect,
    ensure_session,
    query_all,
    run_stop_maintenance,
    upsert_tool_call,
    utc_now_iso,
    write_tx,
)


def _neutral_response(event: str) -> dict[str, Any]:
    if event == "pre-tool":
        return {"decision": ""}
    if event == "stop":
        return {"decision": "stop"}
    return {}


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _record_hook_event(
    conn: Any,
    *,
    session_id: str,
    hook_type: str,
    action_taken: str,
    rule_name: str | None = None,
    latency_ms: float = 0.0,
) -> None:
    if not session_id:
        return
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO hook_events (session_id, hook_type, action_taken, rule_name, hook_latency_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, hook_type, action_taken, rule_name, round(latency_ms, 2)),
        )


def _handle_session_start(payload: dict[str, Any], cfg: OgyConfig) -> dict[str, Any]:
    session_id = str(payload.get("conversationId") or "").strip()
    ws_paths = payload.get("workspacePaths")
    workspace = ws_paths[0] if isinstance(ws_paths, list) and ws_paths else "."
    model_id = normalize_model_id(str(payload.get("modelName") or "gemini-4-hydrogen"))

    if session_id:
        conn = connect(cfg.db_path, init_schema=True)
        try:
            ensure_session(
                conn,
                session_id=session_id,
                workspace_path=str(workspace),
                initial_prompt=payload.get("lastUserInput"),
                initial_model=model_id,
                transcript_path=payload.get("transcriptPath"),
            )
        finally:
            conn.close()

    # Inject pending .ogy/handoff.md once (100% cacheable prefix) then archive it
    handoff_path = Path(workspace) / ".ogy" / "handoff.md"
    if handoff_path.is_file():
        content = handoff_path.read_text(encoding="utf-8", errors="replace").strip()
        archived = handoff_path.with_name("handoff.archived.md")
        try:
            handoff_path.replace(archived)
        except OSError:
            pass
        if content:
            return {"injectSteps": [{"userMessage": f"[ogy handoff]\n{content}"}]}

    return {}


def _handle_pre_tool(payload: dict[str, Any], cfg: OgyConfig, t0: float) -> dict[str, Any]:
    session_id = str(payload.get("conversationId") or "").strip()
    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tc.get("name") or "")
    tool_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}

    conn = connect(cfg.db_path, init_schema=True)
    try:
        resp = evaluate_pre_tool_firebreaks(
            conn,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            cfg=cfg,
        )
        action = "overwrite" if "overwrite" in resp else (resp.get("decision") or "allow")
        _record_hook_event(
            conn,
            session_id=session_id,
            hook_type="PreToolUse",
            action_taken=action,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return resp
    finally:
        conn.close()


def _handle_post_tool(payload: dict[str, Any], cfg: OgyConfig) -> dict[str, Any]:
    session_id = str(payload.get("conversationId") or "").strip()
    if not session_id:
        return {}
    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tc.get("name") or "unknown")
    tool_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    step_idx = int(payload.get("stepIdx") or 0)
    err = payload.get("error")
    tcat, mcp_srv = categorize_tool(tool_name)
    target_val = tool_args.get("Path") or tool_args.get("CommandLine") or tool_args.get("path")

    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id=session_id, workspace_path=".")
        upsert_tool_call(
            conn,
            session_id=session_id,
            step_index=step_idx,
            tool_name=tool_name,
            tool_category=tcat,
            mcp_server=mcp_srv,
            target_path_or_cmd=str(target_val) if target_val else None,
            args_hash=canonical_args_hash(tool_args),
            args_bytes=len(json.dumps(tool_args, ensure_ascii=False).encode("utf-8")),
            is_error=1 if err else 0,
            error_message=str(err) if err else None,
        )
    finally:
        conn.close()
    return {}


def _handle_pre_invocation(payload: dict[str, Any], cfg: OgyConfig) -> dict[str, Any]:
    # Silent on nominal & whenever invocationNum > 1 to never bust Prompt Cache
    inv_num = int(payload.get("invocationNum") or 1)
    if inv_num > 1:
        return {}

    session_id = str(payload.get("conversationId") or "").strip()
    if not session_id or cfg.budget_hard_cap_usd is None:
        return {}

    conn = connect(cfg.db_path, init_schema=True)
    try:
        rows = query_all(
            conn,
            "SELECT total_cost_usd FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if rows and float(rows[0]["total_cost_usd"] or 0.0) >= cfg.budget_hard_cap_usd:
            return {
                "terminationBehavior": "terminate",
                "injectSteps": [
                    {
                        "errorMessage": {
                            "userMessage": f"[ogy] Plafond budget atteint (${cfg.budget_hard_cap_usd:.2f})."
                        }
                    }
                ],
            }
    finally:
        conn.close()
    return {}


def _handle_post_invocation(payload: dict[str, Any], cfg: OgyConfig) -> dict[str, Any]:
    session_id = str(payload.get("conversationId") or "").strip()
    transcript_path = payload.get("transcriptPath")
    if session_id and transcript_path:
        conn = connect(cfg.db_path, init_schema=True)
        try:
            ensure_session(
                conn,
                session_id=session_id,
                transcript_path=str(transcript_path),
                workspace_path=".",
            )
            ingest_transcript_incremental(conn, session_id, str(transcript_path))
        finally:
            conn.close()
    return {}


def _handle_stop(payload: dict[str, Any], cfg: OgyConfig) -> dict[str, Any]:
    session_id = str(payload.get("conversationId") or "").strip()
    if session_id:
        conn = connect(cfg.db_path, init_schema=True)
        try:
            transcript_path = payload.get("transcriptPath")
            if transcript_path:
                ingest_transcript_incremental(conn, session_id, str(transcript_path))
            enrich_from_agy_dbs(conn, session_id, cfg.agy_cli_dir)

            err = payload.get("error")
            status = "failed" if err else "completed"
            now = utc_now_iso()
            with write_tx(conn):
                conn.execute(
                    "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                    (status, now, session_id),
                )

            # Update routing_decisions.actual_reward if a routing decision exists
            kpis = query_all(
                conn, "SELECT turns_cost_usd FROM v_session_kpis WHERE session_id = ?", (session_id,)
            )
            cost_usd = float(kpis[0]["turns_cost_usd"] or 0.0) if kpis else 0.0
            reward = compute_reward(
                outcome="failed" if err else "success",
                verifier_passed=None,
                cost_usd=cost_usd,
            )
            with write_tx(conn):
                conn.execute(
                    "UPDATE routing_decisions SET actual_reward = ? WHERE session_id = ? AND actual_reward IS NULL",
                    (reward, session_id),
                )

            run_stop_maintenance(conn)
        finally:
            conn.close()
    return {"decision": "stop"}


def dispatch_hook(event: str, payload: dict[str, Any], cfg: OgyConfig | None = None) -> dict[str, Any]:
    """Dispatch a hook event with a pre-parsed payload (testable without stdin)."""
    t0 = time.perf_counter()
    resolved_cfg = cfg or load_config()
    if event == "session-start":
        return _handle_session_start(payload, resolved_cfg)
    if event == "pre-tool":
        return _handle_pre_tool(payload, resolved_cfg, t0)
    if event == "post-tool":
        return _handle_post_tool(payload, resolved_cfg)
    if event == "pre-invocation":
        return _handle_pre_invocation(payload, resolved_cfg)
    if event == "post-invocation":
        return _handle_post_invocation(payload, resolved_cfg)
    if event == "stop":
        return _handle_stop(payload, resolved_cfg)
    return {}


def main(argv: list[str] | None = None) -> int:
    """Absolute fail-open entrypoint: always outputs valid JSON (or statusline) and exits 0."""
    args = argv if argv is not None else sys.argv[1:]
    event = args[0] if args else ""

    if event == "statusline":
        try:
            from ogy_statusline.render import run_statusline_from_stdin

            return run_statusline_from_stdin()
        except BaseException:
            try:
                sys.stdout.write("ogy | OK\n")
                sys.stdout.flush()
            except BaseException:
                pass
            return 0

    try:
        payload = _read_stdin_json()
        resp = dispatch_hook(event, payload)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except BaseException:
        try:
            sys.stdout.write(json.dumps(_neutral_response(event)) + "\n")
            sys.stdout.flush()
        except BaseException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
