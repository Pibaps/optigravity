"""`ogy init`, `ogy uninstall`, and `ogy audit mcp` implementations (100% stdlib)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ogy_cli.config_cmd import render_default_config_toml, set_config_value
from ogy_core.config import load_config
from ogy_core.profile import (
    create_gemini_backup,
    link_oauth_to_isolated_profile,
    list_gemini_backups,
    restore_gemini_backup,
)

MANAGED_KEY = "ogy-managed"
_PLAINTEXT_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")


def _templates_dir() -> Path:
    packaged = Path(__file__).resolve().parent / "templates"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parent.parent / "templates"


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _backup_once(path: Path) -> Path | None:
    """Create `<path>.ogy.bak` only if it does not already exist (idempotent init)."""
    if not path.is_file():
        return None
    bak = path.with_suffix(path.suffix + ".ogy.bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def build_hooks_config(*, enable_firebreaks: bool = False) -> dict[str, Any]:
    """Return ~/.gemini/config/hooks.json entries tagged with `ogy-managed: true`."""
    return {
        "ogy-telemetry-and-budget": {
            MANAGED_KEY: True,
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
            MANAGED_KEY: True,
            "enabled": enable_firebreaks,
            "PreToolUse": [
                {
                    "matcher": "run_command|view_file|read_file",
                    "hooks": [{"type": "command", "command": "ogy-hook pre-tool", "timeout": 5}],
                }
            ],
        },
    }


def _prompt_choice(question: str, choices: tuple[str, ...], default: str) -> str:
    opts = "/".join(c.upper() if c == default else c for c in choices)
    try:
        ans = input(f"{question} [{opts}]: ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans if ans in choices else default


def cmd_init(
    *,
    dry_run: bool = False,
    enable_firebreaks: bool = False,
    non_interactive: bool = False,
    language: str | None = None,
    privacy_mode: str | None = None,
    profile_mode: str | None = None,
) -> int:
    """Guided, idempotent, reversible installation of hooks, statusLine, rules, skills, MCP & config.toml."""
    cfg = load_config()
    tpl = _templates_dir()

    interactive = (not non_interactive) and (not dry_run) and bool(getattr(sys.stdin, "isatty", lambda: False)())

    chosen_lang = (language or cfg.language or "en").lower()
    if chosen_lang not in ("en", "fr"):
        chosen_lang = "en"
    chosen_firebreaks = enable_firebreaks
    chosen_privacy = (privacy_mode or "tier1_only").lower()
    if chosen_privacy not in ("tier1_only", "free_first", "disabled"):
        chosen_privacy = "tier1_only"
    chosen_profile = (profile_mode or cfg.profile_mode or "shared").lower()
    if chosen_profile not in ("isolated", "shared"):
        chosen_profile = "shared"

    if interactive:
        print("── Optigravity Setup (5 steps) ──")
        chosen_lang = _prompt_choice("1/5 Language (en/fr)", ("en", "fr"), chosen_lang)
        chosen_profile = _prompt_choice(
            "2/5 Profile mode (isolated=keeps ~/.gemini untouched, shared=writes into ~/.gemini)",
            ("isolated", "shared"),
            chosen_profile,
        )
        fb_ans = _prompt_choice(
            "3/5 Enable PreToolUse firebreaks (silent wrap + file limits)?",
            ("y", "n"),
            "y" if enable_firebreaks else "n",
        )
        chosen_firebreaks = fb_ans == "y"
        chosen_privacy = _prompt_choice(
            "4/5 Subagents MCP privacy mode (tier1_only=private code safe, free_first=OSS, disabled)",
            ("tier1_only", "free_first", "disabled"),
            chosen_privacy,
        )
        print("5/5 Writing managed configs & commented config.toml...")

    shared_gemini_dir = cfg.shared_gemini_dir or (Path.home() / ".gemini")
    isolated_root = cfg.isolated_profile_root or (Path.home() / ".local" / "share" / "optigravity" / "gemini-profile")
    backups_dir = cfg.backups_dir or (Path.home() / ".local" / "share" / "optigravity" / "backups")

    env_gemini = os.environ.get("OGY_GEMINI_HOME", "").strip()
    if env_gemini:
        target_gemini_dir = Path(env_gemini).expanduser()
    elif chosen_profile == "isolated":
        target_gemini_dir = isolated_root / ".gemini"
    else:
        target_gemini_dir = shared_gemini_dir

    target_cli_dir = target_gemini_dir / "antigravity-cli"
    target_cfg_dir = target_gemini_dir / "config"

    settings_path = target_cli_dir / "settings.json"
    hooks_path = target_cfg_dir / "hooks.json"
    mcp_path = target_cfg_dir / "mcp_config.json"
    rule_dst = target_cfg_dir / "rules" / "ogy-hygiene.md"
    skill_dst = target_cfg_dir / "skills" / "ogy-budget" / "SKILL.md"
    config_toml_path = cfg.config_path

    # 1. settings.json (statusLine, tagged ogy-managed)
    settings_data = _load_json_or_empty(settings_path)
    settings_data["statusLine"] = {
        MANAGED_KEY: True,
        "type": "command",
        "command": "ogy-hook statusline",
        "enabled": True,
        "padding": 0,
        "stack_with_default": True,
    }

    # 2. hooks.json (tagged ogy-managed)
    hooks_data = _load_json_or_empty(hooks_path)
    hooks_data.update(build_hooks_config(enable_firebreaks=chosen_firebreaks))

    # 3. mcp_config.json (eager: false, tagged ogy-managed, ZERO plaintext API keys)
    mcp_data = _load_json_or_empty(mcp_path)
    servers = mcp_data.get("mcpServers") if isinstance(mcp_data.get("mcpServers"), dict) else {}
    servers["optigravity"] = {
        MANAGED_KEY: True,
        "command": "ogy-mcp",
        "args": [],
        "eager": False,
    }
    if chosen_privacy != "disabled":
        servers["ogy-subagents"] = {
            MANAGED_KEY: True,
            "command": "ogy-subagents",
            "args": [],
            "eager": False,
        }
    mcp_data["mcpServers"] = servers

    if dry_run:
        print(f"[dry-run] Profile mode: {chosen_profile}")
        if chosen_profile == "isolated":
            print(f"[dry-run] Would link OAuth credentials from {shared_gemini_dir.as_posix()} -> {target_gemini_dir.as_posix()}")
        else:
            print(f"[dry-run] Would snapshot {target_gemini_dir.as_posix()} into {backups_dir.as_posix()}")
        print(f"[dry-run] Would update {settings_path.as_posix()}")
        print(f"[dry-run] Would update {hooks_path.as_posix()}")
        print(f"[dry-run] Would update {mcp_path.as_posix()}")
        print(f"[dry-run] Would install {rule_dst.as_posix()}")
        print(f"[dry-run] Would install {skill_dst.as_posix()}")
        print(f"[dry-run] Would ensure {config_toml_path.as_posix()}")
        return 0

    snap_info: dict[str, Any] | None = None
    if chosen_profile == "shared":
        snap_info = create_gemini_backup(
            target_gemini_dir,
            backups_dir,
            profile_mode="shared",
            reason="init",
        )
    else:
        link_oauth_to_isolated_profile(shared_gemini_dir, target_gemini_dir)

    for path, payload in (
        (settings_path, settings_data),
        (hooks_path, hooks_data),
        (mcp_path, mcp_data),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if chosen_profile == "shared":
            _backup_once(path)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    rule_src = tpl / "ogy-hygiene.md"
    skill_src = tpl / "ogy-frugal-engineering" / "SKILL.md"
    if rule_src.is_file():
        rule_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rule_src, rule_dst)
    if skill_src.is_file():
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skill_dst)

    if not config_toml_path.is_file():
        config_toml_path.parent.mkdir(parents=True, exist_ok=True)
        config_toml_path.write_text(
            render_default_config_toml(
                language=chosen_lang,
                overwrite_verified_ok=True,
                privacy_mode=chosen_privacy,
                isolate_hooks=cfg.isolate_hooks,
                profile_mode=chosen_profile,
            ),
            encoding="utf-8",
        )
    elif profile_mode is not None or chosen_profile != cfg.profile_mode:
        set_config_value("general.profile_mode", chosen_profile, config_path=config_toml_path)

    if chosen_profile == "isolated":
        print(
            f"Optigravity installed in isolated profile ({target_gemini_dir.as_posix()}); "
            f"{shared_gemini_dir.as_posix()} left untouched."
        )
    else:
        snap_note = f"snapshot={snap_info['snapshot_id']}" if snap_info else "backups=*.ogy.bak"
        print(f"Optigravity installed in shared mode (idempotent; {snap_note}, *.ogy.bak preserved).")
    return 0


def cmd_backup(*, list_only: bool = False) -> int:
    """Create a full .gemini snapshot or list existing snapshots."""
    cfg = load_config()
    backups_dir = cfg.backups_dir or (Path.home() / ".local" / "share" / "optigravity" / "backups")

    if list_only:
        snaps = list_gemini_backups(backups_dir)
        if not snaps:
            print(f"No backups found in {backups_dir.as_posix()}.")
            return 0
        print(f"Backups in {backups_dir.as_posix()}:")
        for s in snaps:
            print(
                f"  {s['snapshot_id']}  {s['created_at']}  mode={s['profile_mode']}  "
                f"files={s['file_count']}  reason={s['reason']}"
            )
        return 0

    env_gemini = os.environ.get("OGY_GEMINI_HOME", "").strip()
    target_gemini_dir = (
        Path(env_gemini).expanduser()
        if env_gemini
        else (cfg.shared_gemini_dir or (Path.home() / ".gemini"))
    )
    res = create_gemini_backup(
        target_gemini_dir,
        backups_dir,
        profile_mode=cfg.profile_mode,
        reason="manual",
    )
    if res["created"]:
        print(
            f"Created backup {res['snapshot_id']} ({res['file_count']} files) "
            f"at {Path(res['path']).as_posix()}"
        )
    else:
        print(
            f"No changes since latest backup {res['snapshot_id']} ({res['file_count']} files)."
        )
    return 0


def cmd_restore(
    *,
    snapshot_id: str | None = None,
    latest: bool = False,
    dry_run: bool = False,
) -> int:
    """Restore .gemini exactly to a saved snapshot."""
    if not snapshot_id and not latest:
        print("Usage: ogy restore [<snapshot_id> | --latest] [--dry-run]")
        return 2

    cfg = load_config()
    backups_dir = cfg.backups_dir or (Path.home() / ".local" / "share" / "optigravity" / "backups")
    env_gemini = os.environ.get("OGY_GEMINI_HOME", "").strip()
    target_gemini_dir = (
        Path(env_gemini).expanduser()
        if env_gemini
        else (cfg.shared_gemini_dir or (Path.home() / ".gemini"))
    )

    try:
        res = restore_gemini_backup(
            target_gemini_dir,
            backups_dir,
            snapshot_id=snapshot_id,
            latest=latest,
            dry_run=dry_run,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ogy restore] {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}Restored snapshot {res['snapshot_id']} -> {target_gemini_dir.as_posix()}")
    for act in res["actions"]:
        print(f"{prefix}  {act}")
    return 0


def cmd_uninstall(
    *,
    purge_data: bool = False,
    restore_backups: bool = False,
    dry_run: bool = False,
) -> int:
    """Exact inverse of `ogy init`: removes ONLY ogy-managed entries; keeps DB/stash unless --purge-data."""
    cfg = load_config()
    settings_path = cfg.agy_cli_dir / "settings.json"
    hooks_path = cfg.agy_config_dir / "hooks.json"
    mcp_path = cfg.agy_config_dir / "mcp_config.json"
    rule_dst = cfg.agy_config_dir / "rules" / "ogy-hygiene.md"
    skill_dst = cfg.agy_config_dir / "skills" / "ogy-budget" / "SKILL.md"

    actions: list[str] = []

    # 1. settings.json: remove statusLine only if ogy-managed
    if settings_path.is_file():
        data = _load_json_or_empty(settings_path)
        sl = data.get("statusLine")
        if isinstance(sl, dict) and (sl.get(MANAGED_KEY) is True or sl.get("command") == "ogy-hook statusline"):
            actions.append(f"Remove ogy-managed statusLine from {settings_path.as_posix()}")
            if not dry_run:
                data.pop("statusLine", None)
                settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # 2. hooks.json: remove only ogy-managed hook groups
    if hooks_path.is_file():
        data = _load_json_or_empty(hooks_path)
        to_remove = [
            k
            for k, v in data.items()
            if (isinstance(v, dict) and v.get(MANAGED_KEY) is True)
            or k in ("ogy-telemetry-and-budget", "ogy-firebreaks")
        ]
        if to_remove:
            actions.append(f"Remove ogy-managed hooks {to_remove} from {hooks_path.as_posix()}")
            if not dry_run:
                for k in to_remove:
                    data.pop(k, None)
                hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # 3. mcp_config.json: remove only ogy-managed server entries (optigravity, ogy-subagents)
    if mcp_path.is_file():
        data = _load_json_or_empty(mcp_path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            removed_srvs: list[str] = []
            for srv_name, expected_cmd in (("optigravity", "ogy-mcp"), ("ogy-subagents", "ogy-subagents")):
                if srv_name in servers:
                    entry = servers[srv_name]
                    if isinstance(entry, dict) and (
                        entry.get(MANAGED_KEY) is True or entry.get("command") == expected_cmd
                    ):
                        removed_srvs.append(srv_name)
                        if not dry_run:
                            servers.pop(srv_name, None)
            if removed_srvs:
                actions.append(
                    f"Remove ogy-managed MCP server(s) {removed_srvs} from {mcp_path.as_posix()}"
                )
                if not dry_run:
                    mcp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # 4. Managed rule & skill files
    if rule_dst.is_file():
        actions.append(f"Delete {rule_dst.as_posix()}")
        if not dry_run:
            rule_dst.unlink(missing_ok=True)
    if skill_dst.is_file():
        actions.append(f"Delete {skill_dst.as_posix()}")
        if not dry_run:
            skill_dst.unlink(missing_ok=True)
            try:
                skill_dst.parent.rmdir()
            except OSError:
                pass

    # 5. Optional backup restoration (prefer full initial snapshot, fallback to *.ogy.bak)
    backups_dir = cfg.backups_dir or (Path.home() / ".local" / "share" / "optigravity" / "backups")
    shared_gemini_dir = cfg.shared_gemini_dir or (Path.home() / ".gemini")
    snapshots = list_gemini_backups(backups_dir)
    available_baks = [
        p.with_suffix(p.suffix + ".ogy.bak")
        for p in (settings_path, hooks_path, mcp_path)
        if p.with_suffix(p.suffix + ".ogy.bak").is_file()
    ]
    if restore_backups:
        if snapshots:
            res = restore_gemini_backup(
                shared_gemini_dir,
                backups_dir,
                oldest=True,
                dry_run=dry_run,
            )
            actions.append(f"Restore initial snapshot {res['snapshot_id']} -> {shared_gemini_dir.as_posix()}")
            actions.extend(f"  {a}" for a in res["actions"])
        elif available_baks:
            for bak in available_baks:
                orig = Path(str(bak)[: -len(".ogy.bak")])
                actions.append(f"Restore {bak.as_posix()} -> {orig.as_posix()}")
                if not dry_run:
                    shutil.copy2(bak, orig)

    # 6. Optional --purge-data (ogy.db, stash, pending_tasks, isolated profile, config.toml)
    if purge_data:
        for suffix in ("", "-wal", "-shm"):
            db_file = Path(str(cfg.db_path) + suffix)
            if db_file.is_file():
                actions.append(f"Purge {db_file.as_posix()}")
                if not dry_run:
                    db_file.unlink(missing_ok=True)
        purge_dirs = [cfg.stash_dir, cfg.stash_dir.parent / "pending_tasks"]
        if cfg.isolated_profile_root is not None:
            purge_dirs.append(cfg.isolated_profile_root)
        for d in purge_dirs:
            if d.is_dir():
                actions.append(f"Purge directory {d.as_posix()}")
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)
        if cfg.config_path.is_file():
            actions.append(f"Purge {cfg.config_path.as_posix()}")
            if not dry_run:
                cfg.config_path.unlink(missing_ok=True)

    prefix = "[dry-run] " if dry_run else ""
    if not actions:
        print(f"{prefix}Nothing to uninstall (no ogy-managed entries found).")
    else:
        for act in actions:
            print(f"{prefix}{act}")
        if not purge_data:
            print(f"{prefix}Data preserved ({cfg.db_path}). Pass --purge-data to also delete DB/stash/config.")
        if (snapshots or available_baks) and not restore_backups:
            print(
                f"{prefix}Backups available ({len(snapshots)} snapshot(s), {len(available_baks)} *.ogy.bak). "
                "Pass --restore-backups to restore them."
            )
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
        raw_srv_blob = json.dumps(srv, ensure_ascii=False)
        if _PLAINTEXT_API_KEY_RE.search(raw_srv_blob):
            issues.append("plaintext Gemini API key (AIza*) detected in mcp_config.json; move to env/keyring/secrets.env")
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
