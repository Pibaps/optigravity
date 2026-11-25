"""Main CLI entrypoint for optigravity (`ogy`)."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from ogy_cli.wrapper import run_agy
from ogy_core.config import load_config
from ogy_core.ingest import open_agy_db_readonly
from ogy_core.storage import connect, detect_waste, query_all


def _run_probe_cmd(cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        res = subprocess.run(
            cmd,
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


def cmd_doctor(probe_agy: bool = False) -> int:
    """Check local environment, SQLite WAL DB, and optionally probe local agy build."""
    cfg = load_config()
    print(f"config_path : {cfg.config_path} (exists={cfg.config_path.is_file()})")
    print(f"db_path     : {cfg.db_path} (exists={cfg.db_path.is_file()})")
    print(f"agy_cli_dir : {cfg.agy_cli_dir} (exists={cfg.agy_cli_dir.is_dir()})")
    print(f"agy_cfg_dir : {cfg.agy_config_dir} (exists={cfg.agy_config_dir.is_dir()})")
    print(f"overwrite_ok: {cfg.overwrite_verified_ok}")

    # Test SQLite WAL connection
    conn = connect(cfg.db_path, init_schema=True)
    try:
        jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        print(f"sqlite_wal  : journal_mode={jm}")
    finally:
        conn.close()

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


def cmd_stats(session_id: str | None = None, as_json: bool = False) -> int:
    """Display KPI summary from v_session_kpis."""
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        if session_id:
            rows = query_all(conn, "SELECT * FROM v_session_kpis WHERE session_id = ?", (session_id,))
        else:
            rows = query_all(conn, "SELECT * FROM v_session_kpis ORDER BY started_at DESC LIMIT 20")
        data = [dict(r) for r in rows]
    finally:
        conn.close()

    if as_json:
        print(json.dumps(data, indent=2))
        return 0

    if not data:
        print("No sessions recorded yet.")
        return 0

    for row in data:
        hit_pct = round((row["cache_hit_ratio"] or 0.0) * 100.0, 1)
        print(
            f"{row['session_id'][:16]} | model={row['initial_model'] or '-'} | "
            f"turns={row['turn_count']} | cache={hit_pct}% | "
            f"peak_ctx={row['peak_context_tokens']} | cost=${row['turns_cost_usd']:.4f}"
        )
    return 0


def cmd_waste(session_id: str | None = None, as_json: bool = False) -> int:
    """Run the 8 automated waste-detection queries."""
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        findings = detect_waste(conn, session_id=session_id)
    finally:
        conn.close()

    if as_json:
        print(json.dumps(findings, indent=2))
        return 0

    if not findings:
        print("No waste detected.")
        return 0

    for f in findings:
        print(f"[W{f['waste_id']}:{f['kind']}] session={f['session_id'][:16]} | {f['detail']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ogy", description="Optigravity CLI for Google Antigravity")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Route and launch agy with direct TTY inheritance")
    p_run.add_argument("prompt", nargs="?", default=None, help="Optional initial prompt")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--effort", default=None)
    p_run.add_argument("--mode", default=None)
    p_run.add_argument("-p", "--print", dest="headless", action="store_true", help="Non-interactive print mode (-p)")
    p_run.add_argument("--sandbox", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")

    p_doc = sub.add_parser("doctor", help="Inspect environment and optionally probe agy")
    p_doc.add_argument("--probe-agy", action="store_true", help="Run Phase 0 probes against local agy")

    p_stats = sub.add_parser("stats", help="Show session KPI statistics")
    p_stats.add_argument("--session", default=None)
    p_stats.add_argument("--json", action="store_true")

    p_waste = sub.add_parser("waste", help="Run the 8 waste-detection queries")
    p_waste.add_argument("--session", default=None)
    p_waste.add_argument("--json", action="store_true")

    p_init = sub.add_parser("init", help="Install hooks, statusLine, rules, skills, and MCP config")
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--enable-firebreaks", action="store_true")

    p_audit = sub.add_parser("audit", help="Audit configurations")
    audit_sub = p_audit.add_subparsers(dest="audit_target", required=True)
    p_audit_mcp = audit_sub.add_parser("mcp", help="Audit MCP servers for context bloat")
    p_audit_mcp.add_argument("--json", action="store_true")

    p_wrap = sub.add_parser("wrap", help="Run shell command with deterministic compression + tee-on-failure")
    p_wrap.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute (after --)")

    p_recall = sub.add_parser("recall", help="Recall stashed raw output by hash")
    p_recall.add_argument("hash", help="SHA-256 hash prefix of stashed output")
    p_recall.add_argument("--lines", default=None, help="Line range A:B (1-based inclusive)")

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
        return cmd_doctor(probe_agy=args.probe_agy)
    if args.command == "stats":
        return cmd_stats(session_id=args.session, as_json=args.json)
    if args.command == "waste":
        return cmd_waste(session_id=args.session, as_json=args.json)
    if args.command == "init":
        from ogy_cli.init_audit import cmd_init

        return cmd_init(dry_run=args.dry_run, enable_firebreaks=args.enable_firebreaks)
    if args.command == "audit" and args.audit_target == "mcp":
        from ogy_cli.init_audit import cmd_audit_mcp

        return cmd_audit_mcp(as_json=args.json)
    if args.command == "wrap":
        from ogy_core.firebreaks import run_wrapped_command

        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        return run_wrapped_command(cmd)
    if args.command == "recall":
        from ogy_core.firebreaks import recall_stashed_output

        print(recall_stashed_output(args.hash, lines_spec=args.lines))
        return 0
    if args.command in ("tui", "chat"):
        from ogy_frontends.acp_tui.app import run_tui

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
        conn = connect(cfg.db_path, init_schema=True)
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
