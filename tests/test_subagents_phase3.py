"""Comprehensive unit & async tests for Phase 3 (Lots 3.1, 3.2, 3.3).

All Gemini API calls are mocked (0 real API calls / 0 tokens consumed).
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from ogy_cli.init_audit import audit_mcp_config, cmd_init, cmd_uninstall
from ogy_cli.stats_render import query_filtered_stats, render_stats_report
from ogy_core.config import load_config
from ogy_core.firebreaks import stash_wrapped_output
from ogy_core.pricing import (
    estimate_gemini_api_cost,
    estimate_turn_cost,
    get_model_pricing,
)
from ogy_core.storage import (
    connect,
    connect_readonly,
    ensure_session,
    get_subagent_spend_summary,
    list_active_subagent_runs,
    upsert_subagent_run,
    upsert_turn,
)
from ogy_frontends.acp_tui.app import DelegationModal, HandoffReviewModal, OptigravityApp, TelemetryPanel
from ogy_frontends.acp_tui.client import AcpClientConfig
from ogy_frontends.acp_tui.widgets.composer import PromptTextArea
from ogy_subagents.gemini_client import (
    build_generate_config,
    normalize_thinking_level,
    run_agentic_loop,
)
from ogy_subagents.router import (
    ResilientGeminiRouter,
    SubagentBillingError,
    SubagentQuotaExhaustedError,
    classify_api_error,
    seconds_until_midnight_pt,
)
from ogy_subagents.secrets import (
    RedactingFilter,
    SecretResolution,
    check_secrets_file_permissions,
    redact_secrets,
    resolve_api_key,
    save_secret_to_env_file,
)
from ogy_subagents.server import SubagentsService, create_mcp_server
from ogy_subagents.tools_readonly import git_status, list_files, read_slice, rg_search
from ogy_subagents.worktree import WorktreeDisabledError, apply_approved_patch, run_isolated_worktree_edit
from tests.test_acp_client import FakeAgentConn

FAKE_KEY_FREE = "AIza" + "A" * 35
FAKE_KEY_TIER1 = "AIza" + "B" * 35
FAKE_KEY_GENERIC = "AIza" + "C" * 35


# ---------------------------------------------------------------------------
# Helpers / Mocks for google-genai & MCP Context
# ---------------------------------------------------------------------------


@dataclass
class FakePart:
    text: str | None = None
    function_call: dict[str, Any] | None = None
    thought: bool = False
    thought_signature: bytes | None = None


@dataclass
class FakeContent:
    role: str
    parts: list[FakePart] = field(default_factory=list)


@dataclass
class FakeCandidate:
    content: FakeContent


@dataclass
class FakeUsageMetadata:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0
    cached_content_token_count: int = 0


@dataclass
class FakeResponse:
    candidates: list[FakeCandidate]
    usage_metadata: FakeUsageMetadata
    text: str | None = None
    function_calls: list[dict[str, Any]] | None = None


class FakeAioModels:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        # Capture shallow snapshot of contents list at call time
        snap_contents = list(contents) if isinstance(contents, list) else contents
        self.calls.append({"model": model, "contents": snap_contents, "config": config})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeGenaiClient:
    def __init__(self, responses: list[Any]) -> None:
        self.models_aio = FakeAioModels(responses)
        self.aio = type("_Aio", (), {"models": self.models_aio})()


class FakeMcpContext:
    def __init__(self) -> None:
        self.progress_events: list[tuple[int, int, str]] = []
        self.info_events: list[str] = []

    async def report_progress(self, progress: int, total: int, message: str = "") -> None:
        self.progress_events.append((progress, total, message))

    async def info(self, message: str) -> None:
        self.info_events.append(message)


class FakeApiError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 1. Pricing Gemini 3.5 / 3.6 / 3.8 / 3.1-pro + thoughts_token_count + billing_source
# ---------------------------------------------------------------------------


def test_pricing_gemini_late_2026_catalogue_and_thoughts_billing() -> None:
    p35 = get_model_pricing("gemini-3.5-flash-lite")
    assert p35.input_per_m == 0.30
    assert p35.output_per_m == 2.50
    assert p35.free_tier_eligible is True

    p31_lite = get_model_pricing("gemini-3.1-flash-lite")
    assert p31_lite.input_per_m == 0.25
    assert p31_lite.output_per_m == 1.50

    # 3.6 / 3.8 flash doubles on 2027-01-01
    for m in ("gemini-3.6-flash", "gemini-3.8-flash"):
        before = get_model_pricing(m, as_of=date(2026, 12, 31))
        after = get_model_pricing(m, as_of=date(2027, 1, 1))
        assert (before.input_per_m, before.output_per_m) == (0.75, 3.75)
        assert (after.input_per_m, after.output_per_m) == (1.50, 7.50)

    # 3.1 pro <= 200K ($2/$12) vs > 200K ($4/$18)
    cost_short = estimate_turn_cost(
        "gemini-3.1-pro", input_tokens=100_000, output_tokens=10_000, billing_source="gemini_api_tier1"
    )
    assert cost_short == round(0.1 * 2.00 + 0.01 * 12.00, 6)
    cost_long = estimate_turn_cost(
        "gemini-3.1-pro", input_tokens=250_000, output_tokens=10_000, billing_source="gemini_api_tier1"
    )
    assert cost_long == round(0.25 * 4.00 + 0.01 * 18.00, 6)

    # Explicit (candidates_token_count + thoughts_token_count) * output_rate
    tier1_cost = estimate_gemini_api_cost(
        "gemini-3.5-flash-lite",
        prompt_token_count=100_000,
        candidates_token_count=2_000,
        thoughts_token_count=8_000,
        billing_source="gemini_api_tier1",
    )
    expected = round(0.1 * 0.30 + ((2_000 + 8_000) / 1_000_000) * 2.50, 6)
    assert tier1_cost == expected

    # Free Tier costs $0.0 regardless of tokens
    free_cost = estimate_gemini_api_cost(
        "gemini-3.5-flash-lite",
        prompt_token_count=100_000,
        candidates_token_count=2_000,
        thoughts_token_count=8_000,
        billing_source="gemini_api_free",
    )
    assert free_cost == 0.0


# ---------------------------------------------------------------------------
# 2. SDK Constraints: thinking_level, minimal->low, store=False, thought signatures
# ---------------------------------------------------------------------------


def test_sdk_config_constraints_and_thinking_level_translation() -> None:
    assert normalize_thinking_level("gemini-3.5-flash-lite", "minimal") == "minimal"
    assert normalize_thinking_level("gemini-3.8-flash", "minimal") == "low"
    assert normalize_thinking_level("gemini-4-hydrogen", "minimal") == "low"

    cfg = build_generate_config(
        model_id="gemini-3.8-flash",
        thinking_level="minimal",
        system_instruction="Be concise",
    )
    assert cfg["store"] is False
    assert cfg["thinking_config"] == {"thinking_level": "low"}
    for forbidden in ("thinking_budget", "temperature", "top_p", "top_k"):
        assert forbidden not in cfg
        with pytest.raises(ValueError):
            build_generate_config(model_id="gemini-3.5-flash-lite", **{forbidden: 0.5})


def test_agentic_loop_preserves_thought_signatures_and_enforces_max_steps(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def hello():\n    return 42\n", encoding="utf-8")

    # Turn 1: model emits a thought part with binary thought_signature + function_call
    turn1_content = FakeContent(
        role="model",
        parts=[
            FakePart(text="Let me inspect app.py", thought=True, thought_signature=b"sig-turn-1-xyz"),
            FakePart(function_call={"name": "read_slice", "args": {"path": "app.py", "start_line": 1, "end_line": 10}}),
        ],
    )
    turn2_content = FakeContent(
        role="model",
        parts=[FakePart(text="Found `hello()` at app.py:1 returning 42.")],
    )

    client = FakeGenaiClient([
        FakeResponse(
            candidates=[FakeCandidate(turn1_content)],
            usage_metadata=FakeUsageMetadata(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=50),
        ),
        FakeResponse(
            candidates=[FakeCandidate(turn2_content)],
            usage_metadata=FakeUsageMetadata(prompt_token_count=200, candidates_token_count=30, thoughts_token_count=10),
        ),
    ])

    steps_seen: list[int] = []

    async def _run() -> Any:
        return await run_agentic_loop(
            client,
            model_id="gemini-3.5-flash-lite",
            prompt="Where is hello defined?",
            system_instruction="Cite file:line.",
            tool_declarations=[],
            tool_dispatcher={"read_slice": lambda a: read_slice(tmp_path, a["path"], start_line=a["start_line"], end_line=a["end_line"])},
            thinking_level="low",
            max_steps=8,
            timeout_s=10.0,
            on_step=lambda s, m, msg, u: steps_seen.append(s),
            use_sdk_types=False,
        )

    res = asyncio.run(_run())
    assert res.stopped_reason == "completed"
    assert res.steps_taken == 2
    assert "app.py:1" in res.text
    assert res.usage.thoughts_token_count == 60
    assert res.usage.candidates_token_count == 50

    # Verify exact Content object (with thought_signature) was preserved in turn 2 contents!
    second_call_contents = client.models_aio.calls[1]["contents"]
    assert second_call_contents[1] is turn1_content
    assert second_call_contents[1].parts[0].thought_signature == b"sig-turn-1-xyz"
    assert client.models_aio.calls[0]["config"]["store"] is False
    assert steps_seen

    # Verify max_steps hard cap stops infinite tool loops
    loop_turn = FakeContent(
        role="model",
        parts=[FakePart(function_call={"name": "list_files", "args": {}})],
    )
    client_cap = FakeGenaiClient([
        FakeResponse(candidates=[FakeCandidate(loop_turn)], usage_metadata=FakeUsageMetadata())
        for _ in range(5)
    ])
    res_cap = asyncio.run(
        run_agentic_loop(
            client_cap,
            model_id="gemini-3.5-flash-lite",
            prompt="Loop forever",
            system_instruction="Test",
            tool_declarations=[],
            tool_dispatcher={"list_files": lambda _: list_files(tmp_path)},
            max_steps=3,
            timeout_s=10.0,
            use_sdk_types=False,
        )
    )
    assert res_cap.stopped_reason == "max_steps"
    assert res_cap.steps_taken == 3


# ---------------------------------------------------------------------------
# 3. Read-only tools + N1/N2 MCP Server tools + Dual progress (MCP + SQLite WAL)
# ---------------------------------------------------------------------------


def test_readonly_tools_and_path_traversal_guard(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(
        f"KEY = '{FAKE_KEY_FREE}'\ndef target_fn():\n    return 'ok'\n",
        encoding="utf-8",
    )
    slice_out = read_slice(tmp_path, "src/demo.py", start_line=1, end_line=3)
    assert "L2: def target_fn():" in slice_out
    assert FAKE_KEY_FREE not in slice_out  # redacted!

    # Traversal refused
    assert "Access denied" in read_slice(tmp_path, "../outside.txt")
    assert "src/demo.py" in list_files(tmp_path)
    search_out = rg_search(tmp_path, "target_fn")
    assert "demo.py:2" in search_out
    assert isinstance(git_status(tmp_path), str)


def test_n1_n2_service_dual_progress_and_mcp_readonly_annotations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OGY_DB_PATH", str(tmp_path / "ogy.db"))
    cfg = load_config()
    cfg.stash_dir = tmp_path / "stash"
    cfg.subagents_privacy_mode = "free_first"

    # Prepare a stashed log with a leaked key to verify redaction
    digest = stash_wrapped_output(
        f"FAILED tests/test_x.py::test_boom\nAssertionError: 1 != 2\nleaked={FAKE_KEY_FREE}\n",
        "stderr detail\n",
        cmd=["pytest"],
        exit_code=1,
        duration_s=1.2,
        stash_dir=cfg.stash_dir,
    )

    # Prepare repo file for N2 research
    (tmp_path / "mod.py").write_text("VALUE = 123\n", encoding="utf-8")

    # Queue mock clients: 1) summarize_stash, 2) build_handoff, 3) research_repo (2 turns), 4) triage_failure (1 turn)
    clients = [
        FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text=f"Summary: tests/test_x.py failed ({FAKE_KEY_FREE})")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=300, candidates_token_count=40, thoughts_token_count=10),
            )
        ]),
        FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text="## Goal\nFix test_boom\n## Next Step\nPatch mod.py")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=400, candidates_token_count=180, thoughts_token_count=20),
            )
        ]),
        FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(function_call={"name": "read_slice", "args": {"path": "mod.py", "start_line": 1, "end_line": 5}})]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=250, candidates_token_count=25, thoughts_token_count=30),
            ),
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text="mod.py:1 defines VALUE = 123")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=350, candidates_token_count=35, thoughts_token_count=15),
            ),
        ]),
        FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text="Root Cause: AssertionError at tests/test_x.py")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=500, candidates_token_count=60, thoughts_token_count=40),
            )
        ]),
    ]

    router = ResilientGeminiRouter(
        privacy_mode="free_first",
        client_factory=lambda _key: clients.pop(0),
        secret_resolver=lambda tier: SecretResolution(tier=tier, key=FAKE_KEY_FREE, source="env:MOCK"),
    )
    svc = SubagentsService(cfg=cfg, router=router, use_sdk_types=False)

    # Verify MCP server exposes all 4 tools with readOnlyHint=True
    mcp_srv = create_mcp_server(svc)
    tools = asyncio.run(mcp_srv.list_tools())
    tool_map = {t.name: t for t in tools}
    assert set(tool_map.keys()) == {
        "ogy_summarize_stash",
        "ogy_build_handoff",
        "ogy_research_repo",
        "ogy_triage_failure",
    }
    for t_obj in tool_map.values():
        assert t_obj.annotations is not None
        assert getattr(t_obj.annotations, "read_only_hint", getattr(t_obj.annotations, "readOnlyHint", None)) is True
        assert t_obj.annotations.model_dump(by_alias=True).get("readOnlyHint") is True

    ctx = FakeMcpContext()

    async def _exercise() -> tuple[str, str, str, str]:
        s1 = await svc.summarize_stash(digest[:12], focus_query="AssertionError", ctx=ctx)
        s2 = await svc.build_handoff("Fix test_boom", git_status="M mod.py", last_error="AssertionError", ctx=ctx)
        s3 = await svc.research_repo("What is VALUE?", workspace_path=str(tmp_path), ctx=ctx)
        s4 = await svc.triage_failure("AssertionError in test_x", stash_hash=digest[:12], workspace_path=str(tmp_path), ctx=ctx)
        return s1, s2, s3, s4

    out_stash, out_handoff, out_research, out_triage = asyncio.run(_exercise())
    assert "tests/test_x.py" in out_stash
    assert FAKE_KEY_FREE not in out_stash  # redacted in LLM output
    assert "## Goal" in out_handoff
    assert "mod.py:1" in out_research
    assert "Root Cause" in out_triage
    assert len(ctx.progress_events) >= 4
    assert len(ctx.info_events) >= 4

    # Verify SQLite WAL subagent_runs populated
    conn = connect_readonly(cfg.db_path)
    try:
        runs = list_active_subagent_runs(conn)
        assert len(runs) == 4
        research_row = next(r for r in runs if r["tool_name"] == "ogy_research_repo")
        assert research_row["status"] == "completed"
        assert research_row["step"] == 2
        assert research_row["thought_tokens"] == 45
        assert research_row["tier"] == "free"
        assert research_row["cost_usd"] == 0.0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Resilience Router: RPM retry vs RPD circuit-breaker -> Tier 1 fallback, tier1_only, 402
# ---------------------------------------------------------------------------


def test_router_429_rpm_retry_rpd_fallback_and_privacy_isolation() -> None:
    assert classify_api_error(FakeApiError(429, "RESOURCE_EXHAUSTED: RequestsPerMinute exceeded")) == "rpm_tpm"
    assert classify_api_error(FakeApiError(429, "RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel")) == "rpd"
    assert classify_api_error(FakeApiError(429, "RESOURCE_EXHAUSTED: generic quota")) == "rpd_ambiguous"
    assert classify_api_error(FakeApiError(402, "Payment Required: insufficient balance")) == "billing_402"
    assert seconds_until_midnight_pt(datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)) > 60.0

    # A) RPM 429 -> 2 retries with backoff+jitter -> succeeds on Free Tier
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    rpm_client = FakeGenaiClient([
        FakeApiError(429, "RESOURCE_EXHAUSTED: RequestsPerMinute limit"),
        FakeApiError(429, "RESOURCE_EXHAUSTED: TokensPerMinute limit"),
        FakeResponse(
            candidates=[FakeCandidate(FakeContent("model", [FakePart(text="Recovered after RPM")]))],
            usage_metadata=FakeUsageMetadata(prompt_token_count=100, candidates_token_count=10),
        ),
    ])
    router_rpm = ResilientGeminiRouter(
        privacy_mode="free_first",
        client_factory=lambda _k: rpm_client,
        secret_resolver=lambda t: SecretResolution(tier=t, key=FAKE_KEY_FREE if t == "free" else FAKE_KEY_TIER1, source="env"),
        sleep_fn=fake_sleep,
        jitter_fn=lambda: 0.1,
    )

    async def _run_rpm() -> Any:
        from ogy_subagents.gemini_client import run_stateless_generation

        return await router_rpm.execute(
            model_id="gemini-3.5-flash-lite",
            runner=lambda c, _t: run_stateless_generation(
                c, model_id="gemini-3.5-flash-lite", prompt="hi", system_instruction="ok", use_sdk_types=False
            ),
        )

    res_rpm = asyncio.run(_run_rpm())
    assert res_rpm.tier == "free"
    assert res_rpm.cost_usd == 0.0
    assert sleeps == [1.6, 4.1]

    # B) RPD 429 on Free Tier -> cooldown + automatic fallback to Tier 1 with cost notice
    calls_by_key: list[str] = []

    def factory_rpd(key: str) -> Any:
        calls_by_key.append(key)
        if key == FAKE_KEY_FREE:
            return FakeGenaiClient([FakeApiError(429, "RESOURCE_EXHAUSTED: RequestsPerDay quota reached")])
        return FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text="Answered on Tier 1")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=10_000, candidates_token_count=1_000, thoughts_token_count=1_000),
            )
        ])

    router_rpd = ResilientGeminiRouter(
        privacy_mode="free_first",
        fallback_to_tier1=True,
        client_factory=factory_rpd,
        secret_resolver=lambda t: SecretResolution(tier=t, key=FAKE_KEY_FREE if t == "free" else FAKE_KEY_TIER1, source="env"),
        sleep_fn=fake_sleep,
    )

    async def _run_rpd() -> Any:
        from ogy_subagents.gemini_client import run_stateless_generation

        return await router_rpd.execute(
            model_id="gemini-3.5-flash-lite",
            runner=lambda c, _t: run_stateless_generation(
                c, model_id="gemini-3.5-flash-lite", prompt="hi", system_instruction="ok", use_sdk_types=False
            ),
        )

    res_rpd = asyncio.run(_run_rpd())
    assert res_rpd.tier == "tier1"
    assert res_rpd.billing_source == "gemini_api_tier1"
    assert res_rpd.cost_usd > 0.0
    assert res_rpd.fallback_notice is not None and "Tier 1" in res_rpd.fallback_notice
    assert router_rpd.is_in_cooldown("free") is True
    assert calls_by_key == [FAKE_KEY_FREE, FAKE_KEY_TIER1]

    # C) tier1_only NEVER touches Project A (Free Tier)
    from ogy_subagents.gemini_client import run_stateless_generation

    tiers_requested: list[str] = []
    router_t1 = ResilientGeminiRouter(
        privacy_mode="tier1_only",
        client_factory=lambda _k: FakeGenaiClient([
            FakeResponse(
                candidates=[FakeCandidate(FakeContent("model", [FakePart(text="Private ok")]))],
                usage_metadata=FakeUsageMetadata(prompt_token_count=50, candidates_token_count=10),
            )
        ]),
        secret_resolver=lambda t: (tiers_requested.append(t) or SecretResolution(tier=t, key=FAKE_KEY_TIER1 if t == "tier1" else None, source="env")),
    )
    res_t1 = asyncio.run(
        router_t1.execute(
            model_id="gemini-3.5-flash-lite",
            runner=lambda c, _t: run_stateless_generation(
                c,
                model_id="gemini-3.5-flash-lite",
                prompt="p",
                system_instruction="s",
                use_sdk_types=False,
            ),
        )
    )
    assert res_t1.tier == "tier1"
    assert tiers_requested == ["tier1"]

    # D) HTTP 402 -> SubagentBillingError
    router_402 = ResilientGeminiRouter(
        privacy_mode="tier1_only",
        client_factory=lambda _k: FakeGenaiClient([FakeApiError(402, "Payment Required")]),
        secret_resolver=lambda t: SecretResolution(tier=t, key=FAKE_KEY_TIER1, source="env"),
    )
    with pytest.raises(SubagentBillingError):
        asyncio.run(
            router_402.execute(
                model_id="gemini-3.5-flash-lite",
                runner=lambda c, _t: c.aio.models.generate_content(model="m", contents="p", config={}),
            )
        )


# ---------------------------------------------------------------------------
# 5. Secrets Cascade, Redaction & CLI init / audit mcp
# ---------------------------------------------------------------------------


def test_secrets_cascade_redaction_and_mcp_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redaction regex & logging filter
    leaked = f"Authorization failed for {FAKE_KEY_TIER1} on host"
    assert FAKE_KEY_TIER1 not in redact_secrets(leaked)
    assert "[REDACTED_GEMINI_KEY]" in redact_secrets(leaked)

    rec = logging.LogRecord("test", logging.INFO, "", 0, "key=%s", (FAKE_KEY_FREE,), None)
    RedactingFilter().filter(rec)
    assert FAKE_KEY_FREE not in rec.getMessage()

    sec_file = tmp_path / "secrets.env"
    save_secret_to_env_file("OGY_GEMINI_API_KEY_TIER1", FAKE_KEY_TIER1, secrets_path=sec_file)

    # 1) Env beats keyring and file
    class FakeKeyring:
        def __init__(self, store: dict[str, str]) -> None:
            self.store = store

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get(username)

    r_env = resolve_api_key(
        "tier1",
        env={"OGY_GEMINI_API_KEY_TIER1": FAKE_KEY_GENERIC},
        secrets_path=sec_file,
        keyring_module=FakeKeyring({"gemini_api_key_tier1": FAKE_KEY_FREE}),
    )
    assert r_env.key == FAKE_KEY_GENERIC
    assert r_env.source == "env:OGY_GEMINI_API_KEY_TIER1"

    # 2) Keyring beats file when non-empty
    r_kr = resolve_api_key(
        "tier1",
        env={},
        secrets_path=sec_file,
        keyring_module=FakeKeyring({"gemini_api_key_tier1": FAKE_KEY_FREE}),
    )
    assert r_kr.key == FAKE_KEY_FREE
    assert r_kr.source == "keyring:gemini_api_key_tier1"

    # 3) Empty/broken keyring falls through to secrets.env (headless/SSH safety)
    r_file = resolve_api_key(
        "tier1",
        env={},
        secrets_path=sec_file,
        keyring_module=FakeKeyring({"gemini_api_key_tier1": "   "}),
    )
    assert r_file.key == FAKE_KEY_TIER1
    assert r_file.source.startswith("file:")

    # Permissions check is OK on both Windows and POSIX 0600
    perm_status, _perm_detail = check_secrets_file_permissions(sec_file)
    assert perm_status == "ok"

    # POSIX too-permissive refusal check
    if os.name != "nt":
        os.chmod(sec_file, 0o644)
        bad_status, _ = check_secrets_file_permissions(sec_file)
        assert bad_status == "fail"
        refused = resolve_api_key("tier1", env={}, secrets_path=sec_file, keyring_module=FakeKeyring({}))
        assert refused.key is None
        assert refused.warning is not None
        os.chmod(sec_file, 0o600)

    # `ogy init` registers `ogy-subagents` with eager:false and ZERO plaintext API keys
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cmd_init(non_interactive=True, privacy_mode="tier1_only") == 0
    mcp_path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "ogy-subagents" in mcp_data["mcpServers"]
    assert mcp_data["mcpServers"]["ogy-subagents"]["eager"] is False
    assert "env" not in mcp_data["mcpServers"]["ogy-subagents"]
    assert FAKE_KEY_TIER1 not in mcp_path.read_text(encoding="utf-8")

    # `audit_mcp_config` catches plaintext AIza* keys
    bad_audit = audit_mcp_config({
        "mcpServers": {
            "leaky": {"command": "python", "env": {"GEMINI_API_KEY": FAKE_KEY_TIER1}, "eager": False}
        }
    })
    assert any("plaintext Gemini API key" in iss for f in bad_audit["findings"] for iss in f["issues"])

    # `ogy uninstall` removes both optigravity and ogy-subagents cleanly
    assert cmd_uninstall() == 0
    mcp_after = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "ogy-subagents" not in mcp_after["mcpServers"]
    assert "optigravity" not in mcp_after["mcpServers"]


# ---------------------------------------------------------------------------
# 6. TUI Sidebar (dual OAuth vs API USD), [D] Modal, /rotate N1 handoff, ogy stats dual
# ---------------------------------------------------------------------------


def test_tui_subagents_sidebar_delegation_modal_and_rotate_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ogy.db"
    monkeypatch.setenv("OGY_DB_PATH", str(db_path))

    # Seed DB with an OAuth session + 2 subagent runs (Free + Tier 1)
    conn = connect(db_path, init_schema=True)
    try:
        ensure_session(conn, session_id="acp-sess-1", workspace_path=str(tmp_path), initial_model="gemini-4-hydrogen")
        upsert_turn(
            conn,
            session_id="acp-sess-1",
            step_index=1,
            model_id="gemini-4-hydrogen",
            input_tokens=10_000,
            cache_read_tokens=40_000,
            output_tokens=500,
            estimated_cost_usd=0.025,
        )
        upsert_subagent_run(
            conn,
            run_id="sub-1",
            parent_session_id="acp-sess-1",
            tool_name="ogy_research_repo",
            model_id="gemini-3.5-flash-lite",
            tier="free",
            step=3,
            max_steps=8,
            status="running",
            input_tokens=4_000,
            output_tokens=300,
            thought_tokens=200,
            cost_usd=0.0,
        )
        upsert_subagent_run(
            conn,
            run_id="sub-2",
            parent_session_id="acp-sess-1",
            tool_name="ogy_triage_failure",
            model_id="gemini-3.5-flash-lite",
            tier="tier1",
            step=4,
            max_steps=8,
            status="completed",
            input_tokens=10_000,
            output_tokens=500,
            thought_tokens=500,
            cost_usd=0.0055,
        )
        summary = get_subagent_spend_summary(conn)
        assert summary["runs"] == 2
        assert summary["free_runs"] == 1
        assert summary["tier1_runs"] == 1
        assert summary["cost_usd"] == pytest.approx(0.0055)

        # Verify `ogy stats` renders dual OAuth vs Subagents MCP blocks
        sessions_data = query_filtered_stats(conn)
        stats_txt = render_stats_report(conn, sessions_data)
        assert "Sessions agy (OAuth)" in stats_txt
        assert "Subagents MCP (API USD)" in stats_txt
        assert "$0.0055" in stats_txt
    finally:
        conn.close()

    # Fake SubagentsService for TUI parallel delegation & /rotate handoff
    class FakeSubagentsSvc:
        async def research_repo(self, question: str, **kwargs: Any) -> str:
            return f"Report for '{question}': found entry at ogy_core/pricing.py:1."

        async def build_handoff(self, goal: str, **kwargs: Any) -> str:
            return f"# Semantic Handoff (N1)\n## Goal\n{goal}\n## Next Step\nRun pytest."

    fake_conn = FakeAgentConn()
    app = OptigravityApp(
        client_config=AcpClientConfig(cwd=str(tmp_path)),
        ogy_config=load_config(),
        injected_agent_conn=fake_conn,
        subagents_service=FakeSubagentsSvc(),
    )

    async def _run() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.25)

            # Sidebar displays live subagent tree & separated Gemini API spend
            tel = app.query_one("#telemetry-panel", TelemetryPanel)
            tree_text = str(tel.query_one("#subagent-tree", Static).render())
            assert "ogy_research_repo [3/8]" in tree_text
            assert "3.5-flash-lite" in tree_text
            assert "Free" in tree_text
            spend_text = str(tel.query_one("#tel-api-spend").render())
            assert "$0.0055" in spend_text

            # Trigger [D] > 60K delegation modal (Mode 1: insert instruction)
            assert app.current_state is not None
            app.current_state.context_tokens = 85_000
            app.current_state.active_goal = "Locate pricing switch"
            await app.action_open_delegation_modal()
            await pilot.pause(0.05)
            assert isinstance(app.screen, DelegationModal)
            await pilot.click("#delegate-insert-btn")
            await pilot.pause(0.05)
            ta = app.query_one("#prompt-input", PromptTextArea)
            assert "ogy_research_repo" in ta.text
            assert "Locate pricing switch" in ta.text

            # Mode 2: run_parallel attaches sourced report to prompt composer
            ta.text = ""
            await app.action_open_delegation_modal("Inspect pricing")
            await pilot.pause(0.05)
            await pilot.click("#delegate-run-btn")
            await pilot.pause(0.15)
            assert "ogy_core/pricing.py:1" in ta.text

            # /rotate uses N1 `ogy_build_handoff` when subagent service is configured
            await app.action_open_rotation_review()
            await pilot.pause(0.1)
            assert isinstance(app.screen, HandoffReviewModal)
            editor = app.screen.query_one("#handoff-editor")
            assert "Semantic Handoff (N1)" in editor.text
            await pilot.press("escape")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 7. Opt-in Level 3 Worktree Isolation + V1 Stdlib Hot-Path Invariant
# ---------------------------------------------------------------------------


def test_worktree_opt_in_and_isolated_patch_contract(tmp_path: Path) -> None:
    # Disabled by default
    cfg = load_config()
    cfg.subagents_worktree_enabled = False
    with pytest.raises(WorktreeDisabledError):
        run_isolated_worktree_edit(tmp_path, lambda wt: "noop", cfg=cfg)

    # Create a real tiny git repo in tmp_path
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@optigravity.local"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Optigravity Test"], cwd=str(tmp_path), check=True)
    (tmp_path / "hello.txt").write_text("line1\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

    def _edit_in_wt(wt: Path) -> str:
        (wt / "hello.txt").write_text("line1\nline2_from_subagent\n", encoding="utf-8")
        return "Added line2_from_subagent"

    contract = run_isolated_worktree_edit(tmp_path, _edit_in_wt, cfg=cfg, opt_in=True)
    # Main repo MUST remain untouched!
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "line1\n"
    assert contract.requires_human_approval is True
    assert "hello.txt" in contract.files_touched
    assert Path(contract.patch_path).is_file()
    parsed = json.loads(contract.to_json())
    assert set(parsed.keys()) >= {"files_touched", "git_diff_stat", "patch_path", "requires_human_approval"}

    # Patch applies cleanly on dry-run check
    check = apply_approved_patch(tmp_path, contract.patch_path, check_only=True)
    assert check["ok"] is True


def test_v1_hot_path_remains_100_percent_stdlib() -> None:
    """Invariant #1: ogy_core/*, ogy_hooks/*, ogy_statusline/* must NEVER import third-party deps."""
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_roots = {"google", "mcp", "keyring", "textual", "rich"}
    for pkg in ("ogy_core", "ogy_hooks", "ogy_statusline"):
        for py_file in (repo_root / pkg).rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top not in forbidden_roots, f"{py_file} imports forbidden {alias.name}"
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    assert top not in forbidden_roots, f"{py_file} imports forbidden {node.module}"


# ---------------------------------------------------------------------------
# Phase 2 (Lot 3.5.3): Real `google.genai.errors` & `types.Part` thought_signature harness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genai_sdk_real_client_error_429_402_400_and_server_error_5xx() -> None:
    """Exercise ResilientGeminiRouter against real `google.genai.errors.ClientError` & `ServerError`."""
    from google.genai import errors as genai_errors

    def _ok_result(text: str = "ok") -> Any:
        from ogy_subagents.gemini_client import SubagentLoopResult, UsageAccumulator

        return SubagentLoopResult(
            text=text,
            steps_taken=1,
            max_steps=1,
            usage=UsageAccumulator(prompt_token_count=50, candidates_token_count=10),
        )

    # 1. 429 RPD on Project A (free) -> immediate circuit-breaker & fallback to Project B (tier1)
    rpd_err = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "message": "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel-FreeTier"}},
    )
    assert classify_api_error(rpd_err) == "rpd"

    sleeps: list[float] = []

    async def _fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    now_t = [1000.0]
    router_rpd = ResilientGeminiRouter(
        privacy_mode="free_first",
        fallback_to_tier1=True,
        client_factory=lambda key: key,
        secret_resolver=lambda tier: SecretResolution(
            key=FAKE_KEY_FREE if tier == "free" else FAKE_KEY_TIER1,
            source=f"env:{tier}",
            tier=tier,
        ),
        sleep_fn=_fake_sleep,
        clock_fn=lambda: now_t[0],
        jitter_fn=lambda: 0.0,
    )

    tiers_called: list[str] = []

    async def _runner_rpd(_client: Any, tier: str) -> Any:
        tiers_called.append(tier)
        if tier == "free":
            raise rpd_err
        return _ok_result("tier1-fallback-ok")

    routed = await router_rpd.execute(model_id="gemini-3.5-flash-lite", runner=_runner_rpd)
    assert tiers_called == ["free", "tier1"]
    assert routed.tier == "tier1"
    assert routed.billing_source == "gemini_api_tier1"
    assert routed.fallback_notice is not None
    assert router_rpd.is_in_cooldown("free") is True

    # 2. 429 RPM/TPM -> exponential backoff retries before succeeding
    rpm_err = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "message": "Rate limit exceeded: RequestsPerMinute"}},
    )
    assert classify_api_error(rpm_err) == "rpm_tpm"

    rpm_sleeps: list[float] = []

    async def _rpm_sleep(sec: float) -> None:
        rpm_sleeps.append(sec)

    router_rpm = ResilientGeminiRouter(
        privacy_mode="tier1_only",
        client_factory=lambda key: key,
        secret_resolver=lambda tier: SecretResolution(key=FAKE_KEY_TIER1, source="env:tier1", tier=tier),
        sleep_fn=_rpm_sleep,
        clock_fn=lambda: now_t[0],
        jitter_fn=lambda: 0.0,
    )
    attempts = [0]

    async def _runner_rpm(_client: Any, _tier: str) -> Any:
        attempts[0] += 1
        if attempts[0] < 3:
            raise rpm_err
        return _ok_result("recovered-after-rpm-backoff")

    res_rpm = await router_rpm.execute(model_id="gemini-3.5-flash-lite", runner=_runner_rpm)
    assert res_rpm.result.text == "recovered-after-rpm-backoff"
    assert rpm_sleeps == [1.5, 4.0]

    # 3. Ambiguous 429 -> 15 min cooldown (900s)
    amb_err = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED"}},
    )
    assert classify_api_error(amb_err) == "rpd_ambiguous"

    router_amb = ResilientGeminiRouter(
        privacy_mode="free_first",
        fallback_to_tier1=False,
        client_factory=lambda key: key,
        secret_resolver=lambda tier: SecretResolution(key=FAKE_KEY_FREE, source="env:free", tier=tier),
        sleep_fn=_fake_sleep,
        clock_fn=lambda: now_t[0],
    )
    with pytest.raises(SubagentQuotaExhaustedError):
        await router_amb.execute(model_id="gemini-3.5-flash-lite", runner=lambda _c, _t: (_ for _ in ()).throw(amb_err))
    assert 895.0 <= router_amb.cooldown_remaining_s("free") <= 900.0

    # 4. 402 Payment Required -> immediate SubagentBillingError (no retry)
    err_402 = genai_errors.ClientError(
        402,
        {"error": {"code": 402, "message": "Payment Required: insufficient balance on billing account"}},
    )
    assert classify_api_error(err_402) == "billing_402"

    router_402 = ResilientGeminiRouter(
        privacy_mode="tier1_only",
        client_factory=lambda key: key,
        secret_resolver=lambda tier: SecretResolution(key=FAKE_KEY_TIER1, source="env:tier1", tier=tier),
        sleep_fn=_fake_sleep,
        clock_fn=lambda: now_t[0],
    )
    with pytest.raises(SubagentBillingError):
        await router_402.execute(model_id="gemini-3.5-flash-lite", runner=lambda _c, _t: (_ for _ in ()).throw(err_402))

    # 5. 400 ClientError and 503 ServerError classify as 'other' and propagate cleanly
    err_400 = genai_errors.ClientError(400, {"error": {"code": 400, "message": "INVALID_ARGUMENT"}})
    err_503 = genai_errors.ServerError(503, {"error": {"code": 503, "message": "Service Unavailable"}})
    assert classify_api_error(err_400) == "other"
    assert classify_api_error(err_503) == "other"


@pytest.mark.asyncio
async def test_genai_sdk_real_types_part_thought_signature_preserved_in_level2_loop(tmp_path: Path) -> None:
    """Real `google.genai.types.Part` with `thought_signature` is preserved verbatim across Level 2 turns."""
    from google.genai import types

    (tmp_path / "calc.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")

    sig_bytes = b"\xde\xad\xbe\xef-thought-sig-turn1"
    turn1_content = types.Content(
        role="model",
        parts=[
            types.Part(text="I should read calc.py first.", thought=True, thought_signature=sig_bytes),
            types.Part(
                function_call=types.FunctionCall(
                    name="read_slice",
                    args={"path": "calc.py", "start_line": 1, "end_line": 10},
                )
            ),
        ],
    )
    turn2_content = types.Content(
        role="model",
        parts=[types.Part(text="`double(x)` is defined in `calc.py:1` and returns `x * 2`.")],
    )

    resp1 = types.GenerateContentResponse(
        candidates=[types.Candidate(content=turn1_content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=120,
            candidates_token_count=25,
            thoughts_token_count=40,
        ),
    )
    resp2 = types.GenerateContentResponse(
        candidates=[types.Candidate(content=turn2_content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=210,
            candidates_token_count=30,
            thoughts_token_count=15,
        ),
    )

    fake_client = FakeGenaiClient([resp1, resp2])
    res = await run_agentic_loop(
        fake_client,
        model_id="gemini-3.5-flash-lite",
        prompt="Where is double defined?",
        system_instruction="Be concise and cite file:line.",
        tool_declarations=[],
        tool_dispatcher={"read_slice": lambda a: read_slice(tmp_path, a["path"], start_line=a["start_line"], end_line=a["end_line"])},
        thinking_level="low",
        max_steps=8,
        timeout_s=10.0,
        use_sdk_types=True,
    )

    assert res.stopped_reason == "completed"
    assert res.steps_taken == 2
    assert "calc.py:1" in res.text
    assert res.usage.thoughts_token_count == 55

    # Verify exact SDK Content & Part.thought_signature were retransmitted on turn 2
    turn2_contents = fake_client.models_aio.calls[1]["contents"]
    assert turn2_contents[1] is turn1_content
    assert turn2_contents[1].parts[0].thought_signature == sig_bytes
    # Verify store=False in SDK config when supported
    cfg_sent = fake_client.models_aio.calls[0]["config"]
    if hasattr(cfg_sent, "store") and cfg_sent.store is not None:
        assert cfg_sent.store is False

