"""`ogy init` and `ogy audit mcp` implementations."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ogy_core.config import load_config


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_hooks_config(*, enable_firebreaks: bool = False) -> dict[str, Any]:
    """Return exact ~/.gemini/config/hooks.json structure from Annexe 1.10.C."""
    return {
        "ogy-telemetry-and-budget": {
            "enabled": True,
            "SessionStart": [{"type": "command", "command": "ogy-hook session-start", "timeout": 5}],
            "PreInvocation": [{"type": "command", "command": "ogy-hook pre-invocation", "timeout": 3}],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "ogy-hook post-tool", "timeout": 5}],
                }
            ],
            "PostInvocation": [{"type": "command", "command": "ogy-hook post-invocation", "timeout": 5}],
            "Stop": [{"type": "command", "command": "ogy-hook stop", "timeout": 10}],
        },
        "ogy-firebreaks": {
            "enabled": enable_firebreaks,
            "PreToolUse": [
                {
                    "matcher": "run_command|view_file|read_file",
                    "hooks": [{"type": "command", "command": "ogy-hook pre-tool", "timeout": 5}],
                }
            ],
        },
    }


def cmd_init(*, dry_run: bool = False, enable_firebreaks: bool = False) -> int:
    """Reversibly install statusLine, hooks.json, rule, skill, and MCP server config."""
    cfg = load_config()
    tpl = _templates_dir()

    settings_path = cfg.agy_cli_dir / "settings.json"
    hooks_path = cfg.agy_config_dir / "hooks.json"
    mcp_path = cfg.agy_config_dir / "mcp_config.json"
    rule_dst = cfg.agy_config_dir / "rules" / "ogy-hygiene.md"
    skill_dst = cfg.agy_config_dir / "skills" / "ogy-budget" / "SKILL.md"

    # 1. settings.json (statusLine, no ~ or single backslashes)
    settings_data = _load_json_or_empty(settings_path)
    settings_data["statusLine"] = {
        "type": "command",
        "command": "ogy-hook statusline",
        "enabled": True,
        "padding": 0,
        "stack_with_default": True,
    }

    # 2. hooks.json
    hooks_data = _load_json_or_empty(hooks_path)
    hooks_data.update(build_hooks_config(enable_firebreaks=enable_firebreaks))

    # 3. mcp_config.json (eager: false)
    mcp_data = _load_json_or_empty(mcp_path)
    servers = mcp_data.get("mcpServers") if isinstance(mcp_data.get("mcpServers"), dict) else {}
    servers["optigravity"] = {
        "command": "ogy-mcp",
        "args": [],
        "eager": False,
    }
    mcp_data["mcpServers"] = servers

    if dry_run:
        print(f"[dry-run] Would update {settings_path.as_posix()}")
        print(f"[dry-run] Would update {hooks_path.as_posix()}")
        print(f"[dry-run] Would update {mcp_path.as_posix()}")
        print(f"[dry-run] Would install {rule_dst.as_posix()}")
        print(f"[dry-run] Would install {skill_dst.as_posix()}")
        return 0

    for path, payload in (
        (settings_path, settings_data),
        (hooks_path, hooks_data),
        (mcp_path, mcp_data),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            shutil.copy2(path, path.with_suffix(path.suffix + ".ogy.bak"))
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    rule_src = tpl / "ogy-hygiene.md"
    skill_src = tpl / "ogy-frugal-engineering" / "SKILL.md"
    if rule_src.is_file():
        rule_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rule_src, rule_dst)
    if skill_src.is_file():
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skill_dst)

    print("Optigravity V1 installed (backups saved as *.ogy.bak).")
    return 0


def audit_mcp_config(mcp_data: dict[str, Any]) -> dict[str, Any]:
    """Inspect mcp_config.json for eager tools, UI tool leaks, and bloat."""
    servers = mcp_data.get("mcpServers") if isinstance(mcp_data.get("mcpServers"), dict) else {}
    findings: list[dict[str, Any]] = []
    recommended_profile: dict[str, Any] = {}

    for name, srv in servers.items():
        if not isinstance(srv, dict):
            continue
        issues: list[str] = []
        if srv.get("eager") is True:
            issues.append("eager=true forces full schema into every turn")
        tools = srv.get("tools")
        enabled_tools: list[str] = []
        if isinstance(tools, list):
            for t in tools:
                if not isinstance(t, dict):
                    continue
                tname = str(t.get("name") or "")
                vis = ((t.get("_meta") or {}).get("ui") or {}).get("visibility") or []
                if isinstance(vis, list) and "app" in vis and "cli" not in vis:
                    issues.append(f"UI-only tool '{tname}' leaking into CLI")
                elif tname:
                    enabled_tools.append(tname)
        if issues:
            findings.append({"server": name, "issues": issues})
        if enabled_tools:
            recommended_profile[name] = {"eager": False, "enabledTools": enabled_tools}

    return {"findings": findings, "recommended_profile": recommended_profile}


def cmd_audit_mcp(*, as_json: bool = False) -> int:
    """Audit active MCP configurations."""
    cfg = load_config()
    candidates = [
        cfg.agy_config_dir / "mcp_config.json",
        cfg.agy_cli_dir / "mcp_config.json",
        Path.cwd() / ".agents" / "mcp_config.json",
    ]
    all_reports: dict[str, Any] = {}
    for path in candidates:
        if path.is_file():
            all_reports[path.as_posix()] = audit_mcp_config(_load_json_or_empty(path))

    if as_json:
        print(json.dumps(all_reports, indent=2))
        return 0

    if not all_reports:
        print("No mcp_config.json files found.")
        return 0

    for path_str, rep in all_reports.items():
        print(f"MCP Audit: {path_str}")
        if not rep["findings"]:
            print("  OK: no eager or UI-leak issues detected.")
        for f in rep["findings"]:
            print(f"  [!] {f['server']}: {'; '.join(f['issues'])}")
    return 0
