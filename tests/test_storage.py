"""Unit tests for SQLite WAL storage, idempotent UPSERTs, views, and 8 waste queries."""

from __future__ import annotations

from pathlib import Path

from ogy_core.storage import (
    canonical_args_hash,
    connect,
    detect_waste,
    ensure_session,
    query_all,
    run_stop_maintenance,
    upsert_tool_call,
    upsert_turn,
    write_tx,
)


def test_wal_pragmas_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert jm.lower() == "wal"
        assert fk == 1

        tables = {
            r[0]
            for r in query_all(
                conn, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "tasks",
            "sessions",
            "turns",
            "tool_calls",
            "routing_decisions",
            "quota_snapshots",
            "hook_events",
            "prompt_history",
            "subagent_runs",
        }
        uv = int(conn.execute("PRAGMA user_version;").fetchone()[0])
        assert uv == 2
        run_stop_maintenance(conn)
    finally:
        conn.close()


def test_zero_ddl_locks_on_100_polls_and_readonly(tmp_path: Path) -> None:
    import sqlite3

    from ogy_core.storage import connect_readonly, ensure_schema

    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        # Install authorizer that rejects any DDL/WRITE once schema is at v2
        ddl_or_writes = 0

        def authorizer(action, arg1, arg2, dbname, source):
            nonlocal ddl_or_writes
            if action in (
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_CREATE_VIEW,
                sqlite3.SQLITE_DROP_VIEW,
                sqlite3.SQLITE_CREATE_INDEX,
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            ):
                ddl_or_writes += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        for _ in range(100):
            ensure_schema(conn)
        assert ddl_or_writes == 0
    finally:
        conn.close()

    # 100 consecutive read-only polls must succeed without locking or allowing writes
    for _ in range(100):
        ro = connect_readonly(db_path)
        try:
            assert query_all(ro, "SELECT COUNT(*) AS c FROM sessions")[0]["c"] == 0
        finally:
            ro.close()


def test_camelcase_statusline_and_gen_metadata_enrichment(tmp_path: Path) -> None:
    import json
    import sqlite3

    from ogy_core.ingest import enrich_from_agy_dbs, ingest_statusline_payload

    db_path = tmp_path / "ogy.db"
    agy_dir = tmp_path / "agy-cli"
    conv_dir = agy_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path, init_schema=True)
    try:
        ingest_statusline_payload(
            conn,
            {
                "conversationId": "sess-camel-db",
                "modelName": "gemini-4-neon",
                "stepIndex": 1,
                "usage": {
                    "inputTokens": 3_000,
                    "cacheReadInputTokens": 12_000,
                    "cacheCreationInputTokens": 500,
                    "outputTokens": 250,
                },
                "contextWindow": {"usedTokens": 15_500, "maxTokens": 200_000},
                "cost": {"totalUsd": 0.42},
                "quota": {"gemini-5h": {"remainingFraction": 0.77, "resetInSeconds": 3600}},
            },
        )
        trows = query_all(conn, "SELECT * FROM turns WHERE session_id = 'sess-camel-db'")
        assert len(trows) == 1
        assert trows[0]["input_tokens"] == 3_000
        assert trows[0]["cache_read_tokens"] == 12_000
        assert trows[0]["output_tokens"] == 250
        assert trows[0]["context_window_used"] == 15_500

        srows = query_all(conn, "SELECT total_cost_usd FROM sessions WHERE session_id = 'sess-camel-db'")
        assert abs(float(srows[0]["total_cost_usd"]) - 0.42) < 1e-6

        qrows = query_all(conn, "SELECT remaining_fraction FROM quota_snapshots WHERE session_id = 'sess-camel-db'")
        assert len(qrows) == 1
        assert abs(float(qrows[0]["remaining_fraction"]) - 0.77) < 1e-6

        # Create mock conversations/<id>.db with gen_metadata
        conv_db = conv_dir / "sess-camel-db.db"
        c = sqlite3.connect(str(conv_db))
        try:
            c.execute("CREATE TABLE steps (step_index INTEGER, gen_metadata TEXT)")
            c.execute(
                "INSERT INTO steps VALUES (?, ?)",
                (
                    1,
                    json.dumps({
                        "usage": {"thinkingTokens": 4_500},
                        "toolsSchemaTokens": 19_000,
                        "ttftMs": 420,
                    }),
                ),
            )
            c.commit()
        finally:
            c.close()

        res = enrich_from_agy_dbs(conn, "sess-camel-db", agy_dir)
        assert res["conv_db_found"] is True
        assert res["gen_metadata_enriched"] == 1

        trow = query_all(conn, "SELECT thinking_tokens, tools_schema_tokens, ttft_ms FROM turns WHERE session_id = 'sess-camel-db'")[0]
        assert trow["thinking_tokens"] == 4_500
        assert trow["tools_schema_tokens"] == 19_000
        assert trow["ttft_ms"] == 420

        # W4 and W7 should now be unlocked
        waste_ids = {f["waste_id"] for f in detect_waste(conn, session_id="sess-camel-db")}
        assert 4 in waste_ids
        assert 7 in waste_ids
    finally:
        conn.close()


def test_idempotent_turn_upsert_and_recursive_rollup(tmp_path: Path) -> None:
    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="root-1", workspace_path="/repo")
        ensure_session(
            conn,
            session_id="child-1",
            parent_session_id="root-1",
            root_session_id="root-1",
            nesting_depth=1,
            workspace_path="/repo",
        )

        # First partial upsert, then final upsert on same (session_id, step_index)
        upsert_turn(
            conn,
            session_id="root-1",
            step_index=0,
            model_id="gemini-4-hydrogen",
            input_tokens=100,
            cache_read_tokens=900,
            output_tokens=50,
            estimated_cost_usd=0.001,
        )
        upsert_turn(
            conn,
            session_id="root-1",
            step_index=0,
            model_id="gemini-4-hydrogen",
            input_tokens=120,
            cache_read_tokens=900,
            output_tokens=80,
            estimated_cost_usd=0.002,
        )
        upsert_turn(
            conn,
            session_id="child-1",
            step_index=0,
            model_id="gemini-4-neon",
            input_tokens=200,
            output_tokens=100,
            estimated_cost_usd=0.005,
        )

        rows = query_all(conn, "SELECT * FROM turns WHERE session_id = 'root-1'")
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 120
        assert rows[0]["output_tokens"] == 80

        rollup = query_all(
            conn, "SELECT * FROM v_task_rollup_with_subagents WHERE root_session_id = 'root-1'"
        )
        assert len(rollup) == 1
        assert rollup[0]["session_count"] == 2
        assert rollup[0]["max_depth"] == 1
        assert rollup[0]["total_input_tokens"] == 320
        assert round(rollup[0]["total_estimated_cost_usd"], 4) == 0.007
    finally:
        conn.close()


def test_eight_waste_queries(tmp_path: Path) -> None:
    db_path = tmp_path / "ogy.db"
    conn = connect(db_path, init_schema=True)
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO tasks (task_id, repo_root, category, rollback_count) VALUES ('t1', '/repo', 'trivial_edit', 1)"
            )
        ensure_session(conn, session_id="s1", task_id="t1", workspace_path="/repo")
        with write_tx(conn):
            conn.execute(
                "UPDATE sessions SET total_cost_usd = 1.0, subagent_cost_usd = 2.0 WHERE session_id = 's1'"
            )

        # W1 (low cache hit on step > 0), W4 (over-effort high on trivial_edit), W5 (context >= 120k), W7 (mcp schema > 15k)
        upsert_turn(
            conn,
            session_id="s1",
            step_index=1,
            model_id="gemini-4-neon",
            task_id="t1",
            effort_level="high",
            input_tokens=50_000,
            cache_read_tokens=10_000,
            output_tokens=100,
            thinking_tokens=2_000,
            context_window_used=130_000,
            tools_schema_tokens=18_000,
            gap_since_prev_turn_s=45.0,
        )

        # W2 (retry loop >=3 attempts, >=2 errors)
        h_loop = canonical_args_hash({"CommandLine": "pytest"})
        for step in (1, 2, 3):
            upsert_tool_call(
                conn,
                session_id="s1",
                step_index=step,
                tool_name="run_command",
                args_hash=h_loop,
                is_error=1,
            )

        # W3 (obese tool output >= 32768 bytes)
        upsert_tool_call(
            conn,
            session_id="s1",
            step_index=4,
            tool_name="run_command",
            args_hash=canonical_args_hash({"CommandLine": "cat big.log"}),
            raw_output_bytes=40_000,
            est_output_tokens=10_000,
        )

        # W6 (duplicate file reads >= 2x without compaction)
        for step in (5, 6):
            upsert_tool_call(
                conn,
                session_id="s1",
                step_index=step,
                tool_name="view_file",
                target_path_or_cmd="src/app.py",
                args_hash=canonical_args_hash({"Path": "src/app.py", "step": step}),
            )

        findings = detect_waste(conn, session_id="s1")
        waste_ids = {f["waste_id"] for f in findings}
        assert waste_ids == {1, 2, 3, 4, 5, 6, 7, 8}
    finally:
        conn.close()
