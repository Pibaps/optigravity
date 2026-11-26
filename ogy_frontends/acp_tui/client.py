"""ACP v1 JSON-RPC 2.0 client over stdio (`agy acp`) using agent-client-protocol."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import acp
from acp import schema
from acp.exceptions import RequestError

from ogy_core import get_version
from ogy_core.config import OgyConfig, load_config
from ogy_core.ingest import ingest_acp_turn
from ogy_core.pricing import normalize_model_id
from ogy_core.storage import canonical_args_hash, connect, upsert_tool_call, utc_now_iso

CLIENT_NAME = "optigravity"
CLIENT_VERSION = get_version()

VALID_ACP_MODES = frozenset({"default", "accept-edits", "auto", "plan"})
BANNED_ACP_MODES = frozenset({"yolo"})


class AcpAuthError(RuntimeError):
    """Raised when `agy acp` requires authentication (`-32000`)."""


@dataclass(slots=True)
class AcpClientConfig:
    """Launch and protocol configuration for `agy acp`."""

    cwd: str = "."
    agy_bin: str = "agy"
    sandbox: bool = False
    add_dirs: list[str] = field(default_factory=list)
    agent: str | None = None
    process_per_session: bool = True
    log_file: str | None = None
    disable_telemetry: bool = True
    auth_method: str = "oauth-personal"


# ---------------------------------------------------------------------------
# Normalized UI/Orchestrator events emitted by AcpClient
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    session_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentMessageEvent:
    session_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentThoughtEvent:
    session_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    session_id: str
    tool_call_id: str
    name: str
    title: str
    status: str
    kind: str | None = None
    raw_input: Any = None
    raw_output: Any = None
    is_error: bool = False
    locations: list[Any] = field(default_factory=list)
    content: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UsageEvent:
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    total_tokens: int = 0
    context_used: int | None = None
    context_max: int | None = None
    cost_usd: float | None = None
    is_turn_final: bool = False


@dataclass(frozen=True, slots=True)
class ConfigUpdateEvent:
    session_id: str
    config_options: dict[str, str]


@dataclass(frozen=True, slots=True)
class PermissionRequestEvent:
    session_id: str
    tool_call_id: str
    tool_name: str
    title: str
    options: list[tuple[str, str, str]]  # (option_id, name, kind)
    future: asyncio.Future[str | None]
    kind: str | None = None
    raw_input: Any = None
    content: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ElicitationRequestEvent:
    session_id: str
    elicitation_id: str
    message: str
    future: asyncio.Future[dict[str, Any]]
    mode: str = "form"  # "form" or "url"
    requested_schema: dict[str, Any] | None = None
    url: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ElicitationCompleteEvent:
    session_id: str
    elicitation_id: str


@dataclass(frozen=True, slots=True)
class SessionNoticeEvent:
    session_id: str
    level: str
    message: str


AcpEvent = (
    UserMessageEvent
    | AgentMessageEvent
    | AgentThoughtEvent
    | ToolCallEvent
    | UsageEvent
    | ConfigUpdateEvent
    | PermissionRequestEvent
    | ElicitationRequestEvent
    | ElicitationCompleteEvent
    | SessionNoticeEvent
)


def build_agy_acp_args(cfg: AcpClientConfig) -> list[str]:
    """Build official `agy acp` CLI flags."""
    args: list[str] = ["acp"]
    if cfg.sandbox:
        args.append("--sandbox")
    for d in cfg.add_dirs:
        if d:
            args.extend(["--add-dir", str(d)])
    if cfg.agent:
        args.extend(["--agent", cfg.agent])
    if cfg.process_per_session:
        args.append("--process-per-session")
    if cfg.log_file:
        args.extend(["--log-file", cfg.log_file])
    if cfg.disable_telemetry:
        args.append("--disable-telemetry")
    return args


def _extract_text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return ""


def _parse_config_options(options: Any) -> dict[str, str]:
    resolved: dict[str, str] = {}
    if not options:
        return resolved
    for opt in options:
        opt_id = getattr(opt, "id", None) or (opt.get("id") if isinstance(opt, dict) else None)
        val = getattr(opt, "current_value", None)
        if val is None and isinstance(opt, dict):
            val = opt.get("currentValue") or opt.get("current_value")
        if opt_id and val is not None:
            resolved[str(opt_id)] = str(val)
    return resolved


def _parse_config_choices(options: Any) -> dict[str, list[str]]:
    """Extract selectable values for each ACP SessionConfigOption when advertised."""
    choices: dict[str, list[str]] = {}
    if not options:
        return choices
    for opt in options:
        opt_id = getattr(opt, "id", None) or (opt.get("id") if isinstance(opt, dict) else None)
        if not opt_id:
            continue
        raw_opts = getattr(opt, "options", None)
        if raw_opts is None and isinstance(opt, dict):
            raw_opts = opt.get("options")
        values: list[str] = []
        if isinstance(raw_opts, list):
            for entry in raw_opts:
                nested = getattr(entry, "options", None)
                if nested is None and isinstance(entry, dict):
                    nested = entry.get("options")
                if isinstance(nested, list):
                    for sub in nested:
                        v = getattr(sub, "value", None) or (sub.get("value") if isinstance(sub, dict) else None)
                        if v is not None and str(v) not in values:
                            values.append(str(v))
                else:
                    v = getattr(entry, "value", None) or (entry.get("value") if isinstance(entry, dict) else None)
                    if v is not None and str(v) not in values:
                        values.append(str(v))
        if values:
            choices[str(opt_id)] = values
    return choices


class _BridgeClient(acp.Client):
    """Internal `acp.Client` implementation forwarding callbacks to `AcpClient`."""

    def __init__(self, owner: AcpClient) -> None:
        super().__init__()
        self._owner = owner

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        await self._owner._handle_session_update(session_id, update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        return await self._owner._handle_request_permission(session_id, tool_call, options)

    async def create_elicitation(
        self,
        message: str,
        mode: Any,
        **kwargs: Any,
    ) -> Any:
        session_id = str(
            getattr(mode, "session_id", None)
            or kwargs.get("session_id")
            or self._owner.active_session_id
            or ""
        )
        elicitation_id = str(
            getattr(mode, "elicitation_id", None)
            or kwargs.get("elicitation_id")
            or ""
        )
        tool_call_id = getattr(mode, "tool_call_id", None)
        url = getattr(mode, "url", None)
        req_schema_raw = getattr(mode, "requested_schema", None)
        if req_schema_raw is None and isinstance(mode, dict):
            req_schema_raw = mode.get("requested_schema") or mode.get("requestedSchema")
            url = url or mode.get("url")
            elicitation_id = elicitation_id or str(mode.get("elicitation_id") or mode.get("elicitationId") or "")
        req_schema: dict[str, Any] | None = None
        if req_schema_raw is not None:
            if hasattr(req_schema_raw, "model_dump"):
                req_schema = req_schema_raw.model_dump()
            elif isinstance(req_schema_raw, dict):
                req_schema = dict(req_schema_raw)
        mode_name = "url" if url else "form"
        return await self._owner._handle_create_elicitation(
            session_id,
            elicitation_id,
            message,
            mode=mode_name,
            requested_schema=req_schema,
            url=str(url) if url else None,
            tool_call_id=str(tool_call_id) if tool_call_id else None,
        )

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or self._owner.active_session_id or "")
        await self._owner._emit(ElicitationCompleteEvent(session_id=session_id, elicitation_id=str(elicitation_id)))


class AcpClient:
    """High-level ACP v1 client managing `agy acp` lifecycle, sessions, steering, and telemetry."""

    def __init__(
        self,
        config: AcpClientConfig | None = None,
        *,
        ogy_config: OgyConfig | None = None,
        on_event: Callable[[AcpEvent], Awaitable[None] | None] | None = None,
    ) -> None:
        self.config = config or AcpClientConfig()
        self.ogy_config = ogy_config or load_config()
        self._on_event = on_event

        self._exit_stack: AsyncExitStack | None = None
        self._conn: acp.Agent | None = None
        self._process: Any = None

        self.server_capabilities: dict[str, Any] = {}
        self.supports_fork: bool = False
        self.supports_load: bool = False
        self.active_session_id: str | None = None
        self.session_configs: dict[str, dict[str, str]] = {}
        self.session_config_choices: dict[str, dict[str, list[str]]] = {}
        self.session_steps: dict[str, int] = {}
        self._last_turn_ts: dict[str, float] = {}
        self._tool_call_names: dict[str, str] = {}
        self._replaying_sessions: set[str] = set()

    def get_config_choices(self, config_id: str, session_id: str | None = None) -> list[str]:
        """Return advertised selectable values for a config option (if known)."""
        sid = session_id or self.active_session_id
        if not sid:
            return []
        choices = self.session_config_choices.get(sid, {})
        if config_id in choices:
            return list(choices[config_id])
        if config_id == "thinking_level" and "thought_level" in choices:
            return list(choices["thought_level"])
        return []

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def _emit(self, event: AcpEvent) -> None:
        if self._on_event is None:
            return
        res = self._on_event(event)
        if asyncio.iscoroutine(res):
            await res

    async def connect(self, *, agent_conn: acp.Agent | None = None) -> dict[str, Any]:
        """Spawn `agy acp` (or attach an injected `acp.Agent` for testing) and run `initialize`."""
        if agent_conn is not None:
            self._conn = agent_conn
        else:
            self._exit_stack = AsyncExitStack()
            env = os.environ.copy()
            env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
            args = build_agy_acp_args(self.config)
            bridge = _BridgeClient(self)
            agy_bin = shutil.which(self.config.agy_bin) or self.config.agy_bin
            conn, proc = await self._exit_stack.enter_async_context(
                acp.spawn_agent_process(
                    bridge,
                    agy_bin,
                    *args,
                    env=env,
                    cwd=self.config.cwd,
                )
            )
            self._conn = conn
            self._process = proc

        return await self._initialize_with_auth()

    async def _initialize_with_auth(self) -> dict[str, Any]:
        assert self._conn is not None
        elicit_kwargs: dict[str, Any] = {"form": schema.ElicitationFormCapabilities()}
        if hasattr(schema, "ElicitationUrlCapabilities"):
            elicit_kwargs["url"] = schema.ElicitationUrlCapabilities()
        client_caps = schema.ClientCapabilities(
            elicitation=schema.ElicitationCapabilities(**elicit_kwargs)
        )
        client_info = schema.Implementation(name=CLIENT_NAME, version=CLIENT_VERSION)
        try:
            resp = await self._conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=client_caps,
                client_info=client_info,
            )
        except RequestError as exc:
            if exc.code == -32000:
                await self._authenticate_or_raise()
                resp = await self._conn.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=client_caps,
                    client_info=client_info,
                )
            else:
                raise

        caps = getattr(resp, "agent_capabilities", None)
        caps_dict = caps.model_dump() if hasattr(caps, "model_dump") else (caps if isinstance(caps, dict) else {})
        self.server_capabilities = caps_dict
        session_caps = (
            caps_dict.get("session") or caps_dict.get("session_capabilities") or caps_dict.get("sessionCapabilities")
            if isinstance(caps_dict, dict)
            else None
        )
        self.supports_fork = bool(isinstance(session_caps, dict) and session_caps.get("fork"))
        self.supports_load = bool(
            isinstance(caps_dict, dict) and (caps_dict.get("load_session") or caps_dict.get("loadSession"))
        )
        return caps_dict

    async def _authenticate_or_raise(self) -> None:
        assert self._conn is not None
        try:
            await self._conn.authenticate(method_id=self.config.auth_method)
        except Exception as exc:
            raise AcpAuthError(
                "Authentication required (-32000). Run `agy acp --login` or check your OAuth session."
            ) from exc

    async def close(self) -> None:
        """Close the `agy acp` subprocess cleanly."""
        self._conn = None
        self._process = None
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    # ------------------------------------------------------------------
    # Session lifecycle (always session/resume, never session/load)
    # ------------------------------------------------------------------

    async def new_session(self, cwd: str | None = None) -> str:
        assert self._conn is not None
        target_cwd = str(Path(cwd or self.config.cwd).resolve())
        try:
            resp = await self._conn.new_session(cwd=target_cwd)
        except RequestError as exc:
            if exc.code == -32000:
                await self._authenticate_or_raise()
                resp = await self._conn.new_session(cwd=target_cwd)
            else:
                raise
        sid = str(resp.session_id)
        self.active_session_id = sid
        raw_opts = getattr(resp, "config_options", None)
        self.session_configs[sid] = _parse_config_options(raw_opts)
        parsed_choices = _parse_config_choices(raw_opts)
        if parsed_choices:
            self.session_config_choices[sid] = parsed_choices
        self.session_steps.setdefault(sid, 0)
        if self.session_configs[sid]:
            await self._emit(ConfigUpdateEvent(sid, dict(self.session_configs[sid])))
        return sid

    async def list_sessions(self, cwd: str | None = None) -> list[dict[str, Any]]:
        assert self._conn is not None
        resp = await self._conn.list_sessions(cwd=cwd or str(Path(self.config.cwd).resolve()))
        sessions = getattr(resp, "sessions", []) or []
        out: list[dict[str, Any]] = []
        for s in sessions:
            if hasattr(s, "model_dump"):
                out.append(s.model_dump())
            elif isinstance(s, dict):
                out.append(s)
            else:
                out.append({"session_id": str(getattr(s, "session_id", ""))})
        return out

    async def resume_session(self, session_id: str, cwd: str | None = None) -> str:
        """Resume an existing session via `session/resume` (never `session/load`)."""
        assert self._conn is not None
        target_cwd = str(Path(cwd or self.config.cwd).resolve())
        resp = await self._conn.resume_session(session_id=session_id, cwd=target_cwd)
        self.active_session_id = session_id
        raw_opts = getattr(resp, "config_options", None)
        cfg = _parse_config_options(raw_opts)
        parsed_choices = _parse_config_choices(raw_opts)
        if parsed_choices:
            self.session_config_choices[session_id] = parsed_choices
        if cfg:
            self.session_configs[session_id] = cfg
            await self._emit(ConfigUpdateEvent(session_id, dict(cfg)))
        return session_id

    async def load_session(self, session_id: str, cwd: str | None = None) -> str:
        """Load an existing session via `session/load` so ACP replays its prior transcript history."""
        assert self._conn is not None
        target_cwd = str(Path(cwd or self.config.cwd).resolve())
        self._replaying_sessions.add(session_id)
        try:
            resp = await self._conn.load_session(session_id=session_id, cwd=target_cwd)
        finally:
            self._replaying_sessions.discard(session_id)
        self.active_session_id = session_id
        raw_opts = getattr(resp, "config_options", None) if resp is not None else None
        cfg = _parse_config_options(raw_opts)
        parsed_choices = _parse_config_choices(raw_opts)
        if parsed_choices:
            self.session_config_choices[session_id] = parsed_choices
        if cfg:
            self.session_configs[session_id] = cfg
            await self._emit(ConfigUpdateEvent(session_id, dict(cfg)))
        return session_id

    async def close_session(self, session_id: str) -> None:
        assert self._conn is not None
        await self._conn.close_session(session_id=session_id)
        if self.active_session_id == session_id:
            self.active_session_id = None

    async def fork_session(self, session_id: str, cwd: str | None = None) -> str | None:
        assert self._conn is not None
        if not self.supports_fork:
            return None
        target_cwd = str(Path(cwd or self.config.cwd).resolve())
        resp = await self._conn.fork_session(session_id=session_id, cwd=target_cwd)
        new_sid = str(resp.session_id)
        self.active_session_id = new_sid
        self.session_configs[new_sid] = _parse_config_options(getattr(resp, "config_options", None))
        return new_sid

    # ------------------------------------------------------------------
    # Dynamic configuration (`session/set_config_option` exclusively)
    # ------------------------------------------------------------------

    async def set_config_option(self, config_id: str, value: str, *, session_id: str | None = None) -> dict[str, str]:
        """Set `model`, `mode`, or `thinking_level`/`thought_level` via `session/set_config_option`."""
        assert self._conn is not None
        sid = session_id or self.active_session_id
        if not sid:
            raise ValueError("No active ACP session.")
        if config_id == "mode":
            norm_mode = value.strip().lower()
            if norm_mode in BANNED_ACP_MODES or norm_mode not in VALID_ACP_MODES:
                raise ValueError(f"Unsupported or banned ACP mode {value!r}; allowed: {sorted(VALID_ACP_MODES)}")
            value = norm_mode

        resp = await self._conn.set_config_option(config_id=config_id, session_id=sid, value=value)
        updated = _parse_config_options(getattr(resp, "config_options", None))
        if not updated:
            updated = dict(self.session_configs.get(sid, {}))
            updated[config_id] = value
        self.session_configs[sid] = updated
        await self._emit(ConfigUpdateEvent(sid, dict(updated)))
        return updated

    # ------------------------------------------------------------------
    # Prompting, concurrent steering & Ctrl+C cancellation
    # ------------------------------------------------------------------

    async def send_prompt(
        self,
        text: str,
        *,
        session_id: str | None = None,
        extra_blocks: list[Any] | None = None,
    ) -> schema.PromptResponse:
        """Send `session/prompt` (concurrent calls during an active turn enable steering)."""
        assert self._conn is not None
        sid = session_id or self.active_session_id
        if not sid:
            raise ValueError("No active ACP session.")

        blocks: list[Any] = [schema.TextContentBlock(type="text", text=text)]
        if extra_blocks:
            blocks.extend(extra_blocks)

        now = time.monotonic()
        prev = self._last_turn_ts.get(sid)
        gap_s = (now - prev) if prev is not None else None
        self._last_turn_ts[sid] = now

        resp = await self._conn.prompt(session_id=sid, prompt=blocks)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            await self._record_final_usage(sid, usage, gap_s=gap_s)
        return resp

    async def cancel_turn(self, *, session_id: str | None = None) -> None:
        """Send `session/cancel` to abort the current turn without killing `agy acp`."""
        if self._conn is None:
            return
        sid = session_id or self.active_session_id
        if not sid:
            return
        await self._conn.cancel(session_id=sid)

    # ------------------------------------------------------------------
    # Inbound notifications & interactive permission / elicitation hooks
    # ------------------------------------------------------------------

    async def _handle_session_update(self, session_id: str, update: Any) -> None:
        kind = getattr(update, "session_update", None)
        if kind == "user_message_chunk":
            text = _extract_text_from_content(getattr(update, "content", None))
            if text:
                await self._emit(UserMessageEvent(session_id, text, getattr(update, "message_id", None)))
        elif kind == "agent_message_chunk":
            text = _extract_text_from_content(getattr(update, "content", None))
            if text:
                await self._emit(AgentMessageEvent(session_id, text, getattr(update, "message_id", None)))
        elif kind == "agent_thought_chunk":
            text = _extract_text_from_content(getattr(update, "content", None))
            if text:
                await self._emit(AgentThoughtEvent(session_id, text, getattr(update, "message_id", None)))
        elif kind in ("tool_call", "tool_call_update"):
            tc_id = str(getattr(update, "tool_call_id", ""))
            name = str(getattr(update, "name", None) or self._tool_call_names.get(tc_id) or "tool")
            if tc_id and name:
                self._tool_call_names[tc_id] = name
            title = str(getattr(update, "title", None) or name)
            status = str(getattr(update, "status", None) or ("running" if kind == "tool_call" else "updated"))
            is_err = status in ("failed", "error")
            raw_in = getattr(update, "raw_input", None)
            raw_out = getattr(update, "raw_output", None)
            raw_locs = getattr(update, "locations", None) or []
            raw_content = getattr(update, "content", None) or []
            locs = [
                loc.model_dump() if hasattr(loc, "model_dump") else loc
                for loc in (raw_locs if isinstance(raw_locs, list) else [raw_locs])
            ]
            content_blocks = [
                blk.model_dump() if hasattr(blk, "model_dump") else blk
                for blk in (raw_content if isinstance(raw_content, list) else [raw_content])
            ]
            await self._emit(
                ToolCallEvent(
                    session_id=session_id,
                    tool_call_id=tc_id,
                    name=name,
                    title=title,
                    status=status,
                    kind=getattr(update, "kind", None),
                    raw_input=raw_in,
                    raw_output=raw_out,
                    is_error=is_err,
                    locations=locs,
                    content=content_blocks,
                )
            )
            if status in ("completed", "failed", "error") and session_id not in self._replaying_sessions:
                self._persist_tool_call(session_id, name, raw_in, raw_out, is_err)
        elif kind == "usage_update":
            used = int(getattr(update, "used", 0) or 0)
            size = int(getattr(update, "size", 0) or 0)
            cost_obj = getattr(update, "cost", None)
            cost_val = float(getattr(cost_obj, "amount", 0.0)) if cost_obj is not None else None
            await self._emit(
                UsageEvent(
                    session_id=session_id,
                    context_used=used,
                    context_max=size or None,
                    cost_usd=cost_val,
                    is_turn_final=False,
                )
            )
        elif kind == "config_option_update":
            raw_opts = getattr(update, "config_options", None)
            cfg = _parse_config_options(raw_opts)
            parsed_choices = _parse_config_choices(raw_opts)
            if parsed_choices:
                self.session_config_choices[session_id] = parsed_choices
            self.session_configs[session_id] = cfg
            await self._emit(ConfigUpdateEvent(session_id, dict(cfg)))

    async def _handle_request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
    ) -> schema.RequestPermissionResponse:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        opts = [(str(o.option_id), str(o.name), str(o.kind)) for o in options]
        tc_id = str(getattr(tool_call, "tool_call_id", ""))
        tool_name = str(getattr(tool_call, "name", None) or getattr(tool_call, "title", None) or "tool")
        title = str(getattr(tool_call, "title", None) or tool_name)
        kind = getattr(tool_call, "kind", None)
        raw_in = getattr(tool_call, "raw_input", None)
        raw_content = getattr(tool_call, "content", None) or []
        content_blocks = [
            blk.model_dump() if hasattr(blk, "model_dump") else blk
            for blk in (raw_content if isinstance(raw_content, list) else [raw_content])
        ]

        if self._on_event is None:
            # No UI attached: fail safe by rejecting once
            return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))

        await self._emit(
            PermissionRequestEvent(
                session_id=session_id,
                tool_call_id=tc_id,
                tool_name=tool_name,
                title=title,
                options=opts,
                future=fut,
                kind=str(kind) if kind is not None else None,
                raw_input=raw_in,
                content=content_blocks,
            )
        )
        chosen_option = await fut
        if not chosen_option:
            return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(option_id=chosen_option, outcome="selected")
        )

    async def _handle_create_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        message: str,
        *,
        mode: str = "form",
        requested_schema: dict[str, Any] | None = None,
        url: str | None = None,
        tool_call_id: str | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        if self._on_event is None:
            return schema.DeclineElicitationResponse(action="decline")
        await self._emit(
            ElicitationRequestEvent(
                session_id=session_id,
                elicitation_id=elicitation_id,
                message=message,
                future=fut,
                mode=mode,
                requested_schema=requested_schema,
                url=url,
                tool_call_id=tool_call_id,
            )
        )
        result = await fut
        action = result.get("action", "decline")
        if action == "accept":
            return schema.AcceptElicitationResponse(action="accept", content=result.get("content", {}))
        if action == "cancel":
            return schema.CancelElicitationResponse(action="cancel")
        return schema.DeclineElicitationResponse(action="decline")

    async def _record_final_usage(self, session_id: str, usage: schema.Usage, *, gap_s: float | None) -> None:
        step = self.session_steps.get(session_id, 0) + 1
        self.session_steps[session_id] = step

        cfg = self.session_configs.get(session_id, {})
        model_id = normalize_model_id(cfg.get("model") or "gemini-4-hydrogen")
        effort = cfg.get("thinking_level") or cfg.get("thought_level")
        mode = cfg.get("mode")

        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        think_tok = int(getattr(usage, "thought_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cached_read_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cached_write_tokens", 0) or 0)
        total_tok = int(getattr(usage, "total_tokens", 0) or (in_tok + out_tok + think_tok + cache_read + cache_write))

        conn = connect(self.ogy_config.db_path, init_schema=True)
        try:
            est_cost = ingest_acp_turn(
                conn,
                session_id=session_id,
                step_index=step,
                workspace_path=self.config.cwd,
                model_id=model_id,
                effort_level=effort,
                execution_mode=mode,
                input_tokens=in_tok,
                cached_read_tokens=cache_read,
                cached_write_tokens=cache_write,
                output_tokens=out_tok,
                thought_tokens=think_tok,
                gap_since_prev_turn_s=gap_s,
                enterprise_cache=self.ogy_config.enterprise_cache_pricing,
            )
        finally:
            conn.close()

        await self._emit(
            UsageEvent(
                session_id=session_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                thought_tokens=think_tok,
                cached_read_tokens=cache_read,
                cached_write_tokens=cache_write,
                total_tokens=total_tok,
                context_used=in_tok + cache_read + cache_write,
                cost_usd=est_cost,
                is_turn_final=True,
            )
        )

    def _persist_tool_call(
        self,
        session_id: str,
        name: str,
        raw_input: Any,
        raw_output: Any,
        is_error: bool,
    ) -> None:
        step = self.session_steps.get(session_id, 0)
        args_dict = raw_input if isinstance(raw_input, dict) else {"raw": str(raw_input or "")}
        args_hash = canonical_args_hash(args_dict)
        raw_out_bytes = len(str(raw_output or "").encode("utf-8", errors="replace"))
        conn = connect(self.ogy_config.db_path, init_schema=True)
        try:
            upsert_tool_call(
                conn,
                session_id=session_id,
                step_index=step,
                tool_name=name,
                args_hash=args_hash,
                raw_output_bytes=raw_out_bytes,
                context_output_bytes=raw_out_bytes,
                is_error=1 if is_error else 0,
                created_at=utc_now_iso(),
            )
        finally:
            conn.close()
