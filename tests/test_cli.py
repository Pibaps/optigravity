"""CLI tests for ogy init, audit mcp, stats, waste, run --dry-run, and transcript raw_output_bytes ingestion."""

from __future__ import annotations

import json
from pathlib import Path

from ogy_cli.main import main as ogy_main
from ogy_core.ingest import ingest_transcript_incremental
from ogy_core.storage import connect, detect_waste, ensure_session


def _patch_env_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "ogy.db"
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    cfg_dir = tmp_path / ".gemini" / "config"
    cli_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))
    return db_path, cli_dir, cfg_dir


def test_cli_init_reversible_and_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    _, cli_dir, cfg_dir = _patch_env_paths(monkeypatch, tmp_path)
    orig_settings = {"theme": "dark"}
    (cli_dir / "settings.json").write_text(json.dumps(orig_settings), encoding="utf-8")

    # Dry run creates no backup or hooks
    assert ogy_main(["init", "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert not (cfg_dir / "hooks.json").exists()

    # Real init creates backups and installs configs
    assert ogy_main(["init", "--enable-firebreaks"]) == 0
    assert (cli_dir / "settings.json.ogy.bak").is_file()
    settings = json.loads((cli_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert settings["statusLine"]["command"] == "ogy-hook statusline"

    hooks = json.loads((cfg_dir / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["ogy-firebreaks"]["enabled"] is True
    assert (cfg_dir / "rules" / "ogy-hygiene.md").is_file()
    assert (cfg_dir / "skills" / "ogy-budget" / "SKILL.md").is_file()


def test_cli_audit_mcp_json(monkeypatch, tmp_path: Path, capsys) -> None:
    _, _, cfg_dir = _patch_env_paths(monkeypatch, tmp_path)
    mcp_cfg = {
        "mcpServers": {
            "noisy_srv": {
                "command": "node",
                "eager": True,
                "tools": [
                    {"name": "ui_panel", "_meta": {"ui": {"visibility": ["app"]}}},
                    {"name": "query_db"},
                ],
            }
        }
    }
    (cfg_dir / "mcp_config.json").write_text(json.dumps(mcp_cfg), encoding="utf-8")

    assert ogy_main(["audit", "mcp", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) >= 1
    rep = next(iter(out.values()))
    assert any(f["server"] == "noisy_srv" for f in rep["findings"])
    assert rep["recommended_profile"]["noisy_srv"]["enabledTools"] == ["query_db"]


def test_cli_run_dry_run_skips_plan_in_headless(monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_env_paths(monkeypatch, tmp_path)
    # "explain architecture" normally routes to plan mode, which must be skipped in -p headless mode
    assert ogy_main(["run", "--dry-run", "-p", "explain architecture and security"]) == 0
    out = capsys.readouterr().out
    assert "--mode plan" not in out


def test_cli_stats_waste_and_raw_output_bytes_ingestion(monkeypatch, tmp_path: Path, capsys) -> None:
    db_path, _, _ = _patch_env_paths(monkeypatch, tmp_path)
    transcript = tmp_path / "transcript_full.jsonl"
    big_output = "X" * 40000
    transcript.write_text(
        json.dumps({
            "stepIdx": 1,
            "modelName": "gemini-4-hydrogen",
            "usage": {"input_tokens": 5000, "cache_read_input_tokens": 0, "output_tokens": 100},
            "toolCalls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "pytest -vv"},
                    "output": big_output,
                    "exitCode": 0,
                }
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="sess-cli-1", workspace_path=str(tmp_path), transcript_path=str(transcript))
        ingest_transcript_incremental(conn, session_id="sess-cli-1", transcript_path=transcript)
        findings = detect_waste(conn, session_id="sess-cli-1")
        assert any(f["waste_id"] == 3 and f["kind"] == "obese_tool_output" for f in findings)
    finally:
        conn.close()

    assert ogy_main(["stats", "--session", "sess-cli-1", "--json"]) == 0
    stats_out = json.loads(capsys.readouterr().out)
    assert stats_out[0]["total_input_tokens"] == 5000

    assert ogy_main(["waste", "--session", "sess-cli-1", "--json"]) == 0
    waste_out = json.loads(capsys.readouterr().out)
    assert any(w["kind"] == "obese_tool_output" for w in waste_out)

    # Human-readable ROI stats & waste tables (NO_COLOR safe)
    monkeypatch.setenv("NO_COLOR", "1")
    assert ogy_main(["stats", "--last", "5", "--repo", tmp_path.name]) == 0
    txt_stats = capsys.readouterr().out
    assert "Sessions agy (OAuth)" in txt_stats
    assert "Subagents MCP (API USD)" in txt_stats

    assert ogy_main(["waste", "--since", "7d"]) == 0
    txt_waste = capsys.readouterr().out
    assert "ogy wrap" in txt_waste


def test_stale_quota_does_not_downgrade_ogy_run(monkeypatch, tmp_path: Path, capsys) -> None:
    db_path, _, _ = _patch_env_paths(monkeypatch, tmp_path)
    conn = connect(db_path, init_schema=True)
    try:
        # Old depleted snapshot at 5%, followed by fresh replenished snapshot at 85%
        conn.execute(
            "INSERT INTO quota_snapshots (bucket_id, remaining_fraction, captured_at) VALUES ('gemini-5h', 0.05, '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO quota_snapshots (bucket_id, remaining_fraction, captured_at) VALUES ('gemini-5h', 0.85, '2026-11-26T12:00:00Z')"
        )
    finally:
        conn.close()

    assert ogy_main(["run", "--dry-run", "fix race condition deadlock in worker"]) == 0
    out = capsys.readouterr().out
    assert "--model gemini-4-neon" in out
    assert "--effort high" in out


def test_cli_lifecycle_init_doctor_config_uninstall_and_version(monkeypatch, tmp_path: Path, capsys) -> None:
    db_path, cli_dir, cfg_dir = _patch_env_paths(monkeypatch, tmp_path)

    # Pre-existing unrelated user config must never be harmed
    (cli_dir / "settings.json").write_text(json.dumps({"theme": "solarized"}), encoding="utf-8")
    (cfg_dir / "hooks.json").write_text(json.dumps({"user-custom-hook": {"enabled": True}}), encoding="utf-8")
    (cfg_dir / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"other_srv": {"command": "node"}}}), encoding="utf-8"
    )

    # 1. Idempotent init (run twice)
    assert ogy_main(["init", "--non-interactive", "--enable-firebreaks", "--lang", "fr"]) == 0
    assert ogy_main(["init", "--non-interactive", "--enable-firebreaks"]) == 0
    capsys.readouterr()

    # Original backup preserved (not overwritten on 2nd init)
    bak_settings = json.loads((cli_dir / "settings.json.ogy.bak").read_text(encoding="utf-8"))
    assert "statusLine" not in bak_settings
    assert (tmp_path / ".config" / "optigravity" / "config.toml").is_file()

    # 2. config show / get / set / validate
    assert ogy_main(["config", "set", "general.language", "en"]) == 0
    assert ogy_main(["config", "get", "general.language"]) == 0
    assert "en" in capsys.readouterr().out
    assert ogy_main(["config", "validate"]) == 0
    assert ogy_main(["config", "set", "general.language", "invalid_lang"]) == 1
    capsys.readouterr()

    # 3. doctor --fix
    assert ogy_main(["doctor", "--fix", "--yes"]) == 0
    doc_out = capsys.readouterr().out
    assert "quick_check=ok" in doc_out
    assert "hook_latency" in doc_out

    # 4. uninstall preserves unrelated entries and keeps DB when --purge-data not passed
    assert ogy_main(["uninstall"]) == 0
    capsys.readouterr()
    assert db_path.is_file()
    settings_after = json.loads((cli_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings_after == {"theme": "solarized"}
    hooks_after = json.loads((cfg_dir / "hooks.json").read_text(encoding="utf-8"))
    assert hooks_after == {"user-custom-hook": {"enabled": True}}
    mcp_after = json.loads((cfg_dir / "mcp_config.json").read_text(encoding="utf-8"))
    assert mcp_after == {"mcpServers": {"other_srv": {"command": "node"}}}

    # 5. uninstall --purge-data removes DB and config.toml
    assert ogy_main(["uninstall", "--purge-data"]) == 0
    assert not db_path.is_file()
    assert not (tmp_path / ".config" / "optigravity" / "config.toml").is_file()

    # 6. --version matches ogy_core.get_version()
    import pytest
    from ogy_core import get_version

    with pytest.raises(SystemExit) as exc:
        ogy_main(["--version"])
    assert exc.value.code == 0
    assert get_version() in capsys.readouterr().out
