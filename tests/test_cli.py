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
