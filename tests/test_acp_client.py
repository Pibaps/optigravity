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
    build_agy_acp_env,
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

    async def fork_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("fork_session", kwargs))
        self.session_counter += 1
        return SimpleNamespace(
            session_id=f"acp-sess-{self.session_counter}",
            config_options=[
                SimpleNamespace(id="model", current_value="gemini-4-hydrogen"),
                SimpleNamespace(id="mode", current_value="auto"),
                SimpleNamespace(id="thinking_level", current_value="medium"),
            ],
        )

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


def test_v31_acp_env_and_command_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lot 3.1.1 / 3.1.3 / V3.5 prep: OGY_ACTIVE=1, isolated profile env, and command_override."""
    import acp

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OGY_GEMINI_HOME", raising=False)
    monkeypatch.delenv("OGY_REAL_HOME", raising=False)
    monkeypatch.delenv("OGY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))

    # 1. Shared mode injects OGY_ACTIVE=1 without overriding USERPROFILE/HOME
    cfg_shared = load_config()
    assert cfg_shared.profile_mode == "shared"
    env_shared = build_agy_acp_env(cfg_shared, base_env={"PATH": "/usr/bin"})
    assert env_shared["OGY_ACTIVE"] == "1"
    assert env_shared["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "OGY_GEMINI_HOME" not in env_shared
    assert "USERPROFILE" not in env_shared

    # 2. Isolated mode injects OGY_ACTIVE=1 + isolated USERPROFILE/HOME + real OGY_DB_PATH/OGY_CONFIG_PATH
    cfg_iso = load_config()
    cfg_iso.profile_mode = "isolated"
    iso_root = tmp_path / ".local" / "share" / "optigravity" / "gemini-profile"
    cfg_iso.isolated_profile_root = iso_root
    cfg_iso.gemini_home = iso_root / ".gemini"
    env_iso = build_agy_acp_env(cfg_iso, base_env={"PATH": "/usr/bin"})
    assert env_iso["OGY_ACTIVE"] == "1"
    assert env_iso["USERPROFILE"] == str(iso_root)
    assert env_iso["HOME"] == str(iso_root)
    assert env_iso["OGY_GEMINI_HOME"] == str(iso_root / ".gemini")
    assert env_iso["OGY_DB_PATH"] == str(cfg_iso.db_path)
    assert env_iso["OGY_CONFIG_PATH"] == str(cfg_iso.config_path)
    assert env_iso["OGY_REAL_HOME"] == str(tmp_path)

    # 3. command_override (V3.5 prep) spawns custom executable with full V3.1 env
    captured: dict[str, Any] = {}
    fake = FakeAgentConn()

    class _FakeSpawnCM:
        async def __aenter__(self) -> tuple[Any, Any]:
            return fake, SimpleNamespace(returncode=0)

        async def __aexit__(self, *args: Any) -> None:
            return None

    def _fake_spawn(bridge: Any, cmd: str, *args: str, **kwargs: Any) -> _FakeSpawnCM:
        captured["cmd"] = cmd
        captured["args"] = list(args)
        captured["env"] = kwargs.get("env")
        return _FakeSpawnCM()

    monkeypatch.setattr(acp, "spawn_agent_process", _fake_spawn)

    client = AcpClient(
        AcpClientConfig(
            cwd=str(tmp_path),
            command_override=["python", "-m", "fake_acp_agent", "--scenario", "v31_env_audit"],
        ),
        ogy_config=cfg_iso,
    )

    async def _run_override() -> None:
        await client.connect()
        await client.close()

    asyncio.run(_run_override())
    assert captured["cmd"] == "python"
    assert captured["args"] == ["-m", "fake_acp_agent", "--scenario", "v31_env_audit"]
    assert "--settings" not in captured["args"]
    assert captured["env"]["OGY_ACTIVE"] == "1"
    assert captured["env"]["USERPROFILE"] == str(iso_root)

    # 4. Default agy acp args never pass --settings (agy acp rejects it) in isolated or shared mode
    from ogy_frontends.acp_tui.client import build_agy_acp_args

    acp_args_iso = build_agy_acp_args(
        AcpClientConfig(cwd=str(tmp_path)),
        settings_path=iso_root / ".gemini" / "antigravity-cli" / "settings.json",
    )
    acp_args_shared = build_agy_acp_args(AcpClientConfig(cwd=str(tmp_path)))
    assert "--settings" not in acp_args_iso
    assert acp_args_iso == acp_args_shared
    assert acp_args_iso[0] == "acp"


@pytest.mark.asyncio
async def test_isolated_vs_shared_acp_stdio_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated mode must behave identically to shared mode over real stdio ACP JSON-RPC 2.0."""
    import json
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent

    real_home = tmp_path / "real_home"
    real_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: real_home)
    monkeypatch.delenv("OGY_GEMINI_HOME", raising=False)
    monkeypatch.delenv("OGY_REAL_HOME", raising=False)
    monkeypatch.delenv("OGY_CONFIG_PATH", raising=False)
    db_path = tmp_path / "parity.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

    results: dict[str, dict[str, Any]] = {}
    for mode in ("shared", "isolated"):
        cfg = load_config()
        cfg.profile_mode = mode
        if mode == "isolated":
            iso_root = real_home / ".local" / "share" / "optigravity" / "gemini-profile"
            cfg.isolated_profile_root = iso_root
            cfg.gemini_home = iso_root / ".gemini"
            cfg.agy_cli_dir = cfg.gemini_home / "antigravity-cli"
            cfg.agy_config_dir = cfg.gemini_home / "config"

        events: list[Any] = []
        client = AcpClient(
            AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("v31_env_audit")),
            ogy_config=cfg,
            on_event=events.append,
        )
        await client.connect()
        try:
            sess = await client.new_session()
            await client.send_prompt("audit env")
        finally:
            await client.close()

        msg_text = "".join(e.text for e in events if isinstance(e, AgentMessageEvent))
        audit = json.loads(msg_text)
        results[mode] = {
            "session_id": sess,
            "supports_load": client.supports_load,
            "audit": audit,
        }

    # Parity checks: both modes connect, create sessions, stream messages, and never pass --settings
    assert results["isolated"]["supports_load"] == results["shared"]["supports_load"]
    assert results["isolated"]["audit"]["has_settings_flag"] is False
    assert results["shared"]["audit"]["has_settings_flag"] is False
    assert results["isolated"]["audit"]["OGY_ACTIVE"] == "1"
    assert results["shared"]["audit"]["OGY_ACTIVE"] == "1"

    # Isolation proof: isolated mode redirects HOME/USERPROFILE/OGY_GEMINI_HOME while preserving DB/REAL_HOME
    iso_audit = results["isolated"]["audit"]
    expected_iso_root = str(real_home / ".local" / "share" / "optigravity" / "gemini-profile")
    assert iso_audit["USERPROFILE"] == expected_iso_root
    assert iso_audit["HOME"] == expected_iso_root
    assert iso_audit["OGY_GEMINI_HOME"] == str(Path(expected_iso_root) / ".gemini")
    assert iso_audit["OGY_REAL_HOME"] == str(real_home)
    assert iso_audit["OGY_DB_PATH"] == str(db_path)


# ---------------------------------------------------------------------------
# Phase 1 (Lot 3.5.1): Real stdio JSON-RPC 2.0 tests via Fake ACP Agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_acp_stdio_nominal_stream_and_db_persistence(tmp_path: Path) -> None:
    """Real stdio JSON-RPC 2.0 round-trip: thought -> grouped tools -> chunks -> Usage -> SQLite."""
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent, AgentThoughtEvent, ToolCallEvent

    ogy_cfg = load_config()
    events: list[Any] = []
    client = AcpClient(
        AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("nominal_stream")),
        ogy_config=ogy_cfg,
        on_event=events.append,
    )
    try:
        caps = await client.connect()
        assert caps
        sid = await client.new_session()
        assert sid == "fake-acp-1"
        resp = await client.send_prompt("Verify V3.1 contracts")
        assert resp.stop_reason == "end_turn"
    finally:
        await client.close()

    assert any(isinstance(e, AgentThoughtEvent) and "Inspecting" in e.text for e in events)
    assert any(isinstance(e, ToolCallEvent) and e.name == "read_file" and e.status == "completed" for e in events)
    assert any(isinstance(e, ToolCallEvent) and e.name == "grep_search" and e.status == "completed" for e in events)
    full_msg = "".join(e.text for e in events if isinstance(e, AgentMessageEvent))
    assert "Analysis complete." in full_msg
    assert any(isinstance(e, UsageEvent) and e.is_turn_final and e.thought_tokens == 420 for e in events)

    conn = connect(ogy_cfg.db_path, init_schema=True)
    try:
        rows = query_all(conn, "SELECT * FROM turns WHERE session_id = 'fake-acp-1'")
        assert len(rows) == 1
        assert rows[0]["data_source"] == "acp"
        assert rows[0]["thinking_tokens"] == 420
        assert rows[0]["cache_read_tokens"] == 8800
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fake_acp_stdio_edit_diff_and_permission_roundtrip(tmp_path: Path) -> None:
    """Real stdio edit diff + request_permission suspension until client resolves future."""
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent

    seen_perm: list[PermissionRequestEvent] = []
    messages: list[str] = []

    async def _on_event(ev: Any) -> None:
        if isinstance(ev, PermissionRequestEvent):
            seen_perm.append(ev)
            ev.future.set_result("allow_always")
        elif isinstance(ev, AgentMessageEvent):
            messages.append(ev.text)

    client = AcpClient(
        AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("edit_with_diff_and_permission")),
        ogy_config=load_config(),
        on_event=_on_event,
    )
    try:
        await client.connect()
        await client.new_session()
        await client.send_prompt("Apply edit")
    finally:
        await client.close()

    assert len(seen_perm) == 1
    assert any(blk.get("type") == "diff" and blk.get("path") == "app.py" for blk in seen_perm[0].content)
    assert "PERMISSION_DECISION:allow_always" in "".join(messages)


@pytest.mark.asyncio
async def test_fake_acp_stdio_elicitation_and_banned_yolo_mode(tmp_path: Path) -> None:
    """Real stdio form + url elicitations and strict rejection of banned 'yolo' mode."""
    import json
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent, ElicitationRequestEvent

    messages: list[str] = []
    seen_modes: list[str] = []

    async def _on_event(ev: Any) -> None:
        if isinstance(ev, ElicitationRequestEvent):
            seen_modes.append(ev.mode)
            if ev.mode == "form":
                ev.future.set_result({"action": "accept", "content": {"env": "prod", "dry_run": False}})
            else:
                ev.future.set_result({"action": "accept", "content": {}})
        elif isinstance(ev, AgentMessageEvent):
            messages.append(ev.text)

    client = AcpClient(
        AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("elicitation_and_tos")),
        ogy_config=load_config(),
        on_event=_on_event,
    )
    try:
        await client.connect()
        await client.new_session()
        with pytest.raises(ValueError, match="Unsupported or banned ACP mode"):
            await client.set_config_option("mode", "yolo")
        await client.send_prompt("Run elicitation")
    finally:
        await client.close()

    assert seen_modes == ["form", "url"]
    full = "".join(messages)
    assert "ELICITATION_RESULT:" in full
    payload = json.loads(full.split("ELICITATION_RESULT:", 1)[1])
    assert payload == {"form_action": "accept", "form_content": {"dry_run": False, "env": "prod"}, "url_action": "accept"}


@pytest.mark.asyncio
async def test_fake_acp_stdio_faults_and_cancel(tmp_path: Path) -> None:
    """Real stdio session/cancel interrupts a slow turn without killing the subprocess."""
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent, AgentThoughtEvent

    thought_seen = asyncio.Event()
    messages: list[str] = []

    async def _on_event(ev: Any) -> None:
        if isinstance(ev, AgentThoughtEvent):
            thought_seen.set()
        elif isinstance(ev, AgentMessageEvent):
            messages.append(ev.text)

    client = AcpClient(
        AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("faults_and_cancel")),
        ogy_config=load_config(),
        on_event=_on_event,
    )
    try:
        await client.connect()
        await client.new_session()
        prompt_task = asyncio.create_task(client.send_prompt("please run slow task"))
        await asyncio.wait_for(thought_seen.wait(), timeout=5.0)
        await client.cancel_turn()
        resp = await asyncio.wait_for(prompt_task, timeout=5.0)
        assert resp.stop_reason == "cancelled"
        assert "TURN_CANCELLED_CLEANLY" in "".join(messages)
        # Process is still alive for subsequent turns
        resp2 = await client.send_prompt("ping")
        assert resp2.stop_reason == "end_turn"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_mode", ["shared", "isolated"])
async def test_fake_acp_stdio_v31_env_audit(tmp_path: Path, profile_mode: str) -> None:
    """V3.1 env & --settings audit over real stdio in both 'shared' and 'isolated' modes."""
    import json
    from tests.conftest import fake_acp_command
    from ogy_frontends.acp_tui.client import AgentMessageEvent

    ogy_cfg = load_config()
    if profile_mode == "isolated":
        ogy_cfg.profile_mode = "isolated"
        iso_root = ogy_cfg.isolated_profile_root
        assert iso_root is not None
        ogy_cfg.gemini_home = iso_root / ".gemini"
        ogy_cfg.agy_cli_dir = ogy_cfg.gemini_home / "antigravity-cli"
        ogy_cfg.agy_config_dir = ogy_cfg.gemini_home / "config"

    messages: list[str] = []
    client = AcpClient(
        AcpClientConfig(cwd=str(tmp_path), command_override=fake_acp_command("v31_env_audit")),
        ogy_config=ogy_cfg,
        on_event=lambda ev: messages.append(ev.text) if isinstance(ev, AgentMessageEvent) else None,
    )
    try:
        await client.connect()
        await client.new_session()
        await client.send_prompt("audit")
    finally:
        await client.close()

    report = json.loads("".join(messages))
    assert report["OGY_ACTIVE"] == "1"
    assert report["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    if profile_mode == "isolated":
        # `agy acp` rejects `--settings` (only root `agy` accepts it); isolation is via env vars.
        assert report["has_settings_flag"] is False
        assert report["settings_arg"] is None
        assert report["OGY_GEMINI_HOME"] == str(ogy_cfg.gemini_home)
        assert report["USERPROFILE"] == str(ogy_cfg.isolated_profile_root)
        assert report["HOME"] == str(ogy_cfg.isolated_profile_root)
    else:
        assert report["has_settings_flag"] is False
        assert report["settings_arg"] is None
        assert report["OGY_GEMINI_HOME"] is None
