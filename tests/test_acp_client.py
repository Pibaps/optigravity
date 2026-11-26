"""Tests for V2 ACP v1 JSON-RPC 2.0 client and ACP telemetry persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import schema
from acp.exceptions import RequestError

from ogy_core.config import load_config
from ogy_core.storage import connect, query_all
from ogy_frontends.acp_tui.client import (
    AcpAuthError,
    AcpClient,
    AcpClientConfig,
    PermissionRequestEvent,
    UsageEvent,
    build_agy_acp_args,
)


class FakeAgentConn:
    """In-memory mock of `acp.Agent` for testing `AcpClient`."""

    def __init__(self, *, require_auth_once: bool = False, fail_auth: bool = False) -> None:
        self.require_auth_once = require_auth_once
        self.fail_auth = fail_auth
        self.authenticated = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.session_counter = 0

    async def initialize(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("initialize", kwargs))
        if self.require_auth_once and not self.authenticated:
            raise RequestError(-32000, "Unauthenticated")
        return SimpleNamespace(
            protocol_version=1,
            agent_capabilities={"session": {"fork": True}},
        )

    async def authenticate(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("authenticate", kwargs))
        if self.fail_auth:
            raise RuntimeError("Auth denied")
        self.authenticated = True
        return SimpleNamespace()

    async def new_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("new_session", kwargs))
        self.session_counter += 1
        return SimpleNamespace(
            session_id=f"acp-sess-{self.session_counter}",
            config_options=[
                SimpleNamespace(id="model", current_value="gemini-4-hydrogen"),
                SimpleNamespace(id="mode", current_value="auto"),
                SimpleNamespace(id="thinking_level", current_value="medium"),
            ],
        )

    async def resume_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("resume_session", kwargs))
        return SimpleNamespace(config_options=[SimpleNamespace(id="model", current_value="gemini-4-neon")])

    async def list_sessions(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("list_sessions", kwargs))
        return SimpleNamespace(
            sessions=[
                SimpleNamespace(session_id=f"acp-sess-{i}", title=f"Session {i}")
                for i in range(1, self.session_counter + 1)
            ]
        )

    async def close_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("close_session", kwargs))
        return SimpleNamespace()

    async def set_config_option(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("set_config_option", kwargs))
        return SimpleNamespace(
            config_options=[SimpleNamespace(id=kwargs["config_id"], current_value=kwargs["value"])]
        )

    async def prompt(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("prompt", kwargs))
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=schema.Usage(
                input_tokens=1200,
                cached_read_tokens=8800,
                cached_write_tokens=0,
                output_tokens=350,
                thought_tokens=420,
                total_tokens=10770,
            ),
        )

    async def cancel(self, **kwargs: Any) -> None:
        self.calls.append(("cancel", kwargs))


def test_build_agy_acp_args() -> None:
    cfg = AcpClientConfig(
        sandbox=True,
        add_dirs=["/tmp/extra"],
        agent="coder",
        process_per_session=True,
        log_file="/tmp/acp.log",
        disable_telemetry=True,
    )
    args = build_agy_acp_args(cfg)
    assert args[0] == "acp"
    assert "--sandbox" in args
    assert "--add-dir" in args and "/tmp/extra" in args
    assert "--agent" in args and "coder" in args
    assert "--process-per-session" in args
    assert "--log-file" in args and "/tmp/acp.log" in args
    assert "--disable-telemetry" in args


def test_acp_client_lifecycle_and_tos_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))
    ogy_cfg = load_config()

    events: list[Any] = []
    client = AcpClient(AcpClientConfig(cwd=str(tmp_path)), ogy_config=ogy_cfg, on_event=events.append)
    fake = FakeAgentConn(require_auth_once=True)

    async def _run() -> None:
        caps = await client.connect(agent_conn=fake)
        assert caps["session"]["fork"] is True
        init_calls = [c for name, c in fake.calls if name == "initialize"]
        assert init_calls[0]["client_info"].name == "optigravity"
        assert any(name == "authenticate" for name, _ in fake.calls)

        sid = await client.new_session()
        assert sid == "acp-sess-1"

        with pytest.raises(ValueError, match="Unsupported or banned ACP mode"):
            await client.set_config_option("mode", "yolo")

        await client.set_config_option("mode", "plan")
        await client.send_prompt("Explain architecture")
        await client.cancel_turn()
        await client.resume_session("acp-sess-1")
        assert any(name == "resume_session" for name, _ in fake.calls)
        assert not any(name == "load_session" for name, _ in fake.calls)
        await client.close_session(sid)

    asyncio.run(_run())

    # Verify ACP turn persisted with exact thought_tokens and data_source='acp'
    conn = connect(db_path, init_schema=True)
    try:
        turns = query_all(conn, "SELECT * FROM turns WHERE session_id = 'acp-sess-1'")
        assert len(turns) == 1
        t = dict(turns[0])
        assert t["data_source"] == "acp"
        assert t["thinking_tokens"] == 420
        assert t["thinking_is_estimated"] == 0
        assert t["cache_read_tokens"] == 8800

        sess = query_all(conn, "SELECT surface FROM sessions WHERE session_id = 'acp-sess-1'")
        assert sess[0]["surface"] == "cli_acp"
    finally:
        conn.close()

    assert any(isinstance(e, UsageEvent) and e.thought_tokens == 420 and e.is_turn_final for e in events)


def test_acp_client_auth_failure_raises_helpful_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    client = AcpClient(AcpClientConfig(cwd=str(tmp_path)), ogy_config=load_config())
    fake = FakeAgentConn(require_auth_once=True, fail_auth=True)

    async def _run() -> None:
        with pytest.raises(AcpAuthError, match="agy acp --login"):
            await client.connect(agent_conn=fake)

    asyncio.run(_run())


def test_acp_permission_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))

    async def _on_event(ev: Any) -> None:
        if isinstance(ev, PermissionRequestEvent):
            ev.future.set_result("allow_once")

    client = AcpClient(AcpClientConfig(cwd=str(tmp_path)), ogy_config=load_config(), on_event=_on_event)

    async def _run() -> None:
        resp = await client._handle_request_permission(
            "s1",
            schema.ToolCallUpdate(tool_call_id="tc1", title="Run pytest"),
            [schema.PermissionOption(option_id="allow_once", name="Allow once", kind="allow_once")],
        )
        assert isinstance(resp.outcome, schema.AllowedOutcome)
        assert resp.outcome.option_id == "allow_once"

    asyncio.run(_run())
