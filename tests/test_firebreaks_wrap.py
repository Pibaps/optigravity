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


def _make_cfg(tmp_path: Path, *, overwrite_ok: bool = False) -> OgyConfig:
    return OgyConfig(
        config_path=tmp_path / "config.toml",
        db_path=tmp_path / "ogy.db",
        agy_cli_dir=tmp_path / "agy-cli",
        agy_config_dir=tmp_path / "agy-cfg",
        stash_dir=tmp_path / "stash",
        state_dir=tmp_path / "state",
        overwrite_verified_ok=overwrite_ok,
    )


def test_firebreak_anti_loop(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="s1", workspace_path=str(tmp_path))
        args = {"CommandLine": "python -m unittest"}
        h = canonical_args_hash(args)
        for step in (1, 2, 3):
            upsert_tool_call(
                conn, session_id="s1", step_index=step, tool_name="run_command", args_hash=h, is_error=1
            )
        res = evaluate_pre_tool_firebreaks(
            conn, session_id="s1", tool_name="run_command", tool_args=args, cfg=cfg
        )
        assert res["decision"] == "deny"
        assert "Boucle" in res["reason"]
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

    # Large file with StartLine/EndLine allowed (nominal decision "")
    res_bounded = evaluate_pre_tool_firebreaks(
        None,
        session_id="s1",
        tool_name="view_file",
        tool_args={"Path": str(big_file), "StartLine": 1, "EndLine": 50},
        cfg=cfg,
    )
    assert res_bounded == {"decision": ""}


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
    assert res2 == {"decision": "", "overwrite": {"CommandLine": "ogy wrap -- pytest -v"}}

    # Excluded mutating commands must pass untouched
    for excluded in ("git commit -m 'msg'", "git push origin main", "rm -rf build"):
        res_ex = evaluate_pre_tool_firebreaks(
            None, session_id="s1", tool_name="run_command", tool_args={"CommandLine": excluded}, cfg=cfg_ow
        )
        assert res_ex == {"decision": ""}


def test_wrap_tee_on_failure_and_recall(tmp_path: Path, capsys) -> None:
    stash = tmp_path / "stash"
    script = "import sys; print('\\n'.join(f'line {i}' for i in range(40))); print('FAILED test_x'); sys.exit(1)"
    rc = run_wrapped_command([sys.executable, "-c", script], stash_dir=stash)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED test_x" in out
    assert "recall=" in out

    logs = list(stash.glob("*.log"))
    assert len(logs) == 1
    digest_prefix = logs[0].stem[:12]
    recalled = recall_stashed_output(digest_prefix, lines_spec="1:3", stash_dir=stash)
    assert recalled.splitlines() == ["line 0", "line 1", "line 2"]
