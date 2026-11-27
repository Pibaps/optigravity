"""In-memory MCP SDK tests & real stdio executable contracts for `ogy-mcp` and `ogy-subagents` (Lot 3.5.3)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client

from ogy_core.config import load_config
from ogy_core.firebreaks import stash_wrapped_output
from ogy_core.storage import connect, ensure_session, list_active_subagent_runs, upsert_turn
from ogy_mcp.server import mcp as ogy_mcp_app
from ogy_subagents.router import ResilientGeminiRouter
from ogy_subagents.secrets import SecretResolution, resolve_api_key, save_secret_to_env_file
from ogy_subagents.server import SubagentsService, create_mcp_server
from tests.test_subagents_phase3 import (
    FAKE_KEY_TIER1,
    FakeCandidate,
    FakeContent,
    FakeGenaiClient,
    FakeMcpContext,
    FakePart,
    FakeResponse,
    FakeUsageMetadata,
)


def _resolve_server_cmd(script_name: str, module_name: str) -> list[str]:
    """Prefer installed console script (`ogy-mcp` / `ogy-subagents`), falling back to `python -m`."""
    exe = shutil.which(script_name)
    if exe:
        return [exe]
    return [sys.executable, "-m", module_name]


def _extract_tool_text(call_result: Any) -> str:
    content = getattr(call_result, "content", None) or []
    parts: list[str] = []
    for blk in content:
        txt = getattr(blk, "text", None)
        if isinstance(txt, str):
            parts.append(txt)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. In-memory MCP SDK v2 Client tests (`async with Client(app) as client:`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ogy_mcp_in_memory_tools_and_compact_schemas(tmp_path: Path) -> None:
    cfg = load_config()
    conn = connect(cfg.db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="mcp-mem-1", workspace_path=str(tmp_path))
        upsert_turn(
            conn,
            session_id="mcp-mem-1",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=4000,
            cache_read_tokens=16000,
            output_tokens=200,
            context_window_used=20000,
            estimated_cost_usd=0.05,
        )
    finally:
        conn.close()

    stashed = stash_wrapped_output(
        "line1\nline2\nline3\n",
        "",
        cmd="echo test",
        exit_code=0,
        duration_s=0.1,
        stash_dir=cfg.stash_dir,
    )

    async with Client(ogy_mcp_app) as client:
        tools_resp = await client.list_tools()
        names = {t.name for t in tools_resp.tools}
        assert names == {"ogy_session_budget", "ogy_recall", "ogy_write_handoff"}
        # Schemas remain compact (< 1500 bytes per tool)
        for tool in tools_resp.tools:
            schema_obj = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
            schema_bytes = len(json.dumps(schema_obj))
            assert schema_bytes < 1500, f"Tool {tool.name} schema too large: {schema_bytes}B"

        budget_res = await client.call_tool("ogy_session_budget", {"session_id": "mcp-mem-1"})
        budget = json.loads(_extract_tool_text(budget_res))
        assert budget["session_id"] == "mcp-mem-1"
        assert budget["active_context_tokens"] == 20000
        assert budget["zone"] == "green"

        recall_res = await client.call_tool(
            "ogy_recall",
            {"hash_prefix": stashed[:8], "start_line": 1, "end_line": 2},
        )
        assert "line1" in _extract_tool_text(recall_res)

        handoff_res = await client.call_tool(
            "ogy_write_handoff",
            {
                "goal": "Ship V3.5",
                "modified_files": ["tests/test_mcp_integration.py"],
                "checks": "pytest green",
                "negative_constraints": ["No host ~/.gemini writes"],
                "next_step": "Run full suite",
                "workspace_path": str(tmp_path),
            },
        )
        hdata = json.loads(_extract_tool_text(handoff_res))
        assert hdata["lines"] <= 30
        assert (tmp_path / ".ogy" / "handoff.md").is_file()


@pytest.mark.asyncio
async def test_ogy_subagents_in_memory_and_dual_progress_reporter(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def compute():\n    return 99\n", encoding="utf-8")
    cfg = load_config()

    fake_resp = FakeResponse(
        candidates=[FakeCandidate(FakeContent(role="model", parts=[FakePart(text="compute() returns 99 at sample.py:1")]))],
        usage_metadata=FakeUsageMetadata(prompt_token_count=80, candidates_token_count=15, thoughts_token_count=5),
    )

    router = ResilientGeminiRouter(
        privacy_mode="tier1_only",
        client_factory=lambda _key: FakeGenaiClient([fake_resp]),
        secret_resolver=lambda tier: SecretResolution(key=FAKE_KEY_TIER1, source="env:OGY_GEMINI_API_KEY_TIER1", tier=tier),
    )
    service = SubagentsService(
        cfg=cfg,
        router=router,
        use_sdk_types=False,
    )
    sub_mcp = create_mcp_server(service)

    async with Client(sub_mcp) as client:
        tools_resp = await client.list_tools()
        names = {t.name for t in tools_resp.tools}
        assert {"ogy_build_handoff", "ogy_research_repo", "ogy_summarize_stash", "ogy_triage_failure"}.issubset(names)

    # Also verify _DualProgressReporter directly (ctx.report_progress + SQLite subagent_runs)
    ctx = FakeMcpContext()
    res_text = await service.triage_failure(
        error_summary="AssertionError: expected 99",
        workspace_path=str(tmp_path),
        ctx=ctx,
    )
    assert "99" in res_text
    assert ctx.progress_events  # ctx.report_progress fired

    conn = connect(cfg.db_path, init_schema=True)
    try:
        rows = conn.execute("SELECT status, model_id FROM subagent_runs").fetchall()
        assert len(rows) >= 1
        assert rows[-1]["status"] == "completed"
        active = list_active_subagent_runs(conn)
        assert all(r["status"] == "completed" for r in active)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Real stdio executable guardrails: stdout purity & V3.1 env isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script_name", "module_name"),
    [
        ("ogy-mcp", "ogy_mcp.server"),
        ("ogy-subagents", "ogy_subagents.server"),
    ],
)
def test_real_stdio_mcp_stdout_purity_and_v31_env_isolation(
    tmp_path: Path,
    script_name: str,
    module_name: str,
) -> None:
    """Spawn real `ogy-mcp` / `ogy-subagents` with minimal env & isolated OGY_GEMINI_HOME.

    Verifies:
    - Every stdout line is a single-line valid JSON-RPC 2.0 object (0 stray prints/logs).
    - Minimal env + external cwd + isolated OGY_GEMINI_HOME resolves cleanly without touching host ~/.gemini/.
    """
    ext_cwd = tmp_path / "external_workspace"
    ext_cwd.mkdir()
    iso_home = tmp_path / "isolated_home"
    iso_gemini = iso_home / ".gemini"
    iso_gemini.mkdir(parents=True)
    cfg_path = iso_home / ".config" / "optigravity" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('[general]\nprofile_mode = "isolated"\n', encoding="utf-8")
    db_path = iso_home / ".local" / "share" / "optigravity" / "ogy.db"

    # Minimal environment: preserve only PATH/SYSTEMROOT/PATHEXT/PYTHONIOENCODING + isolated OGY_* vars
    minimal_env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(iso_home),
        "USERPROFILE": str(iso_home),
        "OGY_GEMINI_HOME": str(iso_gemini),
        "OGY_CONFIG_PATH": str(cfg_path),
        "OGY_DB_PATH": str(db_path),
        "PYTHONIOENCODING": "utf-8",
    }
    for win_key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if win_key in os.environ:
            minimal_env[win_key] = os.environ[win_key]

    init_req = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ogy-v35-test", "version": "3.5.0"},
            },
        }
    )
    initialized_notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    list_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    cmd = _resolve_server_cmd(script_name, module_name)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ext_cwd),
        env=minimal_env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(init_req + "\n")
        proc.stdin.flush()
        line1 = proc.stdout.readline()
        proc.stdin.write(initialized_notif + "\n" + list_req + "\n")
        proc.stdin.flush()
        line2 = proc.stdout.readline()
        proc.stdin.close()
        remaining = proc.stdout.read()
        stderr_out = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait(timeout=10.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

    assert rc == 0, f"{script_name} failed (rc={rc}): {stderr_out}"
    stdout_lines = [ln for ln in (line1 + line2 + remaining).splitlines() if ln.strip()]
    assert len(stdout_lines) >= 2, f"Expected >= 2 JSON-RPC lines, got: {stdout_lines!r}"

    parsed_by_id: dict[Any, dict[str, Any]] = {}
    for line in stdout_lines:
        obj = json.loads(line)
        assert isinstance(obj, dict), f"Non-object JSON line on stdout: {line!r}"
        assert obj.get("jsonrpc") == "2.0", f"Non-JSON-RPC line on stdout: {line!r}"
        if "id" in obj:
            parsed_by_id[obj["id"]] = obj

    assert 1 in parsed_by_id and "result" in parsed_by_id[1]
    assert 2 in parsed_by_id and "tools" in parsed_by_id[2]["result"]


def test_v31_secrets_cascade_env_keyring_secrets_env_in_isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify env -> keyring -> secrets.env cascade inside isolated HOME without touching host."""
    monkeypatch.delenv("OGY_GEMINI_API_KEY_TIER1", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    secrets_file = tmp_path / "_ogy_test_home" / ".config" / "optigravity" / "secrets.env"
    save_secret_to_env_file("OGY_GEMINI_API_KEY_TIER1", FAKE_KEY_TIER1, secrets_path=secrets_file)

    class _FakeKeyring:
        def __init__(self, value: str | None) -> None:
            self._value = value

        def get_password(self, _service: str, _name: str) -> str | None:
            return self._value

    # 1. Fallback to secrets.env when env & keyring are absent
    res_file = resolve_api_key("tier1", secrets_path=secrets_file, keyring_module=_FakeKeyring(None))
    assert res_file.key == FAKE_KEY_TIER1
    assert "secrets.env" in res_file.source

    # 2. Keyring overrides secrets.env
    res_kr = resolve_api_key("tier1", secrets_path=secrets_file, keyring_module=_FakeKeyring("AIza" + "K" * 35))
    assert res_kr.key == "AIza" + "K" * 35
    assert "keyring" in res_kr.source

    # 3. Env overrides keyring & secrets.env
    monkeypatch.setenv("OGY_GEMINI_API_KEY_TIER1", "AIza" + "E" * 35)
    res_env = resolve_api_key("tier1", secrets_path=secrets_file, keyring_module=_FakeKeyring("AIza" + "K" * 35))
    assert res_env.key == "AIza" + "E" * 35
    assert res_env.source.startswith("env:")
