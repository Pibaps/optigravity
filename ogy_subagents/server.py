"""Dedicated stdio MCP server `ogy-subagents` (Level 1 Stateless + Level 2 Async google-genai).

Zero `print()` to stdout (MCP JSON-RPC uses stdio). All logs go to `sys.stderr`
filtered through `RedactingFilter`. All standard tools carry `readOnlyHint=True`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import Context as _McpContext
except ImportError:  # pragma: no cover - optional [mcp]/[subagents] extra
    _McpContext = Any  # type: ignore[misc,assignment]

Context = _McpContext

from ogy_core.config import OgyConfig, load_config
from ogy_core.firebreaks import recall_stashed_output
from ogy_core.pricing import estimate_gemini_api_cost
from ogy_core.storage import connect, upsert_subagent_run, utc_now_iso
from ogy_subagents.gemini_client import (
    UsageAccumulator,
    run_agentic_loop,
    run_stateless_generation,
)
from ogy_subagents.router import ResilientGeminiRouter
from ogy_subagents.secrets import RedactingFilter, redact_secrets
from ogy_subagents.tools_readonly import (
    READONLY_TOOL_DECLARATIONS,
    build_readonly_tool_dispatcher,
)

# Configure stderr-only logging with API key redaction
_logger = logging.getLogger("ogy_subagents")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.addFilter(RedactingFilter())
    _handler.setFormatter(logging.Formatter("[ogy-subagents] %(levelname)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

PROGRESS_THROTTLE_S = 0.10  # ~100 ms throttle for MCP ctx progress notifications

SYSTEM_SUMMARIZE_STASH = (
    "You are Optigravity's log summarizer. Produce a concise, sourced summary in 15-20 lines max: "
    "highlight exit status, root cause errors with exact file paths and line numbers, and key metrics. "
    "Never invent facts not present in the log."
)

SYSTEM_BUILD_HANDOFF = (
    "You are Optigravity's session handoff builder. Write a clean Markdown `.ogy/handoff.md` "
    "calibrated to 600-900 tokens with sections: ## Goal, ## Modified Files, ## Checks & Last Error, "
    "## Negative Constraints, ## Next Step. Be dense, exact, and factual."
)

SYSTEM_RESEARCH_REPO = (
    "You are Optigravity's read-only repository researcher. Use ONLY the provided read-only tools "
    "(rg_search, read_slice, list_files, git_status) to answer the question accurately. "
    "Return a concise sourced report (~20 lines max) citing exact relative file paths and line numbers."
)

SYSTEM_TRIAGE_FAILURE = (
    "You are Optigravity's read-only failure triage subagent. Inspect the error summary and relevant "
    "code slices using ONLY read-only tools. Return a concise sourced triage report (~20 lines max) "
    "with: Root Cause, Exact File:Line Citations, and Recommended Fix Strategy."
)


class _DualProgressReporter:
    """Emits throttled MCP `ctx.report_progress` + `ctx.info` AND real-time SQLite WAL updates."""

    def __init__(
        self,
        *,
        db_path: Path,
        run_id: str,
        tool_name: str,
        model_id: str,
        tier: str,
        max_steps: int,
        parent_session_id: str | None = None,
        ctx: Any | None = None,
        throttle_s: float = PROGRESS_THROTTLE_S,
    ) -> None:
        self.db_path = db_path
        self.run_id = run_id
        self.tool_name = tool_name
        self.model_id = model_id
        self.tier = tier
        self.max_steps = max_steps
        self.parent_session_id = parent_session_id
        self.ctx = ctx
        self.throttle_s = throttle_s
        self._last_mcp_emit = 0.0
        self.started_at = utc_now_iso()

    def _persist_sync(
        self,
        *,
        step: int,
        status: str,
        usage: UsageAccumulator | None = None,
        cost_usd: float = 0.0,
        ended_at: str | None = None,
    ) -> None:
        try:
            conn = connect(self.db_path, init_schema=True)
            try:
                upsert_subagent_run(
                    conn,
                    run_id=self.run_id,
                    parent_session_id=self.parent_session_id,
                    tool_name=self.tool_name,
                    model_id=self.model_id,
                    tier=self.tier,
                    step=step,
                    max_steps=self.max_steps,
                    status=status,
                    input_tokens=usage.prompt_token_count if usage else 0,
                    output_tokens=usage.candidates_token_count if usage else 0,
                    thought_tokens=usage.thoughts_token_count if usage else 0,
                    cost_usd=cost_usd,
                    started_at=self.started_at,
                    ended_at=ended_at,
                )
            finally:
                conn.close()
        except Exception as exc:
            _logger.debug("SQLite subagent_runs write skipped: %s", exc)

    async def report(
        self,
        step: int,
        max_steps: int,
        message: str,
        usage: UsageAccumulator | None = None,
        *,
        status: str = "running",
        cost_usd: float | None = None,
        force_mcp: bool = False,
        ended_at: str | None = None,
    ) -> None:
        safe_msg = redact_secrets(message)
        billing_source = "gemini_api_free" if self.tier == "free" else "gemini_api_tier1"
        est_cost = (
            cost_usd
            if cost_usd is not None
            else (
                estimate_gemini_api_cost(
                    self.model_id,
                    prompt_token_count=usage.prompt_token_count,
                    candidates_token_count=usage.candidates_token_count,
                    thoughts_token_count=usage.thoughts_token_count,
                    cached_content_token_count=usage.cached_content_token_count,
                    billing_source=billing_source,
                )
                if usage
                else 0.0
            )
        )
        # Channel 1: SQLite WAL (always written in background thread)
        await asyncio.to_thread(
            self._persist_sync,
            step=step,
            status=status,
            usage=usage,
            cost_usd=est_cost,
            ended_at=ended_at,
        )
        # Channel 2: MCP progress + info (throttled ~100 ms)
        if self.ctx is not None:
            now = time.monotonic()
            if force_mcp or (now - self._last_mcp_emit) >= self.throttle_s:
                self._last_mcp_emit = now
                try:
                    if hasattr(self.ctx, "report_progress"):
                        await self.ctx.report_progress(step, max_steps, safe_msg)
                    if hasattr(self.ctx, "info"):
                        await self.ctx.info(safe_msg)
                except Exception as exc:
                    _logger.debug("MCP progress notify skipped: %s", exc)


class SubagentsService:
    """Core implementation of Level 1 and Level 2 subagent tools (testable without stdio transport)."""

    def __init__(
        self,
        *,
        cfg: OgyConfig | None = None,
        router: ResilientGeminiRouter | None = None,
        use_sdk_types: bool = True,
    ) -> None:
        self.cfg = cfg or load_config()
        self.router = router or ResilientGeminiRouter(
            privacy_mode=self.cfg.subagents_privacy_mode,
            fallback_to_tier1=self.cfg.subagents_fallback_to_tier1,
        )
        self.use_sdk_types = use_sdk_types

    async def summarize_stash(
        self,
        hash_prefix: str,
        *,
        focus_query: str = "",
        parent_session_id: str | None = None,
        ctx: Any | None = None,
    ) -> str:
        """Level 1 (Stateless): summarize a verbose `ogy wrap` stashed log in ~15-20 sourced lines."""
        raw_log = recall_stashed_output(hash_prefix, stream="both", stash_dir=self.cfg.stash_dir)
        if raw_log.startswith("[ogy recall]"):
            return raw_log
        bounded_log = redact_secrets(raw_log[:60_000])
        prompt = f"Stashed log (hash={hash_prefix}):\n"
        if focus_query.strip():
            prompt += f"Focus query: {focus_query.strip()}\n\n"
        prompt += f"```\n{bounded_log}\n```"

        model_id = self.cfg.subagents_default_model
        run_id = f"sub-{uuid.uuid4().hex[:12]}"
        reporter = _DualProgressReporter(
            db_path=self.cfg.db_path,
            run_id=run_id,
            tool_name="ogy_summarize_stash",
            model_id=model_id,
            tier="tier1" if self.cfg.subagents_privacy_mode == "tier1_only" else "free",
            max_steps=1,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )
        await reporter.report(0, 1, f"Summarizing stash {hash_prefix[:12]}", force_mcp=True)

        async def _run(client: Any, tier: str) -> Any:
            reporter.tier = tier
            return await run_stateless_generation(
                client,
                model_id=model_id,
                prompt=prompt,
                system_instruction=SYSTEM_SUMMARIZE_STASH,
                thinking_level="minimal",
                max_output_tokens=1024,
                timeout_s=min(30.0, self.cfg.subagents_timeout_s),
                use_sdk_types=self.use_sdk_types,
            )

        try:
            routed = await self.router.execute(model_id=model_id, runner=_run)
            reporter.tier = routed.tier
            await reporter.report(
                1,
                1,
                "Stash summary completed",
                routed.result.usage,
                status="completed",
                cost_usd=routed.cost_usd,
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            out = routed.result.text
            if routed.fallback_notice:
                out = f"{routed.fallback_notice}\n\n{out}"
            return redact_secrets(out)
        except Exception as exc:
            await reporter.report(
                1,
                1,
                f"Failed: {exc}",
                status="failed",
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            raise

    async def build_handoff(
        self,
        goal: str,
        *,
        git_status: str = "",
        last_error: str = "",
        recent_context: str = "",
        parent_session_id: str | None = None,
        ctx: Any | None = None,
    ) -> str:
        """Level 1 (Stateless): generate a calibrated 600-900 token `.ogy/handoff.md`."""
        prompt = (
            f"Goal:\n{goal}\n\n"
            f"Git status:\n{git_status or '(clean)'}\n\n"
            f"Last error / checks:\n{last_error or '(none)'}\n\n"
            f"Recent context:\n{recent_context[:24_000] or '(none)'}\n"
        )
        model_id = self.cfg.subagents_default_model
        run_id = f"sub-{uuid.uuid4().hex[:12]}"
        reporter = _DualProgressReporter(
            db_path=self.cfg.db_path,
            run_id=run_id,
            tool_name="ogy_build_handoff",
            model_id=model_id,
            tier="tier1" if self.cfg.subagents_privacy_mode == "tier1_only" else "free",
            max_steps=1,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )
        await reporter.report(0, 1, "Building semantic handoff", force_mcp=True)

        async def _run(client: Any, tier: str) -> Any:
            reporter.tier = tier
            return await run_stateless_generation(
                client,
                model_id=model_id,
                prompt=redact_secrets(prompt),
                system_instruction=SYSTEM_BUILD_HANDOFF,
                thinking_level="minimal",
                max_output_tokens=1100,
                timeout_s=min(30.0, self.cfg.subagents_timeout_s),
                use_sdk_types=self.use_sdk_types,
            )

        try:
            routed = await self.router.execute(model_id=model_id, runner=_run)
            reporter.tier = routed.tier
            await reporter.report(
                1,
                1,
                "Handoff built",
                routed.result.usage,
                status="completed",
                cost_usd=routed.cost_usd,
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            out = routed.result.text
            if routed.fallback_notice:
                out = f"{routed.fallback_notice}\n\n{out}"
            return redact_secrets(out)
        except Exception as exc:
            await reporter.report(
                1,
                1,
                f"Failed: {exc}",
                status="failed",
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            raise

    async def research_repo(
        self,
        question: str,
        *,
        scope_paths: list[str] | None = None,
        workspace_path: str = ".",
        parent_session_id: str | None = None,
        max_steps: int | None = None,
        timeout_s: float | None = None,
        ctx: Any | None = None,
    ) -> str:
        """Level 2 (Async read-only loop): research the repo and return a concise ~20-line sourced report."""
        root = Path(workspace_path).resolve()
        eff_steps = max_steps or self.cfg.subagents_max_steps
        eff_timeout = timeout_s or self.cfg.subagents_timeout_s
        model_id = self.cfg.subagents_default_model
        run_id = f"sub-{uuid.uuid4().hex[:12]}"
        dispatcher = build_readonly_tool_dispatcher(root, scope_paths=scope_paths)

        reporter = _DualProgressReporter(
            db_path=self.cfg.db_path,
            run_id=run_id,
            tool_name="ogy_research_repo",
            model_id=model_id,
            tier="tier1" if self.cfg.subagents_privacy_mode == "tier1_only" else "free",
            max_steps=eff_steps,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )
        await reporter.report(0, eff_steps, f"Starting research: {question[:60]}", force_mcp=True)

        scope_hint = f"\nScope paths: {', '.join(scope_paths)}" if scope_paths else ""
        prompt = f"Question: {question}{scope_hint}\n\nInspect the repository using read-only tools and return a concise sourced report (~20 lines)."

        async def _run(client: Any, tier: str) -> Any:
            reporter.tier = tier
            return await run_agentic_loop(
                client,
                model_id=model_id,
                prompt=redact_secrets(prompt),
                system_instruction=SYSTEM_RESEARCH_REPO,
                tool_declarations=READONLY_TOOL_DECLARATIONS,
                tool_dispatcher=dispatcher,
                thinking_level="low",
                max_steps=eff_steps,
                timeout_s=eff_timeout,
                on_step=reporter.report,
                use_sdk_types=self.use_sdk_types,
            )

        try:
            routed = await self.router.execute(model_id=model_id, runner=_run)
            reporter.tier = routed.tier
            final_status = "completed" if routed.result.stopped_reason == "completed" else routed.result.stopped_reason
            await reporter.report(
                routed.result.steps_taken,
                eff_steps,
                f"Research finished ({final_status})",
                routed.result.usage,
                status=final_status,
                cost_usd=routed.cost_usd,
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            out = routed.result.text
            if routed.fallback_notice:
                out = f"{routed.fallback_notice}\n\n{out}"
            return redact_secrets(out)
        except Exception as exc:
            await reporter.report(
                0,
                eff_steps,
                f"Failed: {exc}",
                status="failed",
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            raise

    async def triage_failure(
        self,
        error_summary: str,
        *,
        stash_hash: str | None = None,
        workspace_path: str = ".",
        parent_session_id: str | None = None,
        max_steps: int | None = None,
        timeout_s: float | None = None,
        ctx: Any | None = None,
    ) -> str:
        """Level 2 (Async read-only loop): triage a test/build failure and return a ~20-line sourced report."""
        root = Path(workspace_path).resolve()
        eff_steps = max_steps or self.cfg.subagents_max_steps
        eff_timeout = timeout_s or self.cfg.subagents_timeout_s
        model_id = self.cfg.subagents_default_model
        run_id = f"sub-{uuid.uuid4().hex[:12]}"
        dispatcher = build_readonly_tool_dispatcher(root)

        stash_excerpt = ""
        if stash_hash:
            recalled = recall_stashed_output(stash_hash, stream="both", stash_dir=self.cfg.stash_dir)
            if not recalled.startswith("[ogy recall]"):
                stash_excerpt = f"\n\nStashed output ({stash_hash[:12]}):\n```\n{recalled[:20_000]}\n```"

        reporter = _DualProgressReporter(
            db_path=self.cfg.db_path,
            run_id=run_id,
            tool_name="ogy_triage_failure",
            model_id=model_id,
            tier="tier1" if self.cfg.subagents_privacy_mode == "tier1_only" else "free",
            max_steps=eff_steps,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )
        await reporter.report(0, eff_steps, f"Triaging failure: {error_summary[:60]}", force_mcp=True)

        prompt = (
            f"Failure summary:\n{error_summary}{stash_excerpt}\n\n"
            "Use read-only tools to pinpoint the root cause and return a concise sourced report (~20 lines)."
        )

        async def _run(client: Any, tier: str) -> Any:
            reporter.tier = tier
            return await run_agentic_loop(
                client,
                model_id=model_id,
                prompt=redact_secrets(prompt),
                system_instruction=SYSTEM_TRIAGE_FAILURE,
                tool_declarations=READONLY_TOOL_DECLARATIONS,
                tool_dispatcher=dispatcher,
                thinking_level="medium",
                max_steps=eff_steps,
                timeout_s=eff_timeout,
                on_step=reporter.report,
                use_sdk_types=self.use_sdk_types,
            )

        try:
            routed = await self.router.execute(model_id=model_id, runner=_run)
            reporter.tier = routed.tier
            final_status = "completed" if routed.result.stopped_reason == "completed" else routed.result.stopped_reason
            await reporter.report(
                routed.result.steps_taken,
                eff_steps,
                f"Triage finished ({final_status})",
                routed.result.usage,
                status=final_status,
                cost_usd=routed.cost_usd,
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            out = routed.result.text
            if routed.fallback_notice:
                out = f"{routed.fallback_notice}\n\n{out}"
            return redact_secrets(out)
        except Exception as exc:
            await reporter.report(
                0,
                eff_steps,
                f"Failed: {exc}",
                status="failed",
                force_mcp=True,
                ended_at=utc_now_iso(),
            )
            raise


def create_mcp_server(service: SubagentsService | None = None) -> Any:
    """Create and wire the stdio `MCPServer('ogy-subagents')` with `readOnlyHint=True`."""
    try:
        from mcp.server.mcpserver import Context, MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError(
            "Missing optional MCP dependency (`mcp>=2,<3`). "
            'Install from the repository root with: pip install ".[subagents]" '
            '(or: uv tool install --reinstall ".[subagents]")'
        ) from exc

    svc = service or SubagentsService()
    mcp = MCPServer("ogy-subagents")
    ro_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(annotations=ro_annotations)
    async def ogy_summarize_stash(
        hash_prefix: str,
        focus_query: str = "",
        ctx: Context | None = None,
    ) -> str:
        """Level 1 (Stateless): Summarize a verbose `ogy wrap` stashed log in ~15-20 sourced lines."""
        return await svc.summarize_stash(hash_prefix, focus_query=focus_query, ctx=ctx)

    @mcp.tool(annotations=ro_annotations)
    async def ogy_build_handoff(
        goal: str,
        git_status: str = "",
        last_error: str = "",
        recent_context: str = "",
        ctx: Context | None = None,
    ) -> str:
        """Level 1 (Stateless): Produce a calibrated 600-900 token `.ogy/handoff.md`."""
        return await svc.build_handoff(
            goal,
            git_status=git_status,
            last_error=last_error,
            recent_context=recent_context,
            ctx=ctx,
        )

    @mcp.tool(annotations=ro_annotations)
    async def ogy_research_repo(
        question: str,
        scope_paths: list[str] | None = None,
        workspace_path: str = ".",
        parent_session_id: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Level 2 (Read-only async loop): Research the repo and return a concise ~20-line sourced report."""
        return await svc.research_repo(
            question,
            scope_paths=scope_paths,
            workspace_path=workspace_path,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )

    @mcp.tool(annotations=ro_annotations)
    async def ogy_triage_failure(
        error_summary: str,
        stash_hash: str | None = None,
        workspace_path: str = ".",
        parent_session_id: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Level 2 (Read-only async loop): Triage a failure using read-only tools and return a ~20-line report."""
        return await svc.triage_failure(
            error_summary,
            stash_hash=stash_hash,
            workspace_path=workspace_path,
            parent_session_id=parent_session_id,
            ctx=ctx,
        )

    return mcp


def main() -> None:
    """Run the `ogy-subagents` stdio MCP server (zero stdout prints)."""
    try:
        server = create_mcp_server()
    except RuntimeError as exc:
        sys.stderr.write(f"[ogy-subagents] {exc}\n")
        raise SystemExit(1) from exc
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
