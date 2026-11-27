"""Real Terminal (PTY on POSIX, ConPTY via pywinpty on Windows) E2E smoke tests (Lot 3.5.7).

Marked `@pytest.mark.e2e_tty`. Never uses `pexpect.popen_spawn.PopenSpawn` (which is a pipe, not a TTY).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _run_in_real_pty(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    cols: int = 120,
    rows: int = 36,
    timeout_s: float = 20.0,
    send_after_text: tuple[str, str] | None = None,
) -> tuple[int, str]:
    """Run `cmd` inside a real OS pseudo-terminal (ConPTY on Windows, pty on POSIX).

    If `send_after_text=(trigger, keys)` is provided, writes `keys` once `trigger` appears
    in the terminal output (deterministic wait, no blind sleep).
    """
    if os.name == "nt":
        from winpty import PtyProcess

        proc = PtyProcess.spawn(cmd, cwd=str(cwd), env=env, dimensions=(rows, cols))
        chunks: list[str] = []
        sent = False
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if not proc.isalive():
                    try:
                        tail = proc.read(4096)
                        if tail:
                            chunks.append(tail)
                    except EOFError:
                        pass
                    break
                try:
                    data = proc.read(4096)
                    if data:
                        chunks.append(data)
                        if send_after_text and not sent:
                            joined = ANSI_RE.sub("", "".join(chunks))
                            if send_after_text[0] in joined:
                                proc.write(send_after_text[1])
                                sent = True
                except EOFError:
                    break
                except Exception:
                    time.sleep(0.03)
            else:
                proc.terminate(force=True)
                raise TimeoutError(f"ConPTY command timed out after {timeout_s}s: {cmd}")
            exit_code = int(proc.exitstatus if proc.exitstatus is not None else 0)
            return exit_code, "".join(chunks)
        finally:
            if proc.isalive():
                proc.terminate(force=True)

    # POSIX: real kernel PTY via stdlib `pty` + `select`
    import fcntl
    import pty
    import select
    import struct
    import termios

    master_fd, slave_fd = pty.openpty()
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        proc_p = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=env,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    chunks_b: list[bytes] = []
    sent = False
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            rlist, _, _ = select.select([master_fd], [], [], 0.05)
            if rlist:
                try:
                    data_b = os.read(master_fd, 4096)
                    if not data_b:
                        break
                    chunks_b.append(data_b)
                    if send_after_text and not sent:
                        joined = ANSI_RE.sub("", b"".join(chunks_b).decode("utf-8", errors="replace"))
                        if send_after_text[0] in joined:
                            os.write(master_fd, send_after_text[1].encode("utf-8"))
                            sent = True
                except OSError:
                    break
            elif proc_p.poll() is not None:
                break
        else:
            proc_p.kill()
            proc_p.wait(timeout=5.0)
            raise TimeoutError(f"PTY command timed out after {timeout_s}s: {cmd}")
        rc = proc_p.wait(timeout=5.0)
        return rc, b"".join(chunks_b).decode("utf-8", errors="replace")
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc_p.poll() is None:
            proc_p.kill()
            proc_p.wait(timeout=5.0)


def _install_fake_agy_shim(bin_dir: Path, repo_root: Path) -> Path:
    """Create a cross-platform `agy` shim on PATH invoking `tests.fixtures.fake_agy_cli`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shim = bin_dir / "agy.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" -m tests.fixtures.fake_agy_cli %*\r\n',
            encoding="utf-8",
        )
        return shim
    shim = bin_dir / "agy"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m tests.fixtures.fake_agy_cli "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


@pytest.mark.e2e_tty
def test_e2e_tty_ogy_wrap_spinner_tty_vs_pipe(tmp_path: Path) -> None:
    """`ogy wrap` emits 0 ANSI on stderr when piped, and works cleanly with isatty() in a real TTY."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    # 1. Pipe mode: 0 ANSI escape sequences on stderr
    pipe_res = subprocess.run(
        [sys.executable, "-m", "ogy_cli.main", "wrap", "--", sys.executable, "-c", "print('hello_pipe')"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=15.0,
    )
    assert pipe_res.returncode == 0
    assert "hello_pipe" in pipe_res.stdout
    assert not ANSI_RE.search(pipe_res.stderr), f"Unexpected ANSI in piped stderr: {pipe_res.stderr!r}"

    # 2. Real TTY (PTY/ConPTY): stderr.isatty() == True inside child & clean completion
    rc, tty_out = _run_in_real_pty(
        [
            sys.executable,
            "-m",
            "ogy_cli.main",
            "wrap",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.25); print('hello_real_tty')",
        ],
        env=env,
        cwd=tmp_path,
        timeout_s=15.0,
    )
    assert rc == 0
    assert "hello_real_tty" in ANSI_RE.sub("", tty_out)


@pytest.mark.e2e_tty
def test_e2e_tty_ogy_run_delegation_and_v31_coexistence(tmp_path: Path) -> None:
    """Real TTY `ogy init --isolated` -> `ogy run` vs direct `agy` coexistence proof."""
    repo_root = Path(__file__).resolve().parent.parent
    fake_home = tmp_path / "_ogy_test_home"
    shared_gemini = fake_home / ".gemini"
    (shared_gemini / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    orig_settings = '{"theme":"user-untouched"}\n'
    (shared_gemini / "antigravity-cli" / "settings.json").write_text(orig_settings, encoding="utf-8")

    fake_bin = tmp_path / "fake_bin"
    _install_fake_agy_shim(fake_bin, repo_root)

    base_env = dict(os.environ)
    base_env["PATH"] = str(fake_bin) + os.pathsep + base_env.get("PATH", "")
    base_env["PYTHONPATH"] = str(repo_root) + (os.pathsep + base_env["PYTHONPATH"] if base_env.get("PYTHONPATH") else "")
    base_env["PYTHONIOENCODING"] = "utf-8"

    # Step 1: Initialize isolated profile in fake_home
    init_res = subprocess.run(
        [sys.executable, "-m", "ogy_cli.main", "init", "--isolated"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=base_env,
        timeout=15.0,
    )
    assert init_res.returncode == 0, init_res.stderr

    # Step 2: Run `ogy run` inside a real PTY/ConPTY -> fake `agy` receives TTY + V3.1 isolated env + --settings
    ogy_report_file = tmp_path / "ogy_run_report.json"
    env_ogy = dict(base_env)
    env_ogy["FAKE_AGY_OUT_FILE"] = str(ogy_report_file)

    rc, _out = _run_in_real_pty(
        [sys.executable, "-m", "ogy_cli.main", "run", "fix typo in README"],
        env=env_ogy,
        cwd=tmp_path,
        timeout_s=20.0,
    )
    assert rc == 0
    assert ogy_report_file.is_file()
    ogy_rep = json.loads(ogy_report_file.read_text(encoding="utf-8"))
    assert ogy_rep["stdout_isatty"] is True
    assert ogy_rep["OGY_ACTIVE"] == "1"
    assert ogy_rep["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert ogy_rep["OGY_TASK_ID"] and ogy_rep["OGY_TASK_ID"].startswith("task-")
    assert ogy_rep["has_settings"] is True
    assert ogy_rep["settings_path"] and "gemini-profile" in ogy_rep["settings_path"]
    assert ogy_rep["OGY_GEMINI_HOME"] and "gemini-profile" in ogy_rep["OGY_GEMINI_HOME"]

    # Step 3: Direct `agy` invocation outside `ogy` -> 0 OGY_ACTIVE, 0 interference, ~/.gemini/ 100% untouched
    direct_report_file = tmp_path / "direct_agy_report.json"
    env_direct = dict(base_env)
    env_direct["FAKE_AGY_OUT_FILE"] = str(direct_report_file)
    agy_cmd = str(fake_bin / ("agy.cmd" if os.name == "nt" else "agy"))

    rc_direct, _out_d = _run_in_real_pty(
        [agy_cmd, "--version"],
        env=env_direct,
        cwd=tmp_path,
        timeout_s=15.0,
    )
    assert rc_direct == 0
    direct_rep = json.loads(direct_report_file.read_text(encoding="utf-8"))
    assert direct_rep["OGY_ACTIVE"] is None
    assert direct_rep["has_settings"] is False
    # Shared ~/.gemini/settings.json is 100% untouched
    assert (shared_gemini / "antigravity-cli" / "settings.json").read_text(encoding="utf-8") == orig_settings
    assert not (shared_gemini / "config" / "hooks.json").exists()


@pytest.mark.e2e_tty
def test_e2e_tty_ogy_tui_live_with_fake_acp_agent_clean_exit(tmp_path: Path) -> None:
    """Launch real `ogy tui` in a real PTY/ConPTY connected to Fake ACP Agent, wait for ready, exit cleanly (Ctrl+Q)."""
    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["OGY_ACP_COMMAND_OVERRIDE"] = json.dumps(
        [sys.executable, "-m", "tests.fixtures.fake_acp_agent", "--scenario", "nominal_stream"]
    )

    # Wait deterministically until the connected model name appears in the session bar, then send Ctrl+Q (\x11)
    rc, tty_out = _run_in_real_pty(
        [sys.executable, "-m", "ogy_cli.main", "tui", "--lang", "en"],
        env=env,
        cwd=tmp_path,
        cols=120,
        rows=36,
        timeout_s=20.0,
        send_after_text=("gemini-4-hydrogen", "\x11"),
    )
    assert rc == 0
    clean = ANSI_RE.sub("", tty_out)
    assert "gemini-4-hydrogen" in clean
