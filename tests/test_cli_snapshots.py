"""Deterministic time-machine tests, inline-snapshot + dirty-equals, and syrupy CLI snapshots (Lot 3.5.4)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import time_machine
from dirty_equals import IsDict, IsFloat, IsPositiveInt, IsStr
from inline_snapshot import snapshot

from ogy_cli.init_audit import cmd_backup, cmd_init, cmd_restore
from ogy_cli.main import cmd_doctor
from ogy_cli.stats_render import query_filtered_stats, render_stats_report, render_waste_report
from ogy_core.config import OgyConfig, load_config
from ogy_core.context_health import assess_context_health, write_handoff
from ogy_core.firebreaks import evaluate_pre_tool_firebreaks, gc_stash_dir, stash_wrapped_output
from ogy_core.pricing import get_model_pricing
from ogy_core.profile import create_gemini_backup
from ogy_core.storage import (
    connect,
    consume_pending_task,
    detect_waste,
    ensure_session,
    upsert_turn,
    write_pending_task,
)
from ogy_hooks.cli import dispatch_hook
from ogy_mcp.server import ogy_session_budget
from ogy_statusline.render import render_statusline
from ogy_subagents.router import seconds_until_midnight_pt


def _normalize_cli_output(text: str, tmp_path: Path) -> str:
    """Scrub ephemeral tmp_path, hashes, timings, and platform path separators for deterministic syrupy snapshots."""
    out = text.replace(str(tmp_path), "<TMP>").replace(tmp_path.as_posix(), "<TMP>")
    out = out.replace("\\", "/")
    out = re.sub(r"[0-9a-f]{12,64}", "<HEX>", out)
    out = re.sub(r"\d+\.\d+ ms", "<MS> ms", out)
    return out


# ---------------------------------------------------------------------------
# 1. Deterministic Time Tests (`time-machine`, tick=False)
# ---------------------------------------------------------------------------


def test_time_machine_pricing_switches_2026_12_01_and_2027_01_01() -> None:
    with time_machine.travel("2026-11-30T23:59:59Z", tick=False):
        c_before = get_model_pricing("gemini-4-carbon")
        assert (c_before.input_per_m, c_before.output_per_m) == (2.00, 10.00)

    with time_machine.travel("2026-12-01T00:00:01Z", tick=False):
        c_after = get_model_pricing("gemini-4-carbon")
        assert (c_after.input_per_m, c_after.output_per_m) == (4.00, 20.00)

    with time_machine.travel("2026-12-31T23:59:59Z", tick=False):
        f_before = get_model_pricing("gemini-3.6-flash")
        assert (f_before.input_per_m, f_before.output_per_m) == (0.75, 3.75)

    with time_machine.travel("2027-01-01T00:00:01Z", tick=False):
        f_after = get_model_pricing("gemini-3.6-flash")
        assert (f_after.input_per_m, f_after.output_per_m) == (1.50, 7.50)


def test_time_machine_rpd_reset_midnight_pacific() -> None:
    pt = ZoneInfo("America/Los_Angeles")
    # 23:59:59 PT -> floor is 60s
    with time_machine.travel("2026-11-15T23:59:59-08:00", tick=False):
        assert seconds_until_midnight_pt() == 60.0

    # 00:00:01 PT -> ~24h (86399s) until next midnight PT
    with time_machine.travel("2026-11-16T00:00:01-08:00", tick=False):
        rem = seconds_until_midnight_pt()
        assert 86_390.0 <= rem <= 86_400.0
    assert pt.key == "America/Los_Angeles"


def test_time_machine_pending_tasks_ttl_30s(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    with time_machine.travel("2026-11-15T12:00:00Z", tick=False) as traveller:
        write_pending_task(
            tmp_path,
            pending_dir=pending_dir,
            task_id="task-ttl-1",
            decision={"stage": "stage1", "model": "gemini-4-neon"},
            ttl_seconds=30.0,
        )
        # 29s later -> still valid
        traveller.shift(29.0)
        got = consume_pending_task(tmp_path, pending_dir=pending_dir)
        assert got is not None and got["task_id"] == "task-ttl-1"

        # Write again and advance 31s -> expired
        write_pending_task(
            tmp_path,
            pending_dir=pending_dir,
            task_id="task-ttl-2",
            decision={"stage": "stage1", "model": "gemini-4-neon"},
            ttl_seconds=30.0,
        )
        traveller.shift(31.0)
        assert consume_pending_task(tmp_path, pending_dir=pending_dir) is None


def test_time_machine_statusline_debounce_10s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("OGY_ACTIVE", "1")
    cfg = load_config()
    cfg.state_dir = tmp_path / "sl-state"

    payload = {
        "conversationId": "sess-debounce",
        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 9000},
        "context_window": {"used": 10_000, "max": 200_000},
        "cost": {"total_usd": 0.05},
        "quota": {"gemini-5h": {"remaining_fraction": 0.9}},
    }

    with time_machine.travel("2026-11-15T12:00:00Z", tick=False) as traveller:
        render_statusline(payload, cfg=cfg, terminal_width=140, persist=True)
        conn = connect(cfg.db_path, init_schema=True)
        try:
            c1 = conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0]
        finally:
            conn.close()
        assert c1 == 1

        # 5s later (< 10s debounce) -> no new DB write
        traveller.shift(5.0)
        render_statusline(payload, cfg=cfg, terminal_width=140, persist=True)
        conn = connect(cfg.db_path, init_schema=True)
        try:
            c2 = conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0]
        finally:
            conn.close()
        assert c2 == 1

        # +6s (total 11s > 10s debounce) -> persists second snapshot
        traveller.shift(6.0)
        render_statusline(payload, cfg=cfg, terminal_width=140, persist=True)
        conn = connect(cfg.db_path, init_schema=True)
        try:
            c3 = conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0]
        finally:
            conn.close()
        assert c3 == 2


def test_time_machine_stash_lru_7_day_purge(tmp_path: Path) -> None:
    stash_dir = tmp_path / "stash"
    with time_machine.travel("2026-11-01T12:00:00Z", tick=False) as traveller:
        stashed = stash_wrapped_output(
            "old log line\n" * 10,
            "",
            cmd="pytest",
            exit_code=0,
            duration_s=0.1,
            stash_dir=stash_dir,
        )
        old_entry = stash_dir / stashed
        assert old_entry.is_dir()
        for p in [old_entry, *old_entry.rglob("*")]:
            os.utime(p, (1793534400.0, 1793534400.0))

        # 6 days later -> kept
        traveller.shift(6 * 86400)
        res_keep = gc_stash_dir(stash_dir, force=True)
        assert res_keep["removed_count"] == 0
        assert old_entry.is_dir()

        # +2 days (total 8 days > 7d) -> purged
        traveller.shift(2 * 86400)
        res_purge = gc_stash_dir(stash_dir, force=True)
        assert res_purge["removed_count"] == 1
        assert not old_entry.exists()


def test_time_machine_backup_deterministic_timestamp_and_dedup(tmp_path: Path) -> None:
    gemini_dir = tmp_path / ".gemini"
    (gemini_dir / "antigravity-cli").mkdir(parents=True)
    (gemini_dir / "antigravity-cli" / "settings.json").write_text('{"theme":"dark"}\n', encoding="utf-8")
    backups_dir = tmp_path / "backups"

    with time_machine.travel("2026-11-15T14:30:45Z", tick=False):
        b1 = create_gemini_backup(gemini_dir, backups_dir, profile_mode="shared", reason="manual")
        assert b1["created"] is True
        assert b1["snapshot_id"] == "20261115-143045"

        # Dedup when unchanged
        b2 = create_gemini_backup(gemini_dir, backups_dir, profile_mode="shared", reason="manual")
        assert b2["created"] is False
        assert b2["snapshot_id"] == "20261115-143045"

        # Force at same second -> deterministic -1 suffix
        b3 = create_gemini_backup(gemini_dir, backups_dir, profile_mode="shared", reason="manual", force=True)
        assert b3["created"] is True
        assert b3["snapshot_id"] == "20261115-143045-1"


def test_time_machine_cache_eviction_thresholds() -> None:
    h149 = assess_context_health(context_tokens=30_000, turn_count=5, idle_gap_s=149.0)
    h150 = assess_context_health(context_tokens=30_000, turn_count=5, idle_gap_s=150.0)
    h185 = assess_context_health(context_tokens=30_000, turn_count=5, idle_gap_s=185.0)
    h305 = assess_context_health(context_tokens=30_000, turn_count=5, idle_gap_s=305.0)
    assert h149.idle_warning is None
    assert h150.idle_warning is not None and "150s" in h150.idle_warning
    assert h185.idle_warning is not None
    assert h305.idle_warning is not None and "CACHE FROID" in h305.idle_warning


# ---------------------------------------------------------------------------
# 2. Compact Structural Snapshots (`inline-snapshot` + `dirty-equals`)
# ---------------------------------------------------------------------------


def test_inline_snapshots_hooks_v31_active_vs_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()

    # Regime A: OGY_ACTIVE absent -> pass-through
    monkeypatch.delenv("OGY_ACTIVE", raising=False)
    pt_pre = dispatch_hook(
        "pre-tool",
        {"conversationId": "s-pt", "toolCall": {"name": "read_file", "args": {"Path": "package-lock.json"}}},
        cfg=cfg,
    )
    pt_start = dispatch_hook("session-start", {"conversationId": "s-pt", "workspacePaths": [str(tmp_path)]}, cfg=cfg)
    pt_stop = dispatch_hook("stop", {"conversationId": "s-pt"}, cfg=cfg)
    assert pt_pre == snapshot({"decision": "allow"})
    assert pt_start == snapshot({})
    assert pt_stop == snapshot({})

    # Regime B: OGY_ACTIVE="1" -> active firebreak on lockfile
    monkeypatch.setenv("OGY_ACTIVE", "1")
    act_pre = dispatch_hook(
        "pre-tool",
        {"conversationId": "s-act", "toolCall": {"name": "read_file", "args": {"Path": "package-lock.json"}}},
        cfg=cfg,
    )
    assert act_pre == {
        "decision": "deny",
        "reason": IsStr(regex=r".*lockfile.*package-lock\.json.*"),
    }


def test_inline_snapshot_mcp_session_budget_payload(tmp_path: Path) -> None:
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="snap-mcp-1", workspace_path=str(tmp_path))
        upsert_turn(
            conn,
            session_id="snap-mcp-1",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=2000,
            cache_read_tokens=18000,
            output_tokens=250,
            context_window_used=20000,
            estimated_cost_usd=0.04,
        )
    finally:
        conn.close()

    raw = json.loads(ogy_session_budget("snap-mcp-1"))
    assert raw == IsDict(
        session_id="snap-mcp-1",
        active_context_tokens=20000,
        warm_cache_hit_pct=IsFloat(ge=89.0, le=91.0),
        idle_gap_s=0.0,
        zone="green",
        advice="keep_warm",
        reason=IsStr(),
        top_waste=[],
    )


# ---------------------------------------------------------------------------
# 3. Voluminous CLI / HUD / V3.1 Output Snapshots (`syrupy`)
# ---------------------------------------------------------------------------


def test_syrupy_stats_and_waste_reports(tmp_path: Path, snapshot: Any) -> None:
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="s-rep-1", workspace_path=str(tmp_path))
        upsert_turn(
            conn,
            session_id="s-rep-1",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=5000,
            cache_read_tokens=45000,
            output_tokens=600,
            thinking_tokens=300,
            context_window_used=50000,
            estimated_cost_usd=0.08,
        )
        sessions_data = query_filtered_stats(conn)
        stats_out = render_stats_report(conn, sessions_data)
        waste_out = render_waste_report(detect_waste(conn))
    finally:
        conn.close()

    assert _normalize_cli_output(stats_out, tmp_path) == snapshot(name="ogy_stats")
    assert _normalize_cli_output(waste_out, tmp_path) == snapshot(name="ogy_waste")


def test_syrupy_statusline_ansi_and_no_color(monkeypatch: pytest.MonkeyPatch, snapshot: Any) -> None:
    monkeypatch.setenv("OGY_ACTIVE", "1")
    payload = {
        "conversationId": "sess-sl-snap",
        "modelName": "gemini-4-hydrogen",
        "usage": {"input_tokens": 5_000, "cache_read_input_tokens": 59_000, "output_tokens": 300},
        "context_window": {"used": 64_000, "max": 500_000},
        "cost": {"total_usd": 0.24},
        "quota": {
            "gemini-5h": {"remaining_fraction": 0.88},
            "gemini-weekly": {"remaining_fraction": 0.72},
        },
    }

    monkeypatch.setenv("NO_COLOR", "1")
    wide_plain = render_statusline(payload, terminal_width=140, persist=False)
    narrow_plain = render_statusline(payload, terminal_width=90, persist=False)

    monkeypatch.delenv("NO_COLOR", raising=False)
    wide_ansi = render_statusline(payload, terminal_width=140, persist=False)

    assert wide_plain == snapshot(name="statusline_wide_no_color")
    assert narrow_plain == snapshot(name="statusline_narrow_no_color")
    assert wide_ansi == snapshot(name="statusline_wide_ansi")


def test_syrupy_firebreak_refusal_and_handoff(tmp_path: Path, snapshot: Any) -> None:
    big_file = tmp_path / "big_module.py"
    big_file.write_text("\n".join(f"x_{i} = {i}" for i in range(2500)) + "\n", encoding="utf-8")

    cfg = load_config()
    verdict = evaluate_pre_tool_firebreaks(
        None,
        session_id="snap-fb-1",
        tool_name="read_file",
        tool_args={"Path": str(big_file)},
        cfg=cfg,
    )
    assert verdict["decision"] == "deny"
    assert _normalize_cli_output(verdict["reason"], tmp_path) == snapshot(name="firebreak_large_file")

    res = write_handoff(
        goal="Complete V3.5 test pyramid",
        modified_files=["tests/test_cli_snapshots.py"],
        checks="pytest -q green",
        negative_constraints=["Never touch host ~/.gemini"],
        next_step="Run cross-OS CI matrix",
        workspace_path=tmp_path,
    )
    assert res["lines"] <= 30
    handoff_text = Path(res["path"]).read_text(encoding="utf-8")
    assert handoff_text == snapshot(name="handoff_md")


def test_syrupy_v31_backup_restore_and_doctor_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot: Any,
) -> None:
    fake_home = tmp_path / "_ogy_test_home"
    shared_gemini = fake_home / ".gemini"
    (shared_gemini / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    (shared_gemini / "antigravity-cli" / "settings.json").write_text('{"theme":"dark"}\n', encoding="utf-8")
    (shared_gemini / "oauth_creds.json").write_text('{"token":"fake"}\n', encoding="utf-8")

    with time_machine.travel("2026-11-15T15:00:00Z", tick=False):
        # 1. Shared mode init + doctor + backup --list + restore --dry-run
        assert cmd_init(profile_mode="shared") == 0
        capsys.readouterr()

        assert cmd_doctor() == 0
        doc_shared = _normalize_cli_output(capsys.readouterr().out, tmp_path)

        assert cmd_backup(list_only=True) == 0
        bak_list = _normalize_cli_output(capsys.readouterr().out, tmp_path)

        assert cmd_restore(latest=True, dry_run=True) == 0
        rest_dry = _normalize_cli_output(capsys.readouterr().out, tmp_path)

        # 2. Isolated mode init + doctor
        assert cmd_init(profile_mode="isolated") == 0
        capsys.readouterr()

        assert cmd_doctor() == 0
        doc_iso = _normalize_cli_output(capsys.readouterr().out, tmp_path)

    assert doc_shared == snapshot(name="doctor_shared")
    assert bak_list == snapshot(name="backup_list")
    assert rest_dry == snapshot(name="restore_dry_run")
    assert doc_iso == snapshot(name="doctor_isolated")
