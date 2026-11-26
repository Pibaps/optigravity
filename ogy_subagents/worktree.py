"""Opt-in Level 3 isolated `git worktree` delegated edits (disabled by default).

Never mutates the main working tree:
1. Requires explicit opt-in (`subagents.worktree_enabled = true` or `opt_in=True`).
2. Creates a temporary detached `git worktree` at `HEAD`.
3. Executes the delegated edit runner inside that isolated worktree.
4. Exports a unified `.patch` file and returns a mandatory JSON contract
   (`files_touched`, `git_diff_stat`, `patch_path`, `requires_human_approval=True`)
   for human review before any application to the main repo.
5. Cleans up the temporary worktree reliably in `finally`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ogy_core.config import OgyConfig, load_config
from ogy_subagents.secrets import redact_secrets


class WorktreeDisabledError(PermissionError):
    """Raised when Level 3 worktree editing is invoked without explicit opt-in."""


@dataclass(frozen=True, slots=True)
class WorktreeEditContract:
    """Mandatory JSON contract returned by an isolated worktree edit run."""

    files_touched: list[str]
    git_diff_stat: str
    patch_path: str
    base_commit: str
    summary: str
    requires_human_approval: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _run_git(args: list[str], *, cwd: Path, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def run_isolated_worktree_edit(
    repo_root: Path | str,
    runner: Callable[[Path], str],
    *,
    cfg: OgyConfig | None = None,
    opt_in: bool = False,
    patch_dir: Path | None = None,
) -> WorktreeEditContract:
    """Execute `runner(worktree_path)` inside an isolated temporary `git worktree`.

    `runner` receives the temporary worktree directory and returns a short summary string.
    The main repository working tree is NEVER modified.
    """
    resolved_cfg = cfg or load_config()
    if not (opt_in or resolved_cfg.subagents_worktree_enabled):
        raise WorktreeDisabledError(
            "Level 3 isolated worktree editing is disabled by default. "
            "Enable explicitly with `ogy config set subagents.worktree_enabled true` or pass opt_in=True."
        )

    root = Path(repo_root).resolve()
    head_proc = _run_git(["rev-parse", "HEAD"], cwd=root)
    if head_proc.returncode != 0:
        raise RuntimeError(f"Not a valid git repository with HEAD at {root}: {head_proc.stderr.strip()}")
    base_commit = head_proc.stdout.strip()

    run_tag = uuid.uuid4().hex[:10]
    tmp_base = Path(tempfile.mkdtemp(prefix=f"ogy-wt-{run_tag}-"))
    wt_dir = tmp_base / "worktree"

    add_proc = _run_git(["worktree", "add", "--detach", str(wt_dir), base_commit], cwd=root)
    if add_proc.returncode != 0:
        shutil.rmtree(tmp_base, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {add_proc.stderr.strip()}")

    try:
        summary = redact_secrets(str(runner(wt_dir) or ""))

        # Stage untracked files with intent-to-add so `git diff` captures new files too
        _run_git(["add", "-N", "."], cwd=wt_dir)

        status_proc = _run_git(["status", "--porcelain"], cwd=wt_dir)
        files_touched: list[str] = []
        for line in status_proc.stdout.splitlines():
            if len(line) >= 4:
                path_part = line[3:].strip()
                if " -> " in path_part:
                    path_part = path_part.split(" -> ", 1)[1].strip()
                if path_part:
                    files_touched.append(path_part.replace("\\", "/"))

        stat_proc = _run_git(["diff", "--stat"], cwd=wt_dir)
        diff_stat = redact_secrets(stat_proc.stdout.strip())

        diff_proc = _run_git(["diff"], cwd=wt_dir)
        patch_content = redact_secrets(diff_proc.stdout)

        out_dir = patch_dir or (root / ".ogy" / "patches")
        out_dir.mkdir(parents=True, exist_ok=True)
        patch_file = out_dir / f"worktree-{run_tag}.patch"
        patch_file.write_text(patch_content, encoding="utf-8", newline="\n")

        return WorktreeEditContract(
            files_touched=sorted(set(files_touched)),
            git_diff_stat=diff_stat,
            patch_path=str(patch_file),
            base_commit=base_commit,
            summary=summary,
            requires_human_approval=True,
        )
    finally:
        _run_git(["worktree", "remove", "--force", str(wt_dir)], cwd=root)
        shutil.rmtree(tmp_base, ignore_errors=True)


def apply_approved_patch(repo_root: Path | str, patch_path: Path | str, *, check_only: bool = True) -> dict[str, Any]:
    """Validate (or apply after explicit human approval) a patch generated by `run_isolated_worktree_edit`."""
    root = Path(repo_root).resolve()
    pfile = Path(patch_path).resolve()
    if not pfile.is_file():
        return {"ok": False, "error": f"Patch file not found: {pfile}"}
    args = ["apply", "--check", str(pfile)] if check_only else ["apply", str(pfile)]
    proc = _run_git(args, cwd=root)
    return {
        "ok": proc.returncode == 0,
        "check_only": check_only,
        "stderr": redact_secrets(proc.stderr.strip()),
    }
