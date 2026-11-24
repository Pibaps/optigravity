"""SQLite WAL persistence, KPI views, idempotent UPSERTs, and waste detection (100% stdlib)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

INIT_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA auto_vacuum = INCREMENTAL;",
    "PRAGMA journal_size_limit = 67108864;",
    "PRAGMA temp_store = MEMORY;",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  repo_root TEXT NOT NULL,
  git_branch TEXT,
  git_head_start TEXT,
  git_head_end TEXT,
  title TEXT,
  category TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL DEFAULT 'in_progress',
  verifier_passed INTEGER,
  rollback_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  parent_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
  root_session_id TEXT NOT NULL,
  nesting_depth INTEGER NOT NULL DEFAULT 0,
  agent_name TEXT NOT NULL DEFAULT 'main',
  subagent_role TEXT,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  surface TEXT NOT NULL DEFAULT 'cli_tui',
  workspace_path TEXT NOT NULL,
  agy_version TEXT,
  plan_tier TEXT,
  initial_prompt TEXT,
  initial_model TEXT,
  initial_effort TEXT,
  initial_mode TEXT,
  sandbox_enabled INTEGER NOT NULL DEFAULT 0,
  transcript_path TEXT,
  transcript_byte_offset INTEGER NOT NULL DEFAULT 0,
  last_ingested_step INTEGER NOT NULL DEFAULT -1,
  status TEXT NOT NULL DEFAULT 'active',
  compaction_count INTEGER NOT NULL DEFAULT 0,
  total_cost_usd REAL NOT NULL DEFAULT 0.0,
  subagent_cost_usd REAL NOT NULL DEFAULT 0.0,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  wall_duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS turns (
  turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  step_index INTEGER NOT NULL,
  invocation_num INTEGER,
  response_id TEXT,
  model_id TEXT NOT NULL,
  effort_level TEXT,
  execution_mode TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  thinking_is_estimated INTEGER NOT NULL DEFAULT 0,
  context_window_used INTEGER,
  context_window_max INTEGER,
  exceeds_200k INTEGER NOT NULL DEFAULT 0,
  system_prompt_tokens INTEGER,
  tools_schema_tokens INTEGER,
  ttft_ms INTEGER,
  duration_ms INTEGER,
  gap_since_prev_turn_s REAL,
  estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
  is_compaction_turn INTEGER NOT NULL DEFAULT 0,
  data_source TEXT NOT NULL DEFAULT 'statusline',
  created_at TEXT NOT NULL,
  UNIQUE(session_id, step_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_response_id ON turns(response_id) WHERE response_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  turn_id INTEGER REFERENCES turns(turn_id) ON DELETE SET NULL,
  step_index INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  tool_category TEXT NOT NULL DEFAULT 'other',
  mcp_server TEXT,
  target_path_or_cmd TEXT,
  args_hash TEXT NOT NULL,
  args_bytes INTEGER NOT NULL DEFAULT 0,
  raw_output_bytes INTEGER NOT NULL DEFAULT 0,
  context_output_bytes INTEGER NOT NULL DEFAULT 0,
  est_output_tokens INTEGER NOT NULL DEFAULT 0,
  was_truncated_by_agy INTEGER NOT NULL DEFAULT 0,
  hook_action TEXT NOT NULL DEFAULT 'none',
  firebreak_rule TEXT,
  exit_code INTEGER,
  is_error INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  duration_ms INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, step_index, tool_name, args_hash)
);

CREATE TABLE IF NOT EXISTS routing_decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  stage TEXT NOT NULL,
  detected_category TEXT NOT NULL,
  features_json TEXT NOT NULL,
  chosen_model TEXT NOT NULL,
  chosen_effort TEXT NOT NULL,
  chosen_mode TEXT NOT NULL,
  propensity REAL NOT NULL DEFAULT 1.0,
  predicted_score REAL,
  actual_reward REAL,
  escalated_from_id INTEGER REFERENCES routing_decisions(decision_id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
  bucket_id TEXT NOT NULL,
  remaining_fraction REAL,
  reset_time TEXT,
  reset_in_seconds INTEGER,
  plan_tier TEXT,
  captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS hook_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  hook_type TEXT NOT NULL,
  action_taken TEXT,
  rule_name TEXT,
  tokens_saved_est INTEGER NOT NULL DEFAULT 0,
  hook_latency_ms REAL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id);
CREATE INDEX IF NOT EXISTS idx_turns_session_time ON turns(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_hash ON tool_calls(session_id, tool_name, args_hash);
CREATE INDEX IF NOT EXISTS idx_quota_bucket_time ON quota_snapshots(bucket_id, captured_at);

CREATE VIEW IF NOT EXISTS v_session_kpis AS
SELECT
  s.session_id,
  s.root_session_id,
  s.parent_session_id,
  s.task_id,
  s.status,
  s.initial_model,
  s.started_at,
  COUNT(t.turn_id) AS turn_count,
  COALESCE(SUM(t.input_tokens), 0) AS total_input_tokens,
  COALESCE(SUM(t.cache_read_tokens), 0) AS total_cache_read_tokens,
  COALESCE(SUM(t.cache_creation_tokens), 0) AS total_cache_creation_tokens,
  COALESCE(SUM(t.output_tokens), 0) AS total_output_tokens,
  COALESCE(SUM(t.thinking_tokens), 0) AS total_thinking_tokens,
  CAST(COALESCE(SUM(t.cache_read_tokens), 0) AS REAL) /
    NULLIF(COALESCE(SUM(t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens), 0), 0)
    AS cache_hit_ratio,
  COALESCE(MAX(t.context_window_used), 0) AS peak_context_tokens,
  COALESCE(SUM(t.estimated_cost_usd), 0.0) AS turns_cost_usd,
  s.total_cost_usd,
  s.subagent_cost_usd
FROM sessions s
LEFT JOIN turns t ON t.session_id = s.session_id
GROUP BY s.session_id;

CREATE VIEW IF NOT EXISTS v_task_rollup_with_subagents AS
WITH RECURSIVE session_tree(session_id, root_session_id, depth) AS (
  SELECT session_id, session_id AS root_session_id, 0 AS depth
  FROM sessions
  WHERE parent_session_id IS NULL
  UNION ALL
  SELECT c.session_id, p.root_session_id, p.depth + 1
  FROM sessions c
  JOIN session_tree p ON c.parent_session_id = p.session_id
  WHERE p.depth < 10
)
SELECT
  st.root_session_id,
  MAX(s.task_id) AS task_id,
  COUNT(DISTINCT st.session_id) AS session_count,
  MAX(st.depth) AS max_depth,
  COALESCE(SUM(t.input_tokens), 0) AS total_input_tokens,
  COALESCE(SUM(t.cache_read_tokens), 0) AS total_cache_read_tokens,
  COALESCE(SUM(t.cache_creation_tokens), 0) AS total_cache_creation_tokens,
  COALESCE(SUM(t.output_tokens), 0) AS total_output_tokens,
  COALESCE(SUM(t.thinking_tokens), 0) AS total_thinking_tokens,
  COALESCE(SUM(t.estimated_cost_usd), 0.0) AS total_estimated_cost_usd
FROM session_tree st
JOIN sessions s ON s.session_id = st.session_id
LEFT JOIN turns t ON t.session_id = st.session_id
GROUP BY st.root_session_id;

CREATE VIEW IF NOT EXISTS v_routing_performance AS
SELECT
  rd.detected_category AS category,
  rd.chosen_model AS model_id,
  rd.chosen_effort AS effort_level,
  COUNT(*) AS decision_count,
  AVG(CASE WHEN tk.outcome = 'success' THEN 1.0 WHEN tk.outcome IS NOT NULL AND tk.outcome != 'in_progress' THEN 0.0 ELSE NULL END) AS success_rate,
  AVG(rd.actual_reward) AS avg_reward,
  AVG(COALESCE(sk.turns_cost_usd, 0.0)) AS avg_cost_usd,
  AVG(COALESCE(sk.turn_count, 0)) AS avg_turns
FROM routing_decisions rd
LEFT JOIN tasks tk ON tk.task_id = rd.task_id
LEFT JOIN v_session_kpis sk ON sk.session_id = rd.session_id
GROUP BY rd.detected_category, rd.chosen_model, rd.chosen_effort;
"""


def utc_now_iso() -> str:
    """Return UTC ISO-8601 timestamp with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def canonical_args_hash(args: Any) -> str:
    """Compute canonical SHA-256 hash of tool arguments."""
    if isinstance(args, str):
        payload = args
    else:
        payload = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def connect(db_path: Path | str, *, init_schema: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with mandatory WAL PRAGMAs and explicit transaction control."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in INIT_PRAGMAS:
        conn.execute(pragma)
    if init_schema:
        ensure_schema(conn)
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Execute a write transaction starting with BEGIN IMMEDIATE."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT;")


def query_all(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[sqlite3.Row]:
    """Execute a SELECT, fetch all rows immediately, and close any implicit transaction."""
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    if conn.in_transaction:
        conn.execute("ROLLBACK;")
    return rows


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables, indexes, and views idempotently under BEGIN IMMEDIATE."""
    with write_tx(conn):
        for stmt in SCHEMA_SQL.split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s + ";")


def run_stop_maintenance(conn: sqlite3.Connection) -> None:
    """Run passive WAL checkpoint and incremental vacuum (Stop hook only)."""
    conn.execute("PRAGMA wal_checkpoint(PASSIVE);").fetchall()
    conn.execute("PRAGMA incremental_vacuum(200);").fetchall()


def ensure_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    workspace_path: str = "",
    root_session_id: str | None = None,
    parent_session_id: str | None = None,
    nesting_depth: int = 0,
    agent_name: str = "main",
    subagent_role: str | None = None,
    task_id: str | None = None,
    surface: str = "cli_tui",
    initial_prompt: str | None = None,
    initial_model: str | None = None,
    initial_effort: str | None = None,
    initial_mode: str | None = None,
    transcript_path: str | None = None,
    started_at: str | None = None,
) -> None:
    """Insert or update a session row idempotently."""
    now = started_at or utc_now_iso()
    root_id = root_session_id or session_id
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO sessions (
              session_id, parent_session_id, root_session_id, nesting_depth,
              agent_name, subagent_role, task_id, surface, workspace_path,
              initial_prompt, initial_model, initial_effort, initial_mode,
              transcript_path, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              parent_session_id = COALESCE(excluded.parent_session_id, sessions.parent_session_id),
              root_session_id = CASE
                WHEN sessions.root_session_id = sessions.session_id AND excluded.root_session_id != excluded.session_id
                THEN excluded.root_session_id
                ELSE sessions.root_session_id
              END,
              task_id = COALESCE(excluded.task_id, sessions.task_id),
              transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
              initial_model = COALESCE(sessions.initial_model, excluded.initial_model)
            """,
            (
                session_id,
                parent_session_id,
                root_id,
                nesting_depth,
                agent_name,
                subagent_role,
                task_id,
                surface,
                workspace_path,
                initial_prompt,
                initial_model,
                initial_effort,
                initial_mode,
                transcript_path,
                now,
            ),
        )


def upsert_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    step_index: int,
    model_id: str,
    task_id: str | None = None,
    invocation_num: int | None = None,
    response_id: str | None = None,
    effort_level: str | None = None,
    execution_mode: str | None = None,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    thinking_is_estimated: int = 0,
    context_window_used: int | None = None,
    context_window_max: int | None = None,
    exceeds_200k: int = 0,
    system_prompt_tokens: int | None = None,
    tools_schema_tokens: int | None = None,
    ttft_ms: int | None = None,
    duration_ms: int | None = None,
    gap_since_prev_turn_s: float | None = None,
    estimated_cost_usd: float = 0.0,
    is_compaction_turn: int = 0,
    data_source: str = "statusline",
    created_at: str | None = None,
) -> None:
    """Idempotent UPSERT on (session_id, step_index) keeping maximal/final token counts."""
    now = created_at or utc_now_iso()
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO turns (
              session_id, task_id, step_index, invocation_num, response_id,
              model_id, effort_level, execution_mode,
              input_tokens, cache_read_tokens, cache_creation_tokens,
              output_tokens, thinking_tokens, thinking_is_estimated,
              context_window_used, context_window_max, exceeds_200k,
              system_prompt_tokens, tools_schema_tokens,
              ttft_ms, duration_ms, gap_since_prev_turn_s,
              estimated_cost_usd, is_compaction_turn, data_source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, step_index) DO UPDATE SET
              response_id = COALESCE(excluded.response_id, turns.response_id),
              model_id = COALESCE(excluded.model_id, turns.model_id),
              effort_level = COALESCE(excluded.effort_level, turns.effort_level),
              execution_mode = COALESCE(excluded.execution_mode, turns.execution_mode),
              input_tokens = MAX(turns.input_tokens, excluded.input_tokens),
              cache_read_tokens = MAX(turns.cache_read_tokens, excluded.cache_read_tokens),
              cache_creation_tokens = MAX(turns.cache_creation_tokens, excluded.cache_creation_tokens),
              output_tokens = MAX(turns.output_tokens, excluded.output_tokens),
              thinking_tokens = CASE
                WHEN excluded.thinking_is_estimated = 0 AND excluded.thinking_tokens > 0 THEN excluded.thinking_tokens
                ELSE MAX(turns.thinking_tokens, excluded.thinking_tokens)
              END,
              thinking_is_estimated = CASE
                WHEN excluded.thinking_is_estimated = 0 AND excluded.thinking_tokens > 0 THEN 0
                ELSE turns.thinking_is_estimated
              END,
              context_window_used = COALESCE(excluded.context_window_used, turns.context_window_used),
              context_window_max = COALESCE(excluded.context_window_max, turns.context_window_max),
              exceeds_200k = MAX(turns.exceeds_200k, excluded.exceeds_200k),
              system_prompt_tokens = COALESCE(excluded.system_prompt_tokens, turns.system_prompt_tokens),
              tools_schema_tokens = COALESCE(excluded.tools_schema_tokens, turns.tools_schema_tokens),
              ttft_ms = COALESCE(excluded.ttft_ms, turns.ttft_ms),
              duration_ms = COALESCE(excluded.duration_ms, turns.duration_ms),
              gap_since_prev_turn_s = COALESCE(excluded.gap_since_prev_turn_s, turns.gap_since_prev_turn_s),
              estimated_cost_usd = MAX(turns.estimated_cost_usd, excluded.estimated_cost_usd),
              is_compaction_turn = MAX(turns.is_compaction_turn, excluded.is_compaction_turn),
              data_source = excluded.data_source
            """,
            (
                session_id,
                task_id,
                step_index,
                invocation_num,
                response_id,
                model_id,
                effort_level,
                execution_mode,
                input_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                output_tokens,
                thinking_tokens,
                thinking_is_estimated,
                context_window_used,
                context_window_max,
                exceeds_200k,
                system_prompt_tokens,
                tools_schema_tokens,
                ttft_ms,
                duration_ms,
                gap_since_prev_turn_s,
                estimated_cost_usd,
                is_compaction_turn,
                data_source,
                now,
            ),
        )


def upsert_tool_call(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    step_index: int,
    tool_name: str,
    args_hash: str,
    turn_id: int | None = None,
    tool_category: str = "other",
    mcp_server: str | None = None,
    target_path_or_cmd: str | None = None,
    args_bytes: int = 0,
    raw_output_bytes: int = 0,
    context_output_bytes: int = 0,
    est_output_tokens: int = 0,
    was_truncated_by_agy: int = 0,
    hook_action: str = "none",
    firebreak_rule: str | None = None,
    exit_code: int | None = None,
    is_error: int = 0,
    error_message: str | None = None,
    duration_ms: int | None = None,
    created_at: str | None = None,
) -> None:
    """Idempotent UPSERT on (session_id, step_index, tool_name, args_hash)."""
    now = created_at or utc_now_iso()
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO tool_calls (
              session_id, turn_id, step_index, tool_name, tool_category, mcp_server,
              target_path_or_cmd, args_hash, args_bytes, raw_output_bytes,
              context_output_bytes, est_output_tokens, was_truncated_by_agy,
              hook_action, firebreak_rule, exit_code, is_error, error_message,
              duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, step_index, tool_name, args_hash) DO UPDATE SET
              raw_output_bytes = MAX(tool_calls.raw_output_bytes, excluded.raw_output_bytes),
              context_output_bytes = MAX(tool_calls.context_output_bytes, excluded.context_output_bytes),
              est_output_tokens = MAX(tool_calls.est_output_tokens, excluded.est_output_tokens),
              was_truncated_by_agy = MAX(tool_calls.was_truncated_by_agy, excluded.was_truncated_by_agy),
              hook_action = CASE WHEN excluded.hook_action != 'none' THEN excluded.hook_action ELSE tool_calls.hook_action END,
              firebreak_rule = COALESCE(excluded.firebreak_rule, tool_calls.firebreak_rule),
              exit_code = COALESCE(excluded.exit_code, tool_calls.exit_code),
              is_error = MAX(tool_calls.is_error, excluded.is_error),
              error_message = COALESCE(excluded.error_message, tool_calls.error_message),
              duration_ms = COALESCE(excluded.duration_ms, tool_calls.duration_ms)
            """,
            (
                session_id,
                turn_id,
                step_index,
                tool_name,
                tool_category,
                mcp_server,
                target_path_or_cmd,
                args_hash,
                args_bytes,
                raw_output_bytes,
                context_output_bytes,
                est_output_tokens,
                was_truncated_by_agy,
                hook_action,
                firebreak_rule,
                exit_code,
                is_error,
                error_message,
                duration_ms,
                now,
            ),
        )


def detect_waste(conn: sqlite3.Connection, *, session_id: str | None = None) -> list[dict[str, Any]]:
    """Run the 8 automated waste-detection queries and return structured findings."""
    findings: list[dict[str, Any]] = []
    sess_filter = "AND t.session_id = ?" if session_id else ""
    params: tuple[Any, ...] = (session_id,) if session_id else ()

    # 1. Low cache hit (< 70%) distinguishing server wipe (gap < 180s) vs TTL expiry (gap >= 180s)
    rows = query_all(
        conn,
        f"""
        SELECT
          t.session_id,
          COUNT(*) AS low_cache_turns,
          SUM(CASE WHEN COALESCE(t.gap_since_prev_turn_s, 0) < 180 THEN 1 ELSE 0 END) AS server_wipes,
          SUM(CASE WHEN COALESCE(t.gap_since_prev_turn_s, 0) >= 180 THEN 1 ELSE 0 END) AS ttl_expiries,
          AVG(CAST(t.cache_read_tokens AS REAL) / NULLIF(t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens, 0)) AS avg_hit
        FROM turns t
        WHERE t.step_index > 0
          AND (t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens) > 0
          AND (CAST(t.cache_read_tokens AS REAL) / (t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens)) < 0.70
          {sess_filter}
        GROUP BY t.session_id
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 1,
            "kind": "low_cache_hit",
            "session_id": r["session_id"],
            "detail": f"turns={r['low_cache_turns']} server_wipes={r['server_wipes']} ttl_expiries={r['ttl_expiries']} avg_hit={round((r['avg_hit'] or 0)*100, 1)}%",
        })

    # 2. Retry loops (COUNT >= 3 and SUM(is_error) >= 2 on same args_hash)
    tc_filter = "WHERE tc.session_id = ?" if session_id else ""
    rows = query_all(
        conn,
        f"""
        SELECT tc.session_id, tc.tool_name, tc.args_hash, COUNT(*) AS attempts, SUM(tc.is_error) AS errors
        FROM tool_calls tc
        {tc_filter}
        GROUP BY tc.session_id, tc.args_hash
        HAVING COUNT(*) >= 3 AND SUM(tc.is_error) >= 2
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 2,
            "kind": "retry_loop",
            "session_id": r["session_id"],
            "detail": f"tool={r['tool_name']} attempts={r['attempts']} errors={r['errors']} hash={r['args_hash'][:8]}",
        })

    # 3. Obese tool outputs (> 4000 tokens or >= 32768 bytes)
    tc_and = "AND tc.session_id = ?" if session_id else ""
    rows = query_all(
        conn,
        f"""
        SELECT tc.session_id, tc.tool_name, tc.target_path_or_cmd, tc.est_output_tokens, tc.raw_output_bytes
        FROM tool_calls tc
        WHERE (tc.est_output_tokens > 4000 OR tc.raw_output_bytes >= 32768)
          {tc_and}
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 3,
            "kind": "obese_tool_output",
            "session_id": r["session_id"],
            "detail": f"tool={r['tool_name']} est_tokens={r['est_output_tokens']} bytes={r['raw_output_bytes']}",
        })

    # 4. Reasoning over-effort
    rows = query_all(
        conn,
        f"""
        SELECT t.session_id, t.step_index, t.effort_level, t.thinking_tokens, t.output_tokens, tk.category
        FROM turns t
        LEFT JOIN tasks tk ON tk.task_id = t.task_id
        WHERE (
          (t.effort_level IN ('high', 'xhigh', 'max') AND tk.category IN ('trivial_edit', 'lookup_qa'))
          OR (t.thinking_tokens > 5 * MAX(t.output_tokens, 1) AND t.thinking_tokens > 1000)
        )
        {sess_filter}
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 4,
            "kind": "reasoning_over_effort",
            "session_id": r["session_id"],
            "detail": f"step={r['step_index']} effort={r['effort_level']} category={r['category']} think={r['thinking_tokens']} out={r['output_tokens']}",
        })

    # 5. Long sessions exposed to context rot
    sk_filter = "AND sk.session_id = ?" if session_id else ""
    rows = query_all(
        conn,
        f"""
        SELECT sk.session_id, sk.peak_context_tokens, sk.turn_count, COALESCE(tk.rollback_count, 0) AS rollback_count
        FROM v_session_kpis sk
        LEFT JOIN tasks tk ON tk.task_id = sk.task_id
        WHERE (sk.peak_context_tokens >= 120000 OR sk.turn_count >= 25 OR COALESCE(tk.rollback_count, 0) > 0)
        {sk_filter}
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 5,
            "kind": "context_rot_risk",
            "session_id": r["session_id"],
            "detail": f"peak_ctx={r['peak_context_tokens']} turns={r['turn_count']} rollbacks={r['rollback_count']}",
        })

    # 6. Duplicate file reads (>= 2x without compaction)
    rows = query_all(
        conn,
        f"""
        SELECT tc.session_id, tc.target_path_or_cmd, COUNT(*) AS read_count
        FROM tool_calls tc
        JOIN sessions s ON s.session_id = tc.session_id
        WHERE tc.tool_name IN ('view_file', 'read_file')
          AND tc.target_path_or_cmd IS NOT NULL
          AND s.compaction_count = 0
          {tc_and}
        GROUP BY tc.session_id, tc.target_path_or_cmd
        HAVING COUNT(*) >= 2
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 6,
            "kind": "duplicate_file_read",
            "session_id": r["session_id"],
            "detail": f"path={r['target_path_or_cmd']} reads={r['read_count']}",
        })

    # 7. MCP schema bloat (> 15000 tokens replayed per turn)
    rows = query_all(
        conn,
        f"""
        SELECT t.session_id, MAX(t.tools_schema_tokens) AS max_schema_tokens
        FROM turns t
        WHERE t.tools_schema_tokens > 15000
          {sess_filter}
        GROUP BY t.session_id
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 7,
            "kind": "mcp_schema_bloat",
            "session_id": r["session_id"],
            "detail": f"tools_schema_tokens={r['max_schema_tokens']}",
        })

    # 8. Subagent drift (subagent_cost > parent_cost or failed subagents)
    s_filter = "AND s.session_id = ?" if session_id else ""
    rows = query_all(
        conn,
        f"""
        SELECT s.session_id, s.parent_session_id, s.subagent_cost_usd, s.total_cost_usd, s.status
        FROM sessions s
        WHERE (
          (s.subagent_cost_usd > s.total_cost_usd AND s.total_cost_usd > 0)
          OR (s.parent_session_id IS NOT NULL AND s.status = 'failed')
        )
        {s_filter}
        """,
        params,
    )
    for r in rows:
        findings.append({
            "waste_id": 8,
            "kind": "subagent_drift",
            "session_id": r["session_id"],
            "detail": f"status={r['status']} sub_cost={r['subagent_cost_usd']} total_cost={r['total_cost_usd']}",
        })

    return findings
