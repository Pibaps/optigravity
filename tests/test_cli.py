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


def test_v31_backup_and_restore_byte_for_byte(monkeypatch, tmp_path: Path, capsys) -> None:
    """Lot 3.1.2: full timestamped .gemini backup/restore restores pre-existing files byte-for-byte and removes added files."""
    _, cli_dir, cfg_dir = _patch_env_paths(monkeypatch, tmp_path)
    monkeypatch.delenv("OGY_GEMINI_HOME", raising=False)
    monkeypatch.delenv("OGY_REAL_HOME", raising=False)
    monkeypatch.delenv("OGY_CONFIG_PATH", raising=False)

    orig_settings_bytes = b'{\n  "theme": "gruvbox",\n  "fontSize": 14\n}\n'
    orig_mcp_bytes = b'{\n  "mcpServers": {\n    "user_srv": {"command": "uvx"}\n  }\n}\n'
    orig_rule_bytes = b"# User Custom Rule\nAlways keep diffs small.\n"

    (cli_dir / "settings.json").write_bytes(orig_settings_bytes)
    (cfg_dir / "mcp_config.json").write_bytes(orig_mcp_bytes)
    (cfg_dir / "rules").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "rules" / "user-custom.md").write_bytes(orig_rule_bytes)
    assert not (cfg_dir / "hooks.json").exists()

    # 1. Shared init creates a full snapshot before modifying anything
    assert ogy_main(["init", "--non-interactive", "--shared", "--enable-firebreaks"]) == 0
    init_out = capsys.readouterr().out
    assert "snapshot=" in init_out

    backups_dir = tmp_path / ".local" / "share" / "optigravity" / "backups"
    snaps = [p for p in backups_dir.iterdir() if p.is_dir()]
    assert len(snaps) == 1
    manifest = json.loads((snaps[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["antigravity-cli/settings.json"]["existed"] is True
    assert manifest["files"]["config/hooks.json"]["existed"] is False
    assert manifest["files"]["config/rules/user-custom.md"]["existed"] is True

    # 2. Manual backup captures post-init state, then second manual backup deduplicates
    assert ogy_main(["backup"]) == 0
    b1_out = capsys.readouterr().out
    assert "Created backup" in b1_out

    assert ogy_main(["backup"]) == 0
    b2_out = capsys.readouterr().out
    assert "No changes since latest backup" in b2_out

    assert ogy_main(["backup", "--list"]) == 0
    list_out = capsys.readouterr().out
    assert manifest["snapshot_id"] in list_out

    # 3. Restore --dry-run does not modify files
    assert ogy_main(["restore", manifest["snapshot_id"], "--dry-run"]) == 0
    dry_out = capsys.readouterr().out
    assert "[dry-run]" in dry_out
    assert (cfg_dir / "hooks.json").is_file()

    # 4. Restore initial snapshot restores byte-for-byte & deletes files added by ogy init
    assert ogy_main(["restore", manifest["snapshot_id"]]) == 0
    capsys.readouterr()
    assert (cli_dir / "settings.json").read_bytes() == orig_settings_bytes
    assert (cfg_dir / "mcp_config.json").read_bytes() == orig_mcp_bytes
    assert (cfg_dir / "rules" / "user-custom.md").read_bytes() == orig_rule_bytes
    assert not (cfg_dir / "hooks.json").exists()
    assert not (cfg_dir / "rules" / "ogy-hygiene.md").exists()
    assert not (cfg_dir / "skills" / "ogy-budget" / "SKILL.md").exists()

    # 5. Re-init and verify `ogy uninstall --restore-backups` uses the initial snapshot
    assert ogy_main(["init", "--non-interactive", "--shared"]) == 0
    assert (cfg_dir / "hooks.json").is_file()
    assert ogy_main(["uninstall", "--restore-backups"]) == 0
    uninst_out = capsys.readouterr().out
    assert "Restore initial snapshot" in uninst_out
    assert (cli_dir / "settings.json").read_bytes() == orig_settings_bytes
    assert not (cfg_dir / "hooks.json").exists()


def test_v31_isolated_profile_doctor_and_run_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    """Lot 3.1.3: `ogy init --isolated` keeps ~/.gemini 100% untouched, links OAuth, and configures --settings."""
    _, cli_dir, cfg_dir = _patch_env_paths(monkeypatch, tmp_path)
    monkeypatch.delenv("OGY_GEMINI_HOME", raising=False)
    monkeypatch.delenv("OGY_REAL_HOME", raising=False)
    monkeypatch.delenv("OGY_CONFIG_PATH", raising=False)

    shared_gemini = tmp_path / ".gemini"
    orig_settings_bytes = b'{"theme": "native-untouched"}\n'
    (cli_dir / "settings.json").write_bytes(orig_settings_bytes)
    (shared_gemini / "oauth_creds.json").write_text('{"access_token": "fake-token"}\n', encoding="utf-8")
    (shared_gemini / "google_accounts.json").write_text('{"active": "dev@example.com"}\n', encoding="utf-8")

    # 1. Init in isolated mode
    assert ogy_main(["init", "--non-interactive", "--isolated", "--enable-firebreaks"]) == 0
    init_out = capsys.readouterr().out
    assert "isolated profile" in init_out

    # Shared ~/.gemini must remain 100% untouched
    assert (cli_dir / "settings.json").read_bytes() == orig_settings_bytes
    assert not (cfg_dir / "hooks.json").exists()
    assert not (cfg_dir / "mcp_config.json").exists()
    assert not (cfg_dir / "rules" / "ogy-hygiene.md").exists()

    # Isolated profile must have all managed files + linked OAuth files
    iso_gemini = tmp_path / ".local" / "share" / "optigravity" / "gemini-profile" / ".gemini"
    assert (iso_gemini / "antigravity-cli" / "settings.json").is_file()
    assert (iso_gemini / "config" / "hooks.json").is_file()
    assert (iso_gemini / "config" / "mcp_config.json").is_file()
    assert (iso_gemini / "config" / "rules" / "ogy-hygiene.md").is_file()
    assert (iso_gemini / "config" / "skills" / "ogy-budget" / "SKILL.md").is_file()
    assert (iso_gemini / "oauth_creds.json").is_file()
    assert (iso_gemini / "google_accounts.json").is_file()

    # 2. `ogy run --dry-run` passes --settings pointing to isolated settings.json
    assert ogy_main(["run", "--dry-run", "fix race condition deadlock"]) == 0
    run_out = capsys.readouterr().out
    assert "--settings" in run_out
    assert "gemini-profile" in run_out

    # 3. `ogy doctor` reports isolated mode, OAuth links OK, and shared_clean OK
    assert ogy_main(["doctor"]) == 0
    doc_out = capsys.readouterr().out
    assert "profile_mode: isolated" in doc_out
    assert "oauth_links : OK" in doc_out
    assert "shared_clean: OK" in doc_out

    # 4. If a residue appears in shared ~/.gemini, `ogy doctor` flags it
    (cfg_dir / "rules").mkdir(parents=True, exist_ok=True)
    (cfg_dir / "rules" / "ogy-hygiene.md").write_text("# leftover", encoding="utf-8")
    assert ogy_main(["doctor"]) == 0
    doc_warn = capsys.readouterr().out
    assert "shared_clean: WARN" in doc_warn
    assert "ogy-hygiene.md" in doc_warn

    # 5. OGY_GEMINI_HOME env override is honored by load_config()
    from ogy_core.config import load_config

    custom_gemini = tmp_path / "custom-gemini"
    monkeypatch.setenv("OGY_GEMINI_HOME", str(custom_gemini))
    cfg_override = load_config()
    assert cfg_override.agy_cli_dir == custom_gemini / "antigravity-cli"
    assert cfg_override.agy_config_dir == custom_gemini / "config"
