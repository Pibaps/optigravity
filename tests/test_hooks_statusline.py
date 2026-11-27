"""Tests for fail-open hooks, statusline renderer, and stdlib-only hot path."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from ogy_core.config import OgyConfig
from ogy_hooks.cli import dispatch_hook, main as hooks_main
from ogy_statusline.render import render_statusline


def _cfg(tmp_path: Path, *, cap_usd: float | None = None) -> OgyConfig:
    return OgyConfig(
        config_path=tmp_path / "config.toml",
        db_path=tmp_path / "ogy.db",
        agy_cli_dir=tmp_path / "agy-cli",
        agy_config_dir=tmp_path / "agy-cfg",
        stash_dir=tmp_path / "stash",
        state_dir=tmp_path / "state",
        budget_hard_cap_usd=cap_usd,
    )


def test_hot_path_stdlib_only() -> None:
    # Verify importing ogy_core, ogy_hooks.cli, ogy_statusline.render, and ogy_cli.main
    # in a fresh Python process does not load third-party dependencies.
    code = (
        "import sys; import ogy_core.storage, ogy_core.firebreaks, ogy_core.ingest, "
        "ogy_hooks.cli, ogy_statusline.render, ogy_cli.main; "
        "forbidden = {'mcp', 'pydantic', 'rich', 'numpy', 'textual', 'acp', 'google'} & set(sys.modules); "
        "assert not forbidden, f'Forbidden modules imported: {forbidden}'"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_hook_p95_latency_and_pending_task_correlation(tmp_path: Path, monkeypatch) -> None:
    import time

    from ogy_core.storage import connect, query_all, write_pending_task

    monkeypatch.setenv("OGY_ACTIVE", "1")
    cfg = _cfg(tmp_path)
    # Warm DB schema once (steady state)
    conn = connect(cfg.db_path, init_schema=True)
    conn.close()

    # Measure p95 latency across 20 pre-tool dispatches
    latencies: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        resp = dispatch_hook(
            "pre-tool",
            {"conversationId": "s-lat", "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}}},
            cfg=cfg,
        )
        latencies.append((time.perf_counter() - t0) * 1000.0)
        assert resp == {"decision": "allow"}
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    assert p95 < 20.0, f"Expected p95 < 20ms, got {p95:.2f}ms"

    # Pending task correlation: ogy run -> session-start -> routing_decisions
    conn = connect(cfg.db_path, init_schema=True)
    try:
        conn.execute("INSERT INTO tasks (task_id, repo_root, title, category) VALUES ('task-abc', ?, 'fix bug', 'bugfix_local')", (str(tmp_path),))
    finally:
        conn.close()

    write_pending_task(
        tmp_path,
        pending_dir=cfg.stash_dir.parent / "pending_tasks",
        task_id="task-abc",
        decision={
            "stage": "stage2_bandit",
            "category": "bugfix_local",
            "model": "gemini-4-neon",
            "effort": "low",
            "mode": "auto",
            "features": {"has_stacktrace": True},
        },
        ttl_seconds=30.0,
    )
    dispatch_hook(
        "session-start",
        {"conversationId": "sess-corr", "workspacePaths": [str(tmp_path)], "modelName": "gemini-4-neon"},
        cfg=cfg,
    )
    conn = connect(cfg.db_path, init_schema=True)
    try:
        srow = query_all(conn, "SELECT task_id FROM sessions WHERE session_id = 'sess-corr'")[0]
        assert srow["task_id"] == "task-abc"
        rd = query_all(conn, "SELECT * FROM routing_decisions WHERE session_id = 'sess-corr'")
        assert len(rd) == 1
        assert rd[0]["task_id"] == "task-abc"
        assert rd[0]["stage"] == "stage2_bandit"
    finally:
        conn.close()

    # Second session-start must NOT re-consume the already consumed pending task
    dispatch_hook(
        "session-start",
        {"conversationId": "sess-corr-2", "workspacePaths": [str(tmp_path)]},
        cfg=cfg,
    )
    conn = connect(cfg.db_path, init_schema=True)
    try:
        rd2 = query_all(conn, "SELECT * FROM routing_decisions WHERE session_id = 'sess-corr-2'")
        assert len(rd2) == 0
    finally:
        conn.close()


def test_hooks_fail_open_on_bad_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid json"))
    rc = hooks_main(["pre-tool"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"decision": "allow"}

    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid json"))
    rc2 = hooks_main(["post-tool"])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out.strip())
    assert out2 == {}


def test_session_start_handoff_and_stop_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OGY_ACTIVE", "1")
    cfg = _cfg(tmp_path)
    handoff = tmp_path / ".ogy" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Handoff\nNext: run tests", encoding="utf-8")

    r_start = dispatch_hook(
        "session-start",
        {"conversationId": "sess-1", "workspacePaths": [str(tmp_path)], "modelName": "gemini-4-neon"},
        cfg=cfg,
    )
    assert "injectSteps" in r_start
    assert not handoff.exists()
    assert (tmp_path / ".ogy" / "handoff.archived.md").is_file()

    # PreInvocation > 1 must always be {}
    assert dispatch_hook("pre-invocation", {"conversationId": "sess-1", "invocationNum": 2}, cfg=cfg) == {}

    # PostToolUse must always be {}
    assert (
        dispatch_hook(
            "post-tool",
            {
                "conversationId": "sess-1",
                "stepIdx": 1,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}},
            },
            cfg=cfg,
        )
        == {}
    )

    r_stop = dispatch_hook("stop", {"conversationId": "sess-1"}, cfg=cfg)
    assert r_stop == {"decision": "stop"}


def test_statusline_render_wide_and_narrow(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("OGY_ACTIVE", "1")
    payload = {
        "conversationId": "sess-sl",
        "modelName": "gemini-4-hydrogen",
        "usage": {"input_tokens": 5_000, "cache_read_input_tokens": 59_000, "output_tokens": 300},
        "context_window": {"used": 64_000, "max": 500_000},
        "cost": {"total_usd": 0.24},
        "quota": {
            "gemini-5h": {"remaining_fraction": 0.88},
            "gemini-weekly": {"remaining_fraction": 0.72},
        },
    }
    wide = render_statusline(payload, terminal_width=140, persist=False)
    assert "ogy │ cache 92% │ ctx █░░░░░ 64K (13%) │ $0.24 │ 5h ████░ 88% │ wk ████░ 72% │ ▲ WARN" == wide

    narrow = render_statusline(payload, terminal_width=90, persist=False)
    assert narrow == "ogy │ c:92% │ █░░░ 64K │ $0.24 │ 5h:88% │ wk:72% │ ▲ WARN"


def test_statusline_camelcase_and_db_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("OGY_ACTIVE", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

    # 1. camelCase payload keys
    camel = {
        "conversationId": "sess-camel",
        "usage": {"inputTokens": 2_000, "cacheReadInputTokens": 8_000},
        "contextWindow": {"usedTokens": 10_000, "maxTokens": 200_000},
        "totalCostUsd": 0.12,
    }
    line = render_statusline(camel, terminal_width=140, persist=False)
    assert "ogy │ cache 80% │ ctx ░░░░░░ 10K (5%) │ $0.12 │ ● OK" == line

    # 2. DB fallback when statusLine payload omits usage/cost
    from ogy_core.storage import connect, ensure_session, upsert_turn

    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="sess-fb", workspace_path=str(tmp_path))
        upsert_turn(
            conn,
            session_id="sess-fb",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=4_000,
            cache_read_tokens=16_000,
            output_tokens=200,
            context_window_used=20_000,
            estimated_cost_usd=0.05,
        )
    finally:
        conn.close()

    fb_line = render_statusline(
        {"conversationId": "sess-fb", "quota": {"gemini-5h": {"remaining_fraction": 0.6}}},
        terminal_width=140,
        persist=True,
    )
    assert fb_line == "ogy │ cache 80% │ ctx █░░░░░ 20K (10%) │ t1 · $0.05 │ 5h ███░░ 60% │ ● OK"


def test_v31_ogy_active_guard_and_opt_out(monkeypatch, tmp_path: Path, capsys) -> None:
    """Lot 3.1.1: without OGY_ACTIVE=1 and with isolate_hooks=True, hooks & statusline short-circuit with 0 DB writes."""
    monkeypatch.delenv("OGY_ACTIVE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = _cfg(tmp_path)
    assert cfg.isolate_hooks is True

    handoff = tmp_path / ".ogy" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Keep untouched when OGY_ACTIVE is absent", encoding="utf-8")

    # 1. PreToolUse returns {"decision": "allow"} even on a lockfile/verbose command, with 0 DB writes
    assert (
        dispatch_hook(
            "pre-tool",
            {"conversationId": "native-1", "toolCall": {"name": "view_file", "args": {"Path": str(tmp_path / "uv.lock")}}},
            cfg=cfg,
        )
        == {"decision": "allow"}
    )
    # 2. All other hooks return {} with 0 DB writes and do not touch handoff.md
    assert dispatch_hook("session-start", {"conversationId": "native-1", "workspacePaths": [str(tmp_path)]}, cfg=cfg) == {}
    assert dispatch_hook("pre-invocation", {"conversationId": "native-1", "invocationNum": 1}, cfg=cfg) == {}
    assert dispatch_hook("post-tool", {"conversationId": "native-1", "stepIdx": 1, "toolCall": {"name": "run_command", "args": {}}}, cfg=cfg) == {}
    assert dispatch_hook("post-invocation", {"conversationId": "native-1"}, cfg=cfg) == {}
    assert dispatch_hook("stop", {"conversationId": "native-1"}, cfg=cfg) == {}
    assert handoff.is_file()
    assert not cfg.db_path.exists()

    # 3. Statusline returns empty string and 0 DB writes
    sl_payload = {
        "conversationId": "native-sl",
        "usage": {"input_tokens": 2_000, "cache_read_input_tokens": 8_000},
        "context_window": {"used": 10_000, "max": 200_000},
        "cost": {"total_usd": 0.12},
    }
    assert render_statusline(sl_payload, terminal_width=140, persist=True, cfg=cfg) == ""
    assert not cfg.db_path.exists()

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(sl_payload)))
    assert hooks_main(["statusline"]) == 0
    assert capsys.readouterr().out == ""

    # 4. Explicit opt-out (isolate_hooks=False) re-enables hooks & statusline without OGY_ACTIVE
    cfg_optout = _cfg(tmp_path)
    cfg_optout.isolate_hooks = False
    res_lock = dispatch_hook(
        "pre-tool",
        {"conversationId": "native-2", "toolCall": {"name": "view_file", "args": {"Path": str(tmp_path / "uv.lock")}}},
        cfg=cfg_optout,
    )
    assert res_lock["decision"] == "deny"
    assert "ogy" in render_statusline(sl_payload, terminal_width=140, persist=False, cfg=cfg_optout)
