"""Level 1 CLI wrapper for agy without PTY proxy (Windows & POSIX safe)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Sequence

from ogy_core.config import load_config
from ogy_core.ingest import get_latest_quota_snapshots
from ogy_core.profile import build_agy_subprocess_env
from ogy_core.router import RouteDecision, route_stage1, route_stage2_bandit
from ogy_core.storage import connect, write_pending_task, write_tx


def _detect_git_state(cwd: Path) -> tuple[int, bool]:
    """Return (dirty_file_count, has_test_runner)."""
    dirty = 0
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0:
            dirty = len([ln for ln in res.stdout.splitlines() if ln.strip()])
    except (OSError, subprocess.SubprocessError):
        dirty = 0

    has_tests = any(
        (cwd / marker).exists()
        for marker in ("pyproject.toml", "pytest.ini", "tests", "package.json", "Cargo.toml", "go.mod")
    )
    return dirty, has_tests


def build_agy_args(
    decision: RouteDecision,
    *,
    prompt: str | None = None,
    headless: bool = False,
    sandbox: bool = False,
    settings_path: Path | str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build official agy CLI flags (never --permission-mode or -m; supports --settings in isolated mode)."""
    args: list[str] = []
    if settings_path:
        args.extend(["--settings", str(settings_path)])
    if decision.model_id:
        args.extend(["--model", decision.model_id])
    if decision.effort and decision.model_id.lower() != "orchestrator":
        args.extend(["--effort", decision.effort])
    if decision.mode and not (headless and decision.mode == "plan" and decision.stage != "manual_override"):
        args.extend(["--mode", decision.mode])
    if sandbox:
        args.append("--sandbox")
    args.extend(extra_args)
    if prompt:
        args.extend(["-p" if headless else "-i", prompt])
    return args


def run_agy(
    prompt: str | None = None,
    *,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    headless: bool = False,
    sandbox: bool = False,
    dry_run: bool = False,
    extra_args: Sequence[str] = (),
) -> int:
    """Route the prompt via Stage 1/2, set env vars, and launch agy with direct TTY inheritance."""
    cfg = load_config()
    cwd = Path.cwd()
    dirty_files, has_tests = _detect_git_state(cwd)

    remaining_quota: float | None = None
    conn = connect(cfg.db_path, init_schema=True)
    try:
        latest_quotas = get_latest_quota_snapshots(conn)
        active_fractions = [
            float(snap["remaining_fraction"])
            for snap in latest_quotas.values()
            if snap.get("remaining_fraction") is not None
        ]
        if active_fractions:
            remaining_quota = min(active_fractions)

        if model or effort or mode:
            decision = route_stage1(
                prompt or "",
                has_test_runner=has_tests,
                git_dirty_files=dirty_files,
                remaining_quota_fraction=remaining_quota,
                override_model=model,
                override_effort=effort,
                override_mode=mode,
            )
        else:
            decision = route_stage2_bandit(
                conn,
                prompt or "",
                has_test_runner=has_tests,
                git_dirty_files=dirty_files,
                remaining_quota_fraction=remaining_quota,
            )

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        with write_tx(conn):
            conn.execute(
                """
                INSERT INTO tasks (task_id, repo_root, title, category)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, str(cwd), (prompt or "interactive")[:120], decision.category),
            )
    finally:
        conn.close()

    settings_path: Path | None = (
        (cfg.agy_cli_dir / "settings.json") if cfg.profile_mode == "isolated" else None
    )
    agy_args = build_agy_args(
        decision,
        prompt=prompt,
        headless=headless,
        sandbox=sandbox,
        settings_path=settings_path,
        extra_args=extra_args,
    )

    session_meta = json.dumps(
        {
            "task_id": task_id,
            "stage": decision.stage,
            "category": decision.category,
            "model": decision.model_id,
            "effort": decision.effort,
            "mode": decision.mode,
            "profile_mode": cfg.profile_mode,
        },
        separators=(",", ":"),
    )
    env = build_agy_subprocess_env(
        cfg,
        extra_env={
            "OGY_TASK_ID": task_id,
            "OGY_SESSION_META": session_meta,
        },
    )

    if dry_run:
        print(f"[ogy run dry-run] agy {' '.join(agy_args)}")
        print(f"[ogy meta] {env['OGY_SESSION_META']}")
        return 0

    try:
        write_pending_task(
            cwd,
            pending_dir=cfg.stash_dir.parent / "pending_tasks",
            task_id=task_id,
            decision={
                "stage": decision.stage,
                "category": decision.category,
                "model": decision.model_id,
                "effort": decision.effort,
                "mode": decision.mode,
                "features": decision.features,
                "propensity": decision.propensity,
                "predicted_score": decision.predicted_score,
            },
            ttl_seconds=30.0,
        )
    except OSError:
        pass

    agy_bin = shutil.which("agy") or "agy"
    if os.name == "nt":
        try:
            proc = subprocess.run([agy_bin, *agy_args], env=env)
            return int(proc.returncode)
        except FileNotFoundError:
            print("[ogy run] `agy` executable not found in PATH.", file=sys.stderr)
            return 127
        except KeyboardInterrupt:
            return 130

    os.execvpe(agy_bin, [agy_bin, *agy_args], env)
    return 0
