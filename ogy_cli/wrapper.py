"""Level 1 CLI wrapper for agy without PTY proxy (Windows & POSIX safe)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Sequence

from ogy_core.config import load_config
from ogy_core.router import RouteDecision, route_stage1, route_stage2_bandit
from ogy_core.storage import connect, query_all, write_tx


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
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Build official agy CLI flags (never --settings, --permission-mode, or -m)."""
    args: list[str] = []
    if decision.model_id:
        args.extend(["--model", decision.model_id])
    if decision.effort and decision.model_id.lower() != "orchestrator":
        args.extend(["--effort", decision.effort])
    if decision.mode:
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
        qrows = query_all(
            conn,
            "SELECT MIN(remaining_fraction) AS min_q FROM quota_snapshots WHERE remaining_fraction IS NOT NULL",
        )
        if qrows and qrows[0]["min_q"] is not None:
            remaining_quota = float(qrows[0]["min_q"])

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

    agy_args = build_agy_args(
        decision,
        prompt=prompt,
        headless=headless,
        sandbox=sandbox,
        extra_args=extra_args,
    )

    env = os.environ.copy()
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["OGY_TASK_ID"] = task_id
    env["OGY_SESSION_META"] = json.dumps(
        {
            "task_id": task_id,
            "stage": decision.stage,
            "category": decision.category,
            "model": decision.model_id,
            "effort": decision.effort,
            "mode": decision.mode,
        },
        separators=(",", ":"),
    )

    if dry_run:
        print(f"[ogy run dry-run] agy {' '.join(agy_args)}")
        print(f"[ogy meta] {env['OGY_SESSION_META']}")
        return 0

    if os.name == "nt":
        proc = subprocess.run(["agy", *agy_args], env=env)
        return int(proc.returncode)

    os.execvpe("agy", ["agy", *agy_args], env)
    return 0
