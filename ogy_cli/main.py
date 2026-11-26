"""Main CLI entrypoint for optigravity (`ogy`)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

from ogy_cli.wrapper import run_agy
from ogy_core import get_version
from ogy_core.config import load_config
from ogy_core.ingest import open_agy_db_readonly
from ogy_core.storage import CURRENT_SCHEMA_VERSION, connect, connect_readonly, detect_waste, run_stop_maintenance


def _run_probe_cmd(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 8.0) -> tuple[int, str, str]:
    resolved_cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]] if cmd else cmd
    try:
        res = subprocess.run(
            resolved_cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout after {timeout}s"


def _check_json_file(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return True, "missing (optional)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, "root is not a JSON object"
        return True, "valid JSON"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid JSON ({exc})"


def _check_gemini_api_key(
    *,
    privacy_mode: str = "tier1_only",
    secrets_path: Path | None = None,
) -> tuple[str, str]:
    """Zero-token connectivity probe using the 3-step secrets cascade (env -> keyring -> secrets.env)."""
    if privacy_mode == "disabled":
        return "skip", "disabled in config.toml (subagents.privacy_mode = disabled)"

    tier_order = ("tier1",) if privacy_mode == "tier1_only" else ("free", "tier1")
    resolved_key = ""
    resolved_source = ""
    resolved_warning = ""

    try:
        from ogy_subagents.secrets import probe_gemini_api_key, resolve_api_key

        for tier in tier_order:
            res = resolve_api_key(tier, secrets_path=secrets_path)
            if res.warning and not resolved_warning:
                resolved_warning = res.warning
            if res.key:
                resolved_key = res.key
                resolved_source = f"{tier}:{res.source}"
                break
        if not resolved_key:
            detail = f"not configured for privacy_mode={privacy_mode}"
            if resolved_warning:
                detail += f" ({resolved_warning})"
            return "skip", detail
        status, detail = probe_gemini_api_key(resolved_key)
        return status, f"{detail} [source={resolved_source}, privacy={privacy_mode}]"
    except ImportError:
        # Pure stdlib fallback if ogy_subagents is not installed
        key = (
            os.environ.get("OGY_GEMINI_API_KEY_TIER1")
            or (os.environ.get("OGY_GEMINI_API_KEY_FREE") if privacy_mode != "tier1_only" else None)
            or os.environ.get("GEMINI_API_KEY")
            or ""
        ).strip()
        if not key:
            return "skip", f"not configured (privacy_mode={privacy_mode})"
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash"
        req = urllib.request.Request(url, headers={"x-goog-api-key": key}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                return ("ok", f"HTTP {resp.status} (0 tokens)") if resp.status == 200 else ("warn", f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            return "fail", f"HTTP {exc.code}"
        except Exception as exc:
            return "warn", f"unreachable ({type(exc).__name__})"


def _measure_hook_latency_ms() -> float:
    """Measure in-process PreToolUse dispatch latency (or ogy-hook hot-path timing)."""
    from ogy_hooks.cli import dispatch_hook

    cfg = load_config()
    t0 = time.perf_counter()
    dispatch_hook(
        "pre-tool",
        {"conversationId": "", "toolCall": {"name": "run_command", "args": {"CommandLine": "echo ok"}}},
        cfg=cfg,
    )
    return (time.perf_counter() - t0) * 1000.0


def cmd_doctor(probe_agy: bool = False, fix: bool = False, yes: bool = False) -> int:
    """Check local environment, JSON configs, permissions, hook latency, SQLite WAL, and optionally fix anomalies."""
    cfg = load_config()
    anomalies: list[tuple[str, str]] = []

    print(f"version     : optigravity {get_version()}")
    print(f"config_path : {cfg.config_path} (exists={cfg.config_path.is_file()})")
    print(f"db_path     : {cfg.db_path} (exists={cfg.db_path.is_file()})")
    print(f"agy_cli_dir : {cfg.agy_cli_dir} (exists={cfg.agy_cli_dir.is_dir()})")
    print(f"agy_cfg_dir : {cfg.agy_config_dir} (exists={cfg.agy_config_dir.is_dir()})")
    print(f"language    : {cfg.language} | overwrite_ok={cfg.overwrite_verified_ok}")

    # 1. agy binary check
    agy_bin = shutil.which("agy")
    print(f"agy_binary  : {agy_bin or 'NOT FOUND'}")
    if not agy_bin:
        anomalies.append(("agy_missing", "agy binary not found in PATH"))

    # 2. JSON validity checks
    for label, p in (
        ("settings.json", cfg.agy_cli_dir / "settings.json"),
        ("hooks.json", cfg.agy_config_dir / "hooks.json"),
        ("mcp_config.json", cfg.agy_config_dir / "mcp_config.json"),
    ):
        ok, detail = _check_json_file(p)
        print(f"json:{label:<15}: {'OK' if ok else 'FAIL'} ({detail})")
        if not ok:
            anomalies.append((f"json_{label}", f"{p}: {detail}"))

    # 3. config.toml validation
    from ogy_cli.config_cmd import render_default_config_toml, validate_config_file

    cfg_errors = validate_config_file(cfg.config_path)
    if not cfg.config_path.is_file():
        print("config_toml : MISSING (can be generated with --fix)")
        anomalies.append(("missing_config_toml", f"Create default {cfg.config_path}"))
    elif cfg_errors:
        print(f"config_toml : FAIL ({'; '.join(cfg_errors)})")
        anomalies.append(("invalid_config_toml", "; ".join(cfg_errors)))
    else:
        print("config_toml : OK")

    # 4. secrets.env permissions check
    secrets_path = cfg.config_path.parent / "secrets.env"
    if secrets_path.is_file():
        if os.name != "nt":
            mode = stat.S_IMODE(secrets_path.stat().st_mode)
            if mode & 0o077:
                print(f"secrets.env : WARN (mode {oct(mode)}, expected 0o600)")
                anomalies.append(("secrets_perms", f"chmod 0600 {secrets_path}"))
            else:
                print(f"secrets.env : OK (mode {oct(mode)})")
        else:
            print("secrets.env : OK (present on Windows)")
    else:
        print("secrets.env : not present")

    # 5. SQLite WAL + quick_check + schema version
    conn = connect(cfg.db_path, init_schema=True)
    try:
        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        uv = int(conn.execute("PRAGMA user_version;").fetchone()[0])
        qc = conn.execute("PRAGMA quick_check;").fetchone()[0]
        print(f"sqlite_wal  : journal_mode={jm}, user_version={uv}/{CURRENT_SCHEMA_VERSION}, quick_check={qc}")
        if str(qc).lower() != "ok":
            anomalies.append(("sqlite_corrupt", f"PRAGMA quick_check returned {qc}"))
    finally:
        conn.close()

    # 6. Hook hot-path latency (< 25ms target)
    lat_ms = _measure_hook_latency_ms()
    lat_status = "OK" if lat_ms < 25.0 else "WARN"
    print(f"hook_latency: {lat_ms:.2f} ms ({lat_status}, target < 25 ms)")

    # 7. Gemini API key check (0 tokens, respects privacy_mode + 3-step secrets cascade)
    api_status, api_detail = _check_gemini_api_key(
        privacy_mode=cfg.subagents_privacy_mode,
        secrets_path=secrets_path,
    )
    print(f"gemini_api  : {api_status.upper()} ({api_detail})")

    # Optional --fix for deterministic anomalies
    if fix and anomalies:
        fixable = [a for a in anomalies if a[0] in ("missing_config_toml", "secrets_perms")]
        if fixable:
            proceed = yes or not bool(getattr(sys.stdin, "isatty", lambda: False)())
            if not proceed:
                print("\nFixable anomalies detected:")
                for _code, desc in fixable:
                    print(f"  - {desc}")
                try:
                    ans = input("Apply fixes now? [y/N]: ").strip().lower()
                except EOFError:
                    ans = "n"
                proceed = ans in ("y", "yes")
            if proceed:
                for code, _desc in fixable:
                    if code == "missing_config_toml":
                        cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
                        cfg.config_path.write_text(render_default_config_toml(), encoding="utf-8")
                        print(f"[fixed] Created {cfg.config_path}")
                    elif code == "secrets_perms" and os.name != "nt":
                        os.chmod(secrets_path, 0o600)
                        print(f"[fixed] Set mode 0600 on {secrets_path}")
                # Also run passive WAL checkpoint
                c2 = connect(cfg.db_path, init_schema=True)
                try:
                    run_stop_maintenance(c2)
                finally:
                    c2.close()
        else:
            print("\nNo automatically fixable anomalies.")

    if not probe_agy:
        return 0

    print("\n--- Phase 0 Probes (agy) ---")
    env = os.environ.copy()
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"

    rc, out, err = _run_probe_cmd(["agy", "--version"], env=env)
    print(f"[1] agy --version (rc={rc}): {out or err}")

    rc_h, out_h, err_h = _run_probe_cmd(["agy", "--help"], env=env)
    help_text = out_h or err_h
    has_settings_flag = "--settings" in help_text
    has_disable_telemetry = "--disable-telemetry" in help_text
    print(
        f"[1] agy --help (rc={rc_h}): --settings={has_settings_flag}, --disable-telemetry={has_disable_telemetry}"
    )

    rc_m, out_m, err_m = _run_probe_cmd(["agy", "models"], env=env)
    models_preview = (out_m or err_m).splitlines()[:6]
    print(f"[1] agy models (rc={rc_m}): {models_preview}")

    rc_mcp, out_mcp, err_mcp = _run_probe_cmd(["agy", "mcp", "list"], env=env)
    mcp_preview = (out_mcp or err_mcp).splitlines()[:6]
    print(f"[2] agy mcp list (rc={rc_mcp}): {mcp_preview}")

    for candidate in (
        cfg.agy_config_dir / "mcp_config.json",
        cfg.agy_cli_dir / "mcp_config.json",
        Path.cwd() / ".agents" / "mcp_config.json",
    ):
        print(f"    mcp_config candidate: {candidate} (exists={candidate.is_file()})")

    # [3] Read-only DB probe
    summaries_db = cfg.agy_cli_dir / "conversation_summaries.db"
    s_conn = open_agy_db_readonly(summaries_db)
    if s_conn is not None:
        try:
            tables = [r[0] for r in s_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            print(f"[3] conversation_summaries.db RO OK: tables={tables}")
        finally:
            s_conn.close()
    else:
        print(f"[3] conversation_summaries.db not found at {summaries_db}")

    conv_dir = cfg.agy_cli_dir / "conversations"
    conv_dbs = sorted(conv_dir.glob("*.db"))[:1] if conv_dir.is_dir() else []
    if conv_dbs:
        c_conn = open_agy_db_readonly(conv_dbs[0])
        if c_conn is not None:
            try:
                tables = [r[0] for r in c_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                print(f"[3] sample {conv_dbs[0].name} RO OK: tables={tables}")
            finally:
                c_conn.close()
    else:
        print(f"[3] no conversations/*.db found in {conv_dir}")

    # [4] settings.json & hooks.json check
    settings_path = cfg.agy_cli_dir / "settings.json"
    hooks_path = cfg.agy_config_dir / "hooks.json"
    print(f"[4] settings.json exists={settings_path.is_file()}, hooks.json exists={hooks_path.is_file()}")

    # [6] Python -S cold-start benchmark (10 runs)
    latencies_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-S", "-c", "import sys, json, sqlite3"], check=True)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    p50 = statistics.median(latencies_ms)
    print(f"[6] python -S cold-start (10 runs): p50={p50:.1f}ms, min={min(latencies_ms):.1f}ms, max={max(latencies_ms):.1f}ms")
    return 0


def cmd_stats(
    session_id: str | None = None,
    as_json: bool = False,
    *,
    last: int = 20,
    since: str | None = None,
    repo: str | None = None,
) -> int:
    """Display ROI KPI summary from v_session_kpis using read-only SQLite connection."""
    from ogy_cli.stats_render import query_filtered_stats, render_stats_report

    cfg = load_config()
    if not cfg.db_path.is_file():
        print("[]" if as_json else "No sessions recorded yet.")
        return 0

    conn = connect_readonly(cfg.db_path)
    try:
        data = query_filtered_stats(conn, session_id=session_id, last=last, since=since, repo=repo)
        if as_json:
            print(json.dumps(data, indent=2))
            return 0
        print(render_stats_report(conn, data))
    finally:
        conn.close()
    return 0


def cmd_waste(
    session_id: str | None = None,
    as_json: bool = False,
    *,
    last: int = 50,
    since: str | None = None,
    repo: str | None = None,
) -> int:
    """Run the 8 automated waste-detection queries using read-only SQLite connection."""
    from ogy_cli.stats_render import query_filtered_stats, render_waste_report

    cfg = load_config()
    if not cfg.db_path.is_file():
        print("[]" if as_json else "No waste detected.")
        return 0

    conn = connect_readonly(cfg.db_path)
    try:
        findings = detect_waste(conn, session_id=session_id)
        if since or repo:
            allowed_sids = {
                r["session_id"]
                for r in query_filtered_stats(conn, session_id=session_id, last=last, since=since, repo=repo)
            }
            findings = [f for f in findings if f["session_id"] in allowed_sids]
    finally:
        conn.close()

    if as_json:
        print(json.dumps(findings, indent=2))
        return 0

    print(render_waste_report(findings))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ogy", description="Optigravity CLI for Google Antigravity")
    parser.add_argument("-V", "--version", action="version", version=f"optigravity {get_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Route and launch agy with direct TTY inheritance")
    p_run.add_argument("prompt", nargs="?", default=None, help="Optional initial prompt")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--effort", default=None)
    p_run.add_argument("--mode", default=None)
    p_run.add_argument("-p", "--print", dest="headless", action="store_true", help="Non-interactive print mode (-p)")
    p_run.add_argument("--sandbox", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")

    p_doc = sub.add_parser("doctor", help="Inspect environment, verify health, and optionally fix anomalies")
    p_doc.add_argument("--probe-agy", action="store_true", help="Run Phase 0 probes against local agy")
    p_doc.add_argument("--fix", action="store_true", help="Repair deterministic configuration/permission anomalies")
    p_doc.add_argument("-y", "--yes", action="store_true", help="Confirm --fix without prompting")

    p_stats = sub.add_parser("stats", help="Show session KPI statistics and ROI summary")
    p_stats.add_argument("--session", default=None)
    p_stats.add_argument("--last", type=int, default=20, help="Maximum sessions to display (default: 20)")
    p_stats.add_argument("--since", default=None, help="Filter since duration (7d, 24h) or YYYY-MM-DD")
    p_stats.add_argument("--repo", default=None, help="Filter by workspace path substring")
    p_stats.add_argument("--json", action="store_true")

    p_waste = sub.add_parser("waste", help="Run the 8 waste-detection queries with remediation advice")
    p_waste.add_argument("--session", default=None)
    p_waste.add_argument("--last", type=int, default=50)
    p_waste.add_argument("--since", default=None, help="Filter since duration (7d, 24h) or YYYY-MM-DD")
    p_waste.add_argument("--repo", default=None, help="Filter by workspace path substring")
    p_waste.add_argument("--json", action="store_true")

    p_init = sub.add_parser("init", help="Install hooks, statusLine, rules, skills, MCP config & config.toml")
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--enable-firebreaks", action="store_true")
    p_init.add_argument("--non-interactive", action="store_true", help="Skip interactive wizard (CI/scripts)")
    p_init.add_argument("--lang", choices=["en", "fr"], default=None)
    p_init.add_argument("--privacy-mode", choices=["tier1_only", "free_first", "disabled"], default=None)

    p_uninst = sub.add_parser("uninstall", help="Remove ogy-managed hooks, statusLine, rules, skills & MCP entry")
    p_uninst.add_argument("--purge-data", action="store_true", help="Also delete ogy.db, stash, and config.toml")
    p_uninst.add_argument("--restore-backups", action="store_true", help="Restore *.ogy.bak files if present")
    p_uninst.add_argument("--dry-run", action="store_true")

    p_cfg = sub.add_parser("config", help="Inspect, modify, validate, or edit config.toml")
    p_cfg.add_argument("action", nargs="?", default="show", choices=["show", "get", "set", "validate", "edit"])
    p_cfg.add_argument("key", nargs="?", default=None)
    p_cfg.add_argument("value", nargs="?", default=None)
    p_cfg.add_argument("--json", action="store_true")

    p_audit = sub.add_parser("audit", help="Audit configurations")
    audit_sub = p_audit.add_subparsers(dest="audit_target", required=True)
    p_audit_mcp = audit_sub.add_parser("mcp", help="Audit MCP servers for context bloat")
    p_audit_mcp.add_argument("--json", action="store_true")

    p_wrap = sub.add_parser("wrap", help="Run shell command with deterministic compression + tee-on-failure")
    p_wrap.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (after --)")

    p_raw = sub.add_parser("raw", help="Run shell command directly without ogy wrap compression")
    p_raw.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (after --)")

    p_recall = sub.add_parser("recall", help="Recall stashed raw output by hash")
    p_recall.add_argument("hash", help="SHA-256 hash prefix of stashed output")
    p_recall.add_argument("--lines", default=None, help="Line range A:B (1-based inclusive)")
    p_recall.add_argument("--stream", default="stdout", choices=["stdout", "stderr", "both"])

    for tui_cmd in ("tui", "chat"):
        p_tui = sub.add_parser(tui_cmd, help="Launch interactive V2 ACP TUI (`agy acp`)")
        p_tui.add_argument("prompt", nargs="?", default=None, help="Optional initial prompt")
        p_tui.add_argument("--cwd", default=".", help="Workspace directory")
        p_tui.add_argument("--sandbox", action="store_true", help="Pass --sandbox to agy acp")
        p_tui.add_argument("--add-dir", action="append", default=[], help="Additional directory (--add-dir)")
        p_tui.add_argument("--agent", default=None, help="Agent profile (--agent)")
        p_tui.add_argument("--log-file", default=None, help="ACP log file (--log-file)")
        p_tui.add_argument("--auth-mode", default="oauth-personal", help="Auth mode (oauth-personal or api-key)")
        p_tui.add_argument("--model", default=None, help="Pin initial model (disables 1st-prompt auto-routing)")
        p_tui.add_argument("--effort", default=None, help="Pin initial effort (disables 1st-prompt auto-routing)")
        p_tui.add_argument("--lang", default="fr", choices=["fr", "en"], help="TUI language (fr or en)")

    p_eval = sub.add_parser("router-eval", help="Offline shadow evaluation of Stage 2 bandit")
    p_eval.add_argument("--category", default=None)
    p_eval.add_argument("--min-samples", type=int, default=5)
    p_eval.add_argument("--json", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)

    if args.command == "run":
        return run_agy(
            args.prompt,
            model=args.model,
            effort=args.effort,
            mode=args.mode,
            headless=args.headless,
            sandbox=args.sandbox,
            dry_run=args.dry_run,
            extra_args=unknown,
        )
    if args.command == "doctor":
        return cmd_doctor(probe_agy=args.probe_agy, fix=args.fix, yes=args.yes)
    if args.command == "stats":
        return cmd_stats(
            session_id=args.session,
            as_json=args.json,
            last=args.last,
            since=args.since,
            repo=args.repo,
        )
    if args.command == "waste":
        return cmd_waste(
            session_id=args.session,
            as_json=args.json,
            last=args.last,
            since=args.since,
            repo=args.repo,
        )
    if args.command == "init":
        from ogy_cli.init_audit import cmd_init

        return cmd_init(
            dry_run=args.dry_run,
            enable_firebreaks=args.enable_firebreaks,
            non_interactive=args.non_interactive,
            language=args.lang,
            privacy_mode=args.privacy_mode,
        )
    if args.command == "uninstall":
        from ogy_cli.init_audit import cmd_uninstall

        return cmd_uninstall(
            purge_data=args.purge_data,
            restore_backups=args.restore_backups,
            dry_run=args.dry_run,
        )
    if args.command == "config":
        from ogy_cli.config_cmd import cmd_config

        return cmd_config(args.action, key=args.key, value=args.value, as_json=args.json)
    if args.command == "audit" and args.audit_target == "mcp":
        from ogy_cli.init_audit import cmd_audit_mcp

        return cmd_audit_mcp(as_json=args.json)
    if args.command == "wrap":
        from ogy_core.firebreaks import run_wrapped_command

        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        return run_wrapped_command(cmd)
    if args.command == "raw":
        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("[ogy raw] No command provided.", file=sys.stderr)
            return 2
        try:
            resolved_cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]
            proc = subprocess.run(resolved_cmd)
            return int(proc.returncode)
        except FileNotFoundError:
            print(f"[ogy raw] Command not found: {cmd[0]}", file=sys.stderr)
            return 127
    if args.command == "recall":
        from ogy_core.firebreaks import recall_stashed_output

        print(recall_stashed_output(args.hash, lines_spec=args.lines, stream=args.stream))
        return 0
    if args.command in ("tui", "chat"):
        try:
            from ogy_frontends.acp_tui.app import run_tui
        except ImportError as exc:
            print(
                f"Missing optional TUI dependency ({exc.name or exc}).\n"
                "Install from the repository root with:\n"
                '  pip install ".[tui]"                  (or: pip install ".[all]")\n'
                '  uv tool install --reinstall ".[tui]"  (if using uv)\n'
                '  pipx install --force ".[tui]"         (if using pipx)',
                file=sys.stderr,
            )
            return 1

        return run_tui(
            cwd=args.cwd,
            sandbox=args.sandbox,
            add_dirs=args.add_dir,
            agent=args.agent,
            log_file=args.log_file,
            auth_method=args.auth_mode,
            initial_prompt=args.prompt,
            model=args.model,
            effort=args.effort,
            lang=args.lang,
        )
    if args.command == "router-eval":
        from ogy_core.router import evaluate_bandit_offline

        cfg = load_config()
        if not cfg.db_path.is_file():
            report = {"total_decisions": 0, "categories": {}}
        else:
            conn = connect_readonly(cfg.db_path)
            try:
                report = evaluate_bandit_offline(conn, category=args.category, min_samples=args.min_samples)
            finally:
                conn.close()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Total decisions evaluated: {report['total_decisions']}")
            for cat, info in report["categories"].items():
                print(f"  {cat}: samples={info['samples']} best={info['best_arm']} (reward={info['best_mean_reward']})")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
