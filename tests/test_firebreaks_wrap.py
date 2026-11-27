"""Tests for firebreaks, ogy wrap compression, tee-on-failure, and exclusions."""

from __future__ import annotations

import sys
from pathlib import Path

from ogy_core.config import OgyConfig
from ogy_core.firebreaks import (
    evaluate_pre_tool_firebreaks,
    recall_stashed_output,
    run_wrapped_command,
)
from ogy_core.storage import canonical_args_hash, connect, ensure_session, upsert_tool_call


def _make_cfg(tmp_path: Path, *, overwrite_ok: bool = False, language: str = "en") -> OgyConfig:
    return OgyConfig(
        config_path=tmp_path / "config.toml",
        db_path=tmp_path / "ogy.db",
        agy_cli_dir=tmp_path / "agy-cli",
        agy_config_dir=tmp_path / "agy-cfg",
        stash_dir=tmp_path / "stash",
        state_dir=tmp_path / "state",
        overwrite_verified_ok=overwrite_ok,
        language=language,
    )


def test_firebreak_anti_loop(tmp_path: Path) -> None:
    cfg_en = _make_cfg(tmp_path, language="en")
    cfg_fr = _make_cfg(tmp_path, language="fr")
    conn = connect(cfg_en.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="s1", workspace_path=str(tmp_path))
        args = {"CommandLine": "python -m unittest"}
        h = canonical_args_hash(args)
        for step in (1, 2, 3):
            upsert_tool_call(
                conn, session_id="s1", step_index=step, tool_name="run_command", args_hash=h, is_error=1
            )
        res_en = evaluate_pre_tool_firebreaks(
            conn, session_id="s1", tool_name="run_command", tool_args=args, cfg=cfg_en
        )
        assert res_en["decision"] == "deny"
        assert "loop" in res_en["reason"].lower()

        res_fr = evaluate_pre_tool_firebreaks(
            conn, session_id="s1", tool_name="run_command", tool_args=args, cfg=cfg_fr
        )
        assert res_fr["decision"] == "deny"
        assert "Boucle" in res_fr["reason"]
    finally:
        conn.close()


def test_firebreak_obese_read_and_lockfile(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    big_file = tmp_path / "big.py"
    big_file.write_text("x = 1\n" * 4000, encoding="utf-8")

    # Lockfile blocked
    res_lock = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="view_file", tool_args={"Path": str(tmp_path / "uv.lock")}, cfg=cfg
    )
    assert res_lock["decision"] == "deny"

    # Large file without range blocked
    res_big = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="view_file", tool_args={"Path": str(big_file)}, cfg=cfg
    )
    assert res_big["decision"] == "deny"

    # Large file with StartLine/EndLine allowed (nominal decision "allow")
    res_bounded = evaluate_pre_tool_firebreaks(
        None,
        session_id="s1",
        tool_name="view_file",
        tool_args={"Path": str(big_file), "StartLine": 1, "EndLine": 50},
        cfg=cfg,
    )
    assert res_bounded == {"decision": "allow"}


def test_firebreak_wrap_and_exclusions(tmp_path: Path) -> None:
    cfg_deny = _make_cfg(tmp_path, overwrite_ok=False)
    cfg_ow = _make_cfg(tmp_path, overwrite_ok=True)

    res1 = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": "pytest -v"}, cfg=cfg_deny
    )
    assert res1["decision"] == "deny"
    assert "ogy wrap -- pytest -v" in res1["reason"]

    res2 = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": "pytest -v"}, cfg=cfg_ow
    )
    assert res2 == {"decision": "allow", "overwrite": {"CommandLine": "ogy wrap -- pytest -v"}}

    # Excluded mutating commands must pass untouched
    for excluded in ("git commit -m 'msg'", "git push origin main", "rm -rf build"):
        res_ex = evaluate_pre_tool_firebreaks(
            None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": excluded}, cfg=cfg_ow
        )
        assert res_ex == {"decision": "allow"}


def test_wrap_tee_on_failure_and_recall(tmp_path: Path, capsys) -> None:
    stash = tmp_path / "stash"
    script = (
        "import sys; "
        "print('\\n'.join(f'line {i}' for i in range(45))); "
        "print('FAILED test_x'); "
        "print('STDERR boom', file=sys.stderr); "
        "sys.exit(1)"
    )
    rc = run_wrapped_command([sys.executable, "-c", script], stash_dir=stash)
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAILED test_x" in captured.out
    assert "recall=" in captured.out
    # stderr must remain separated and intact, with 0 ANSI spinner in non-TTY
    assert "STDERR boom" in captured.err
    assert "\x1b[" not in captured.err

    dirs = [p for p in stash.iterdir() if p.is_dir()]
    assert len(dirs) == 1
    assert (dirs[0] / "stdout.log").is_file()
    assert (dirs[0] / "stderr.log").is_file()
    assert (dirs[0] / "meta.json").is_file()

    digest_prefix = dirs[0].name[:12]
    recalled = recall_stashed_output(digest_prefix, lines_spec="1:3", stash_dir=stash)
    assert recalled.splitlines() == ["line 0", "line 1", "line 2"]
    recalled_err = recall_stashed_output(digest_prefix, stream="stderr", stash_dir=stash)
    assert "STDERR boom" in recalled_err


def test_lot12_zero_false_positives_and_surgical_rewrite(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_cfg(tmp_path, overwrite_ok=True)

    # 1. Adaptive file thresholds: README.md (18 KB <= 48 KB) allowed; big .log (10 KB > 8 KB) denied
    readme = tmp_path / "README.md"
    readme.write_text("# Doc\n" * 3000, encoding="utf-8")  # ~18 KB
    assert (
        evaluate_pre_tool_firebreaks(None, session_id="s1", tool_name="read_file", tool_args={"Path": str(readme)}, cfg=cfg)
        == {"decision": "allow"}
    )

    big_log = tmp_path / "server.log"
    big_log.write_text("INFO line\n" * 1000, encoding="utf-8")  # ~10 KB > 8 KB
    res_log = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="read_file", tool_args={"Path": str(big_log)}, cfg=cfg
    )
    assert res_log["decision"] == "deny"
    assert "1000" in res_log["reason"]  # exact line count
    assert "StartLine/EndLine" in res_log["reason"]
    assert "grep_search" not in res_log["reason"]

    # 2. Bounded-aware commands pass untouched
    for bounded in (
        "uv run pytest -q --tb=short",
        "pytest -k test_foo",
        "git log -n 10 --oneline",
        "git diff --stat",
        "git status -s",
        "git status --porcelain",
        "ogy raw -- pytest -v",
        "OGY_NO_WRAP=1 pytest -v",
    ):
        assert (
            evaluate_pre_tool_firebreaks(
                None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": bounded}, cfg=cfg
            )
            == {"decision": "allow"}
        ), f"False positive on: {bounded}"

    # 3. Advanced syntax & pipelines pass untouched
    for advanced in (
        "pytest | head -n 20",
        "git status | Select-Object -First 5",
        "pytest > out.log",
        "pytest |& tee out.log",
        "echo $(git status)",
        "Get-ChildItem | ForEach-Object { $_.Name }",
    ):
        assert (
            evaluate_pre_tool_firebreaks(
                None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": advanced}, cfg=cfg
            )
            == {"decision": "allow"}
        ), f"False positive on advanced syntax: {advanced}"

    # 4. Surgical compound rewrite (only unbounded verbose segment rewritten)
    res_and = evaluate_pre_tool_firebreaks(
        None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": "pytest && ruff check"}, cfg=cfg
    )
    assert res_and == {"decision": "allow", "overwrite": {"CommandLine": "ogy wrap -- pytest && ruff check"}}

    res_semi = evaluate_pre_tool_firebreaks(
        None,
        session_id="s1",
        tool_name="run_command",
        tool_args={"CommandLine": "git status -s; pytest -v"},
        cfg=cfg,
    )
    assert res_semi == {"decision": "allow", "overwrite": {"CommandLine": "git status -s; ogy wrap -- pytest -v"}}

    # 5. OGY_NO_WRAP=1 env escape hatch
    monkeypatch.setenv("OGY_NO_WRAP", "1")
    assert (
        evaluate_pre_tool_firebreaks(
            None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": "pytest -v"}, cfg=cfg
        )
        == {"decision": "allow"}
    )


def test_lot13_small_passthrough_legacy_recall_and_lru_gc(tmp_path: Path, capsys) -> None:
    import os
    import time

    from ogy_core.firebreaks import gc_stash_dir, stash_raw_output

    stash = tmp_path / "stash"

    # 1. Small output (<40 lines and <3KB) -> pass-through, no stash created
    rc = run_wrapped_command([sys.executable, "-c", "print('hello'); print('world')"], stash_dir=stash)
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["hello", "world"]
    assert "[ogy wrap]" not in out
    assert not stash.exists()

    # 2. Legacy flat .log file remains readable by recall_stashed_output
    digest = stash_raw_output("alpha\nbeta\ngamma\n", stash_dir=stash)
    assert recall_stashed_output(digest[:10], lines_spec="2:3", stash_dir=stash) == "beta\ngamma"

    # 3. LRU GC: remove expired (>7d) and evict oldest when over max_total_bytes
    old_dir = stash / ("a" * 64)
    old_dir.mkdir(parents=True)
    (old_dir / "stdout.log").write_text("X" * 1024, encoding="utf-8")
    old_ts = time.time() - (9 * 86400)
    os.utime(old_dir, (old_ts, old_ts))

    mid_dir = stash / ("b" * 64)
    mid_dir.mkdir(parents=True)
    (mid_dir / "stdout.log").write_text("Y" * 2048, encoding="utf-8")
    mid_ts = time.time() - 600
    os.utime(mid_dir, (mid_ts, mid_ts))

    new_dir = stash / ("c" * 64)
    new_dir.mkdir(parents=True)
    (new_dir / "stdout.log").write_text("Z" * 2048, encoding="utf-8")

    # Force GC with 3000-byte cap: old_dir purged by age, mid_dir evicted by LRU, new_dir kept
    stats = gc_stash_dir(stash, max_age_days=7.0, max_total_bytes=3000, force=True)
    assert stats["removed_count"] >= 2
    assert not old_dir.exists()
    assert not mid_dir.exists()
    assert new_dir.exists()
    assert (stash / ".last_gc").is_file()

    # Subsequent non-forced GC within 1h is skipped
    skipped = gc_stash_dir(stash, max_age_days=0.0, max_total_bytes=0, force=False)
    assert skipped["removed_count"] == 0


def test_v31_pre_tool_hook_respects_ogy_active_for_firebreaks(tmp_path: Path, monkeypatch) -> None:
    """Lot 3.1.1: PreToolUse firebreaks only trigger when OGY_ACTIVE=1 (with default isolate_hooks=True)."""
    from ogy_hooks.cli import dispatch_hook

    cfg = _make_cfg(tmp_path, overwrite_ok=True)
    payload = {
        "conversationId": "sess-fb-guard",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -v"}},
    }

    # Without OGY_ACTIVE -> immediate pass-through, no overwrite, no DB write
    monkeypatch.delenv("OGY_ACTIVE", raising=False)
    res_inactive = dispatch_hook("pre-tool", payload, cfg=cfg)
    assert res_inactive == {"decision": "allow"}
    assert not cfg.db_path.exists()

    # With OGY_ACTIVE=1 -> firebreak rewrites command and records hook_event
    monkeypatch.setenv("OGY_ACTIVE", "1")
    res_active = dispatch_hook("pre-tool", payload, cfg=cfg)
    assert res_active == {"decision": "allow", "overwrite": {"CommandLine": "ogy wrap -- pytest -v"}}
    assert cfg.db_path.is_file()
