"""One-shot silent OAuth quota synchronization via official `agy` statusLine.

`agy acp` does not expose OAuth rolling quota buckets (`5h` / `weekly`) in ACP frames.
This module launches the unmodified official `agy` binary in a hidden PTY for ~2 seconds
with `statusLine` active (zero prompts sent, zero tokens consumed, ToS Section 6 compliant),
waits for `ogy-hook statusline` to persist a fresh `quota_snapshots` row in `ogy.db`,
and immediately terminates `agy`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ogy_core.config import OgyConfig, load_config
from ogy_core.profile import build_agy_subprocess_env
from ogy_core.storage import connect_readonly


def _max_quota_snapshot_id(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    try:
        conn = connect_readonly(db_path, timeout=0.25)
        try:
            row = conn.execute("SELECT COALESCE(MAX(snapshot_id), 0) FROM quota_snapshots").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _build_silent_sync_cmd(cfg: OgyConfig, agy_bin: str = "agy") -> list[str]:
    exe = shutil.which(agy_bin) or agy_bin
    cmd = [exe]
    if cfg.profile_mode == "isolated":
        settings_path = cfg.agy_cli_dir / "settings.json"
        if settings_path.is_file():
            cmd.extend(["--settings", str(settings_path)])
    return cmd


def sync_quotas_via_agy_statusline(
    cfg: OgyConfig | None = None,
    *,
    agy_bin: str = "agy",
    timeout_s: float = 12.0,
    poll_interval_s: float = 0.25,
    cwd: str | Path | None = None,
) -> bool:
    """Trigger a single silent `agy` startup in a hidden PTY to capture live `statusLine` quotas.

    Returns True if a new `quota_snapshots` row was persisted, False otherwise. Fail-open: never raises.
    """
    resolved = cfg or load_config()
    cmd = _build_silent_sync_cmd(resolved, agy_bin=agy_bin)
    if not shutil.which(cmd[0]) and not Path(cmd[0]).is_file():
        return False

    env = build_agy_subprocess_env(
        resolved,
        extra_env={"OGY_STATUSLINE_FORCE_PERSIST": "1"},
    )
    work_dir = str(cwd) if cwd else str(Path.cwd())
    before_max = _max_quota_snapshot_id(resolved.db_path)
    deadline = time.monotonic() + max(2.0, timeout_s)

    if sys.platform == "win32":
        return _sync_windows_pty(cmd, env=env, cwd=work_dir, db_path=resolved.db_path, before_max=before_max, deadline=deadline, poll_interval_s=poll_interval_s)
    return _sync_posix_pty(cmd, env=env, cwd=work_dir, db_path=resolved.db_path, before_max=before_max, deadline=deadline, poll_interval_s=poll_interval_s)


def _sync_windows_pty(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    db_path: Path,
    before_max: int,
    deadline: float,
    poll_interval_s: float,
) -> bool:
    try:
        from winpty import PtyProcess  # type: ignore[import-untyped]
    except Exception:
        return False

    proc: Any = None
    try:
        proc = PtyProcess.spawn(cmd, cwd=cwd, env=env, dimensions=(24, 120))
        while time.monotonic() < deadline:
            time.sleep(poll_interval_s)
            if _max_quota_snapshot_id(db_path) > before_max:
                return True
            if not proc.isalive():
                return _max_quota_snapshot_id(db_path) > before_max
        return _max_quota_snapshot_id(db_path) > before_max
    except Exception:
        return False
    finally:
        if proc is not None:
            try:
                proc.terminate(force=True)
            except Exception:
                pass


def _sync_posix_pty(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    db_path: Path,
    before_max: int,
    deadline: float,
    poll_interval_s: float,
) -> bool:
    try:
        import fcntl
        import pty
        import signal
        import struct
        import termios
    except Exception:
        return False

    master_fd: int | None = None
    slave_fd: int | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        # Set realistic terminal dimensions (24x120) so TUI frameworks render statusLine cleanly.
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        except OSError:
            pass
        try:
            os.set_blocking(master_fd, False)
        except OSError:
            pass

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = None
        while time.monotonic() < deadline:
            # Drain master PTY output so the kernel PTY buffer never fills and stalls `agy`.
            try:
                while os.read(master_fd, 4096):
                    pass
            except (BlockingIOError, OSError):
                pass
            time.sleep(poll_interval_s)
            if _max_quota_snapshot_id(db_path) > before_max:
                return True
            if proc.poll() is not None:
                return _max_quota_snapshot_id(db_path) > before_max
        return _max_quota_snapshot_id(db_path) > before_max
    except Exception:
        return False
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        for fd in (slave_fd, master_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
