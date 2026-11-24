"""Tests for fail-open hooks, statusline renderer, and stdlib-only hot path."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from ogy_core.config import OgyConfig
from ogy_hooks.cli import dispatch_hook, main as hooks_main
from ogy_statusline.render import render_statusline


def _cfg(tmp_path: Path, *, cap_usd: float | None = None) -> OgyConfig:
    return OgyConfig(
        config_path=tmp_path / "config.toml",
        db_path=tmp_path / "ogy.db",
        agy_cli_dir=tmp_path / "agy-cli",
        agy_config_dir=tmp_path / "agy-cfg",
        stash_dir=tmp_path / "stash",
        state_dir=tmp_path / "state",
        budget_hard_cap_usd=cap_usd,
    )


def test_hot_path_stdlib_only() -> None:
    # Verify importing ogy_hooks.cli and ogy_statusline.render in a fresh Python process
    # does not load mcp, pydantic, rich, or numpy.
    code = (
        "import sys; import ogy_hooks.cli, ogy_statusline.render; "
        "forbidden = {'mcp', 'pydantic', 'rich', 'numpy'} & set(sys.modules); "
        "assert not forbidden, f'Forbidden modules imported: {forbidden}'"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_hooks_fail_open_on_bad_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid json"))
    rc = hooks_main(["pre-tool"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"decision": ""}

    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid json"))
    rc2 = hooks_main(["post-tool"])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out.strip())
    assert out2 == {}


def test_session_start_handoff_and_stop_flow(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    handoff = tmp_path / ".ogy" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# Handoff\nNext: run tests", encoding="utf-8")

    r_start = dispatch_hook(
        "session-start",
        {"conversationId": "sess-1", "workspacePaths": [str(tmp_path)], "modelName": "gemini-4-neon"},
        cfg=cfg,
    )
    assert "injectSteps" in r_start
    assert not handoff.exists()
    assert (tmp_path / ".ogy" / "handoff.archived.md").is_file()

    # PreInvocation > 1 must always be {}
    assert dispatch_hook("pre-invocation", {"conversationId": "sess-1", "invocationNum": 2}, cfg=cfg) == {}

    # PostToolUse must always be {}
    assert (
        dispatch_hook(
            "post-tool",
            {
                "conversationId": "sess-1",
                "stepIdx": 1,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}},
            },
            cfg=cfg,
        )
        == {}
    )

    r_stop = dispatch_hook("stop", {"conversationId": "sess-1"}, cfg=cfg)
    assert r_stop == {"decision": "stop"}


def test_statusline_render_wide_and_narrow(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    payload = {
        "conversationId": "sess-sl",
        "modelName": "gemini-4-hydrogen",
        "usage": {"input_tokens": 5_000, "cache_read_input_tokens": 59_000, "output_tokens": 300},
        "context_window": {"used": 64_000, "max": 500_000},
        "cost": {"total_usd": 0.24},
        "quota": {"gemini-5h": {"remaining_fraction": 0.88}},
    }
    wide = render_statusline(payload, terminal_width=140, persist=False)
    assert "ogy | cache 92% | ctx 64K (13%) | $0.24 | 5h:88% | WARN" == wide

    narrow = render_statusline(payload, terminal_width=90, persist=False)
    assert narrow == "ogy | c:92% | 64K | $0.24 | 5h:88% | WARN"
