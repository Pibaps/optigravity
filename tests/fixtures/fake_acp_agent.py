"""Deterministic Fake ACP Agent over stdio (`acp.Agent` / `run_agent`) for V3.5 testing.

Usage:
    python -m tests.fixtures.fake_acp_agent --scenario <scenario_name> [--settings <path>]

Scenarios:
    - nominal_stream: thought -> grouped read/search tool calls -> streamed text -> UsageUpdate + final Usage
    - edit_with_diff_and_permission: FileEditToolCallContent diff + request_permission -> echoes decision
    - elicitation_and_tos: form + url elicitations + rejects banned 'yolo' mode
    - faults_and_cancel: supports 'slow' (waits for session/cancel), 'corrupt' (bad JSON line), 'crash' (exit 1)
    - v31_env_audit: emits JSON report of V3.1 env vars (OGY_ACTIVE, OGY_GEMINI_HOME, USERPROFILE, etc.) & --settings
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import acp
from acp import schema
from acp.exceptions import RequestError

BANNED_MODES = frozenset({"yolo"})


def _default_config_options(
    *,
    model: str = "gemini-4-hydrogen",
    mode: str = "auto",
    thinking_level: str = "medium",
) -> list[schema.SessionConfigOptionSelect]:
    return [
        schema.SessionConfigOptionSelect(
            id="model",
            name="Model",
            current_value=model,
            options=[
                schema.SessionConfigSelectOption(value="gemini-4-hydrogen", name="Gemini 4 Hydrogen"),
                schema.SessionConfigSelectOption(value="gemini-4-neon", name="Gemini 4 Neon"),
                schema.SessionConfigSelectOption(value="gemini-4-carbon", name="Gemini 4 Carbon"),
            ],
        ),
        schema.SessionConfigOptionSelect(
            id="mode",
            name="Mode",
            current_value=mode,
            options=[
                schema.SessionConfigSelectOption(value="default", name="Default"),
                schema.SessionConfigSelectOption(value="accept-edits", name="Accept Edits"),
                schema.SessionConfigSelectOption(value="auto", name="Auto"),
                schema.SessionConfigSelectOption(value="plan", name="Plan"),
            ],
        ),
        schema.SessionConfigOptionSelect(
            id="thinking_level",
            name="Effort",
            current_value=thinking_level,
            options=[
                schema.SessionConfigSelectOption(value="low", name="Low"),
                schema.SessionConfigSelectOption(value="medium", name="Medium"),
                schema.SessionConfigSelectOption(value="high", name="High"),
            ],
        ),
    ]


def _extract_prompt_text(prompt: list[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        txt = getattr(block, "text", None)
        if isinstance(txt, str):
            parts.append(txt)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


class FakeAcpAgent(acp.Agent):
    """Scriptable, deterministic ACP v1 agent communicating over stdio JSON-RPC 2.0."""

    def __init__(self, scenario: str, settings_arg: str | None = None, raw_argv: list[str] | None = None) -> None:
        self.scenario = scenario
        self.settings_arg = settings_arg
        self.raw_argv = list(raw_argv or [])
        self._conn: acp.Client | None = None
        self._session_counter = 0
        self._sessions: dict[str, dict[str, str]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: acp.Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=schema.AgentCapabilities(
                load_session=False,
                session_capabilities=schema.SessionCapabilities(
                    fork=schema.SessionForkCapabilities(),
                    resume=schema.SessionResumeCapabilities(),
                    list=schema.SessionListCapabilities(),
                    close=schema.SessionCloseCapabilities(),
                ),
            ),
            agent_info=schema.Implementation(name="fake-acp-agent", version="3.5.0"),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> schema.AuthenticateResponse:
        return schema.AuthenticateResponse()

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        self._session_counter += 1
        sid = f"fake-acp-{self._session_counter}"
        self._sessions[sid] = {
            "model": "gemini-4-hydrogen",
            "mode": "auto",
            "thinking_level": "medium",
            "cwd": cwd,
        }
        self._cancel_events[sid] = asyncio.Event()
        return schema.NewSessionResponse(
            session_id=sid,
            config_options=_default_config_options(),
        )

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any) -> schema.ListSessionsResponse:
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=sid,
                    cwd=meta.get("cwd", cwd or "."),
                    title=f"Fake Session {sid}",
                )
                for sid, meta in self._sessions.items()
            ]
        )

    async def resume_session(self, session_id: str, cwd: str, **kwargs: Any) -> schema.ResumeSessionResponse:
        meta = self._sessions.setdefault(
            session_id,
            {"model": "gemini-4-hydrogen", "mode": "auto", "thinking_level": "medium", "cwd": cwd},
        )
        return schema.ResumeSessionResponse(
            config_options=_default_config_options(
                model=meta["model"],
                mode=meta["mode"],
                thinking_level=meta["thinking_level"],
            )
        )

    async def close_session(self, session_id: str, **kwargs: Any) -> schema.CloseSessionResponse:
        self._sessions.pop(session_id, None)
        self._cancel_events.pop(session_id, None)
        return schema.CloseSessionResponse()

    async def fork_session(self, session_id: str, cwd: str, **kwargs: Any) -> schema.ForkSessionResponse:
        self._session_counter += 1
        new_sid = f"fake-acp-{self._session_counter}"
        base = dict(self._sessions.get(session_id, {"model": "gemini-4-hydrogen", "mode": "auto", "thinking_level": "medium"}))
        base["cwd"] = cwd
        self._sessions[new_sid] = base
        self._cancel_events[new_sid] = asyncio.Event()
        return schema.ForkSessionResponse(
            session_id=new_sid,
            config_options=_default_config_options(
                model=base["model"],
                mode=base["mode"],
                thinking_level=base["thinking_level"],
            ),
        )

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        if config_id == "mode" and str(value).strip().lower() in BANNED_MODES:
            raise RequestError(-32602, f"Banned ACP mode: {value!r}")
        meta = self._sessions.setdefault(
            session_id,
            {"model": "gemini-4-hydrogen", "mode": "auto", "thinking_level": "medium", "cwd": "."},
        )
        meta[config_id] = str(value)
        return schema.SetSessionConfigOptionResponse(
            config_options=_default_config_options(
                model=meta["model"],
                mode=meta["mode"],
                thinking_level=meta["thinking_level"],
            )
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        ev = self._cancel_events.get(session_id)
        if ev is not None:
            ev.set()

    async def _send_thought(self, session_id: str, text: str) -> None:
        assert self._conn is not None
        await self._conn.session_update(
            session_id=session_id,
            update=schema.AgentThoughtChunk(
                content=schema.TextContentBlock(type="text", text=text),
            ),
        )

    async def _send_message(self, session_id: str, text: str) -> None:
        assert self._conn is not None
        await self._conn.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(
                content=schema.TextContentBlock(type="text", text=text),
            ),
        )

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        **kwargs: Any,
    ) -> schema.PromptResponse:
        assert self._conn is not None
        prompt_text = _extract_prompt_text(prompt)
        cancel_ev = self._cancel_events.setdefault(session_id, asyncio.Event())
        cancel_ev.clear()

        if self.scenario == "nominal_stream":
            return await self._run_nominal_stream(session_id, prompt_text)
        if self.scenario == "edit_with_diff_and_permission":
            return await self._run_edit_with_diff_and_permission(session_id)
        if self.scenario == "elicitation_and_tos":
            return await self._run_elicitation_and_tos(session_id)
        if self.scenario == "faults_and_cancel":
            return await self._run_faults_and_cancel(session_id, prompt_text, cancel_ev)
        if self.scenario == "v31_env_audit":
            return await self._run_v31_env_audit(session_id)

        await self._send_message(session_id, f"Echo: {prompt_text}")
        return schema.PromptResponse(stop_reason="end_turn")

    async def _run_nominal_stream(self, session_id: str, prompt_text: str) -> schema.PromptResponse:
        assert self._conn is not None
        await self._send_thought(session_id, "Inspecting repository structure and tests...")

        # Two read/search tool calls (grouped into ActivityGroup in the TUI)
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallStart(
                tool_call_id="tc-read-1",
                name="read_file",
                title="ogy_core/config.py",
                kind="read",
                status="in_progress",
                raw_input={"Path": "ogy_core/config.py"},
            ),
        )
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallProgress(
                tool_call_id="tc-read-1",
                name="read_file",
                title="ogy_core/config.py",
                kind="read",
                status="completed",
                raw_output="163 lines",
            ),
        )
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallStart(
                tool_call_id="tc-search-1",
                name="grep_search",
                title="profile_mode",
                kind="search",
                status="in_progress",
                raw_input={"Query": "profile_mode"},
            ),
        )
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallProgress(
                tool_call_id="tc-search-1",
                name="grep_search",
                title="profile_mode",
                kind="search",
                status="completed",
                raw_output="3 matches",
            ),
        )

        # Streamed answer chunks
        chunks = [
            "Analysis complete. ",
            f"Prompt received: `{prompt_text[:40]}`.\n\n",
            "All V3.1 contracts are verified.",
        ]
        for chunk in chunks:
            await self._send_message(session_id, chunk)

        # Intermediate usage update
        await self._conn.session_update(
            session_id=session_id,
            update=schema.UsageUpdate(
                used=10_000,
                size=1_000_000,
                cost=schema.Cost(amount=0.0042, currency="USD"),
            ),
        )

        return schema.PromptResponse(
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

    async def _run_edit_with_diff_and_permission(self, session_id: str) -> schema.PromptResponse:
        assert self._conn is not None
        diff_block = schema.FileEditToolCallContent(
            type="diff",
            path="app.py",
            old_text="def answer():\n    return 41\n",
            new_text="def answer():\n    return 42\n",
        )
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallStart(
                tool_call_id="tc-edit-1",
                name="edit_file",
                title="Edit app.py",
                kind="edit",
                status="pending",
                content=[diff_block],
                raw_input={"path": "app.py"},
            ),
        )
        perm_resp = await self._conn.request_permission(
            session_id=session_id,
            tool_call=schema.ToolCallUpdate(
                tool_call_id="tc-edit-1",
                name="edit_file",
                title="Edit app.py",
                kind="edit",
                content=[diff_block],
                raw_input={"path": "app.py"},
            ),
            options=[
                schema.PermissionOption(option_id="allow_once", name="Allow once", kind="allow_once"),
                schema.PermissionOption(option_id="allow_always", name="Allow always", kind="allow_always"),
                schema.PermissionOption(option_id="reject_once", name="Reject once", kind="reject_once"),
                schema.PermissionOption(option_id="reject_always", name="Never allow", kind="reject_always"),
            ],
        )
        outcome = perm_resp.outcome
        decision = getattr(outcome, "option_id", None) or getattr(outcome, "outcome", "cancelled")
        status = "completed" if str(decision).startswith("allow") else "failed"
        await self._conn.session_update(
            session_id=session_id,
            update=schema.ToolCallProgress(
                tool_call_id="tc-edit-1",
                name="edit_file",
                title="Edit app.py",
                kind="edit",
                status=status,
                content=[diff_block],
                raw_output=f"decision={decision}",
            ),
        )
        await self._send_message(session_id, f"PERMISSION_DECISION:{decision}")
        return schema.PromptResponse(
            stop_reason="end_turn",
            usage=schema.Usage(input_tokens=400, output_tokens=80, thought_tokens=50, total_tokens=530),
        )

    async def _run_elicitation_and_tos(self, session_id: str) -> schema.PromptResponse:
        assert self._conn is not None
        form_resp = await self._conn.create_elicitation(
            message="Configure deployment target",
            mode=schema.ElicitationFormSessionMode(
                session_id=session_id,
                requested_schema=schema.ElicitationSchema(
                    type="object",
                    properties={
                        "env": schema.ElicitationStringPropertySchema(
                            type="string",
                            title="Environment",
                            default="staging",
                        ),
                        "dry_run": schema.ElicitationBooleanPropertySchema(
                            type="boolean",
                            title="Dry run",
                            default=True,
                        ),
                    },
                    required=["env"],
                ),
            ),
        )
        form_action = getattr(form_resp, "action", "decline")
        form_content = getattr(form_resp, "content", None) or {}

        # URL elicitation (consent flow)
        url_task = asyncio.create_task(
            self._conn.create_elicitation(
                message="Authorize device login",
                mode=schema.ElicitationUrlSessionMode(
                    session_id=session_id,
                    elicitation_id="el-url-oauth",
                    url="https://accounts.example.com/device",
                ),
            )
        )
        url_resp = await url_task
        url_action = getattr(url_resp, "action", "decline")

        payload = {
            "form_action": form_action,
            "form_content": form_content,
            "url_action": url_action,
        }
        await self._send_message(session_id, "ELICITATION_RESULT:" + json.dumps(payload, sort_keys=True))
        return schema.PromptResponse(
            stop_reason="end_turn",
            usage=schema.Usage(input_tokens=300, output_tokens=60, thought_tokens=20, total_tokens=380),
        )

    async def _run_faults_and_cancel(
        self,
        session_id: str,
        prompt_text: str,
        cancel_ev: asyncio.Event,
    ) -> schema.PromptResponse:
        low = prompt_text.lower()
        if "crash" in low:
            os._exit(1)
        if "corrupt" in low:
            sys.stdout.write("{not valid json-rpc\n")
            sys.stdout.flush()
            await self._send_message(session_id, "RECOVERED_AFTER_CORRUPT_FRAME")
            return schema.PromptResponse(stop_reason="end_turn")
        if "slow" in low:
            await self._send_thought(session_id, "Working on a long cancellable operation...")
            try:
                await asyncio.wait_for(cancel_ev.wait(), timeout=10.0)
                await self._send_message(session_id, "TURN_CANCELLED_CLEANLY")
                return schema.PromptResponse(stop_reason="cancelled")
            except TimeoutError:
                await self._send_message(session_id, "TURN_COMPLETED_WITHOUT_CANCEL")
                return schema.PromptResponse(stop_reason="end_turn")
        await self._send_message(session_id, f"FAULTS_OK:{prompt_text}")
        return schema.PromptResponse(stop_reason="end_turn")

    async def _run_v31_env_audit(self, session_id: str) -> schema.PromptResponse:
        report = {
            "OGY_ACTIVE": os.environ.get("OGY_ACTIVE"),
            "AGY_CLI_DISABLE_AUTO_UPDATE": os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"),
            "OGY_GEMINI_HOME": os.environ.get("OGY_GEMINI_HOME"),
            "OGY_REAL_HOME": os.environ.get("OGY_REAL_HOME"),
            "OGY_DB_PATH": os.environ.get("OGY_DB_PATH"),
            "OGY_CONFIG_PATH": os.environ.get("OGY_CONFIG_PATH"),
            "USERPROFILE": os.environ.get("USERPROFILE"),
            "HOME": os.environ.get("HOME"),
            "settings_arg": self.settings_arg,
            "has_settings_flag": "--settings" in self.raw_argv,
        }
        await self._send_message(session_id, json.dumps(report, sort_keys=True))
        return schema.PromptResponse(
            stop_reason="end_turn",
            usage=schema.Usage(input_tokens=100, output_tokens=50, thought_tokens=0, total_tokens=150),
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fake_acp_agent", add_help=False)
    parser.add_argument("--scenario", default="nominal_stream")
    parser.add_argument("--settings", default=None)
    args, _unknown = parser.parse_known_args(argv)
    return args


async def _amain(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(raw)
    agent = FakeAcpAgent(scenario=args.scenario, settings_arg=args.settings, raw_argv=raw)
    await acp.run_agent(agent, use_unstable_protocol=True)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
