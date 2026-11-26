"""Strict `google-genai` SDK wrapper for Gemini 3.5+ subagents (Level 1 & Level 2 async).

Enforces all Gemini 3.5+ SDK invariants:
- `thinking_level` ONLY (`minimal` in N1, `low`/`medium` in N2); automatic `minimal` -> `low`
  translation on `gemini-3.8-flash` and Gemini 4 models.
- NEVER sends `thinking_budget`, `temperature`, `top_p`, or `top_k`.
- Explicitly sets `store=False` (avoids TTFT regression `python-genai#3059`).
- Preserves exact model `Content` objects (including `thought_signature` parts) across
  multi-turn Level 2 tool-calling loops (avoids `400 INVALID_ARGUMENT`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ogy_subagents.secrets import redact_secrets

VALID_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})
FORBIDDEN_GENAI_PARAMS = frozenset({"thinking_budget", "temperature", "top_p", "top_k"})


@dataclass(slots=True)
class UsageAccumulator:
    """Accumulates Gemini API `usage_metadata` counts across turns."""

    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0
    cached_content_token_count: int = 0

    def add_from_response(self, response: Any) -> None:
        meta = getattr(response, "usage_metadata", None)
        if meta is None and isinstance(response, dict):
            meta = response.get("usage_metadata")
        if meta is None:
            return

        def _get_int(obj: Any, name: str) -> int:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            try:
                return max(0, int(val or 0))
            except (TypeError, ValueError):
                return 0

        self.prompt_token_count += _get_int(meta, "prompt_token_count")
        self.candidates_token_count += _get_int(meta, "candidates_token_count")
        self.thoughts_token_count += _get_int(meta, "thoughts_token_count")
        self.cached_content_token_count += _get_int(meta, "cached_content_token_count")


@dataclass(slots=True)
class SubagentLoopResult:
    """Structured result of a Level 1 or Level 2 Gemini API execution."""

    text: str
    steps_taken: int
    max_steps: int
    usage: UsageAccumulator
    tool_calls_made: list[str] = field(default_factory=list)
    stopped_reason: str = "completed"


def normalize_thinking_level(model_id: str, requested_level: str) -> str:
    """Validate and adapt `thinking_level` for Gemini 3.5+ / 3.8 / 4 models.

    Automatically translates `'minimal'` -> `'low'` on `gemini-3.8-flash` and `gemini-4*`.
    """
    lvl = (requested_level or "minimal").strip().lower()
    if lvl not in VALID_THINKING_LEVELS:
        lvl = "low"
    m = (model_id or "").strip().lower()
    if lvl == "minimal" and ("3.8" in m or "gemini-4" in m or "gemini_4" in m):
        return "low"
    return lvl


def build_generate_config(
    *,
    model_id: str,
    thinking_level: str = "minimal",
    system_instruction: str | None = None,
    tool_declarations: list[dict[str, Any]] | None = None,
    max_output_tokens: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a validated Gemini 3.5+ config dictionary strictly enforcing SDK constraints.

    Raises ValueError if any forbidden parameter (`thinking_budget`, `temperature`,
    `top_p`, `top_k`) is passed explicitly.
    """
    forbidden = FORBIDDEN_GENAI_PARAMS.intersection(extra.keys())
    if forbidden:
        raise ValueError(
            f"Forbidden Gemini 3.5+ parameters passed: {sorted(forbidden)}. "
            "Use `thinking_level` only."
        )

    resolved_level = normalize_thinking_level(model_id, thinking_level)
    cfg: dict[str, Any] = {
        "thinking_config": {"thinking_level": resolved_level},
        "store": False,
    }
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if tool_declarations:
        cfg["tools"] = [{"function_declarations": tool_declarations}]
    if max_output_tokens is not None and max_output_tokens > 0:
        cfg["max_output_tokens"] = int(max_output_tokens)
    return cfg


def create_genai_client(api_key: str) -> Any:
    """Lazily instantiate `google.genai.Client(api_key=...)` when `[subagents]` is installed."""
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Missing optional dependency `google-genai`. "
            'Install from the repository root with: pip install ".[subagents]" '
            '(or: uv tool install --reinstall ".[subagents]")'
        ) from exc
    return genai.Client(api_key=api_key)


def _to_sdk_config(cfg_dict: dict[str, Any], *, use_sdk_types: bool = True) -> Any:
    """Convert validated config dict to `google.genai.types.GenerateContentConfig` if available."""
    if not use_sdk_types:
        return cfg_dict
    try:
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError:
        return cfg_dict

    kwargs: dict[str, Any] = {
        "thinking_config": types.ThinkingConfig(
            thinking_level=cfg_dict["thinking_config"]["thinking_level"]
        ),
    }
    if "system_instruction" in cfg_dict:
        kwargs["system_instruction"] = cfg_dict["system_instruction"]
    if "tools" in cfg_dict:
        kwargs["tools"] = cfg_dict["tools"]
    if "max_output_tokens" in cfg_dict:
        kwargs["max_output_tokens"] = cfg_dict["max_output_tokens"]
    # Pass store=False explicitly when supported by installed google-genai version
    try:
        return types.GenerateContentConfig(store=False, **kwargs)
    except TypeError:
        return types.GenerateContentConfig(**kwargs)


def _extract_candidate_content(response: Any) -> Any:
    """Return the exact first candidate `Content` object (preserving thought signatures)."""
    candidates = getattr(response, "candidates", None)
    if candidates is None and isinstance(response, dict):
        candidates = response.get("candidates")
    if candidates:
        first = candidates[0]
        return first.get("content") if isinstance(first, dict) else getattr(first, "content", None)
    return None


def _extract_function_calls(response: Any, candidate_content: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract `(tool_name, args_dict)` pairs from a Gemini response."""
    calls: list[tuple[str, dict[str, Any]]] = []
    direct = getattr(response, "function_calls", None)
    if direct is None and isinstance(response, dict):
        direct = response.get("function_calls")
    if direct:
        for fc in direct:
            name = fc.get("name") if isinstance(fc, dict) else getattr(fc, "name", "")
            args = fc.get("args") if isinstance(fc, dict) else getattr(fc, "args", {})
            if name:
                calls.append((str(name), dict(args or {})))
        return calls

    if candidate_content is not None:
        parts = candidate_content.get("parts") if isinstance(candidate_content, dict) else getattr(candidate_content, "parts", None)
        if parts:
            for part in parts:
                fc = part.get("function_call") if isinstance(part, dict) else getattr(part, "function_call", None)
                if fc:
                    name = fc.get("name") if isinstance(fc, dict) else getattr(fc, "name", "")
                    args = fc.get("args") if isinstance(fc, dict) else getattr(fc, "args", {})
                    if name:
                        calls.append((str(name), dict(args or {})))
    return calls


def _extract_text(response: Any, candidate_content: Any) -> str:
    """Extract textual response from Gemini response or candidate parts."""
    txt = getattr(response, "text", None)
    if txt is None and isinstance(response, dict):
        txt = response.get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    if candidate_content is not None:
        parts = candidate_content.get("parts") if isinstance(candidate_content, dict) else getattr(candidate_content, "parts", None)
        if parts:
            chunks: list[str] = []
            for part in parts:
                is_thought = part.get("thought") if isinstance(part, dict) else getattr(part, "thought", False)
                if is_thought:
                    continue
                pt = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if isinstance(pt, str) and pt:
                    chunks.append(pt)
            if chunks:
                return "\n".join(chunks).strip()
    return ""


def _build_function_response_content(tool_results: list[tuple[str, str]], *, use_sdk_types: bool = True) -> Any:
    """Build a user Content message carrying function_response parts."""
    if use_sdk_types:
        try:
            from google.genai import types  # type: ignore[import-not-found]

            parts = [
                types.Part.from_function_response(name=name, response={"result": result})
                for name, result in tool_results
            ]
            return types.Content(role="user", parts=parts)
        except Exception:
            pass
    return {
        "role": "user",
        "parts": [
            {"function_response": {"name": name, "response": {"result": result}}}
            for name, result in tool_results
        ],
    }


def _build_user_text_content(prompt: str, *, use_sdk_types: bool = True) -> Any:
    if use_sdk_types:
        try:
            from google.genai import types  # type: ignore[import-not-found]

            return types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        except Exception:
            pass
    return {"role": "user", "parts": [{"text": prompt}]}


async def run_stateless_generation(
    client: Any,
    *,
    model_id: str,
    prompt: str,
    system_instruction: str,
    thinking_level: str = "minimal",
    max_output_tokens: int | None = None,
    timeout_s: float = 30.0,
    use_sdk_types: bool = True,
) -> SubagentLoopResult:
    """Level 1 stateless generation (1 call, 0 disk/shell tools, `thinking_level='minimal'`)."""
    cfg_dict = build_generate_config(
        model_id=model_id,
        thinking_level=thinking_level,
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
    )
    sdk_cfg = _to_sdk_config(cfg_dict, use_sdk_types=use_sdk_types)
    usage = UsageAccumulator()

    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model_id,
            contents=prompt,
            config=sdk_cfg,
        ),
        timeout=timeout_s,
    )
    usage.add_from_response(response)
    content = _extract_candidate_content(response)
    text = redact_secrets(_extract_text(response, content))
    return SubagentLoopResult(
        text=text,
        steps_taken=1,
        max_steps=1,
        usage=usage,
        stopped_reason="completed",
    )


StepCallback = Callable[[int, int, str, UsageAccumulator], Awaitable[None] | None]


async def run_agentic_loop(
    client: Any,
    *,
    model_id: str,
    prompt: str,
    system_instruction: str,
    tool_declarations: list[dict[str, Any]],
    tool_dispatcher: dict[str, Callable[[dict[str, Any]], str]],
    thinking_level: str = "low",
    max_steps: int = 8,
    timeout_s: float = 45.0,
    on_step: StepCallback | None = None,
    use_sdk_types: bool = True,
) -> SubagentLoopResult:
    """Level 2 async `while` loop on `client.aio` with read-only tools and thought-signature preservation."""
    cfg_dict = build_generate_config(
        model_id=model_id,
        thinking_level=thinking_level,
        system_instruction=system_instruction,
        tool_declarations=tool_declarations,
    )
    sdk_cfg = _to_sdk_config(cfg_dict, use_sdk_types=use_sdk_types)
    usage = UsageAccumulator()
    contents: list[Any] = [_build_user_text_content(prompt, use_sdk_types=use_sdk_types)]
    tool_calls_made: list[str] = []
    step = 0
    final_text = ""
    stopped_reason = "max_steps"

    async def _emit(cur_step: int, msg: str) -> None:
        if on_step is not None:
            res = on_step(cur_step, max_steps, msg, usage)
            if asyncio.iscoroutine(res):
                await res

    async def _loop() -> None:
        nonlocal step, final_text, stopped_reason
        while step < max_steps:
            step += 1
            await _emit(step, f"Step {step}/{max_steps}: querying {model_id}")
            response = await client.aio.models.generate_content(
                model=model_id,
                contents=contents,
                config=sdk_cfg,
            )
            usage.add_from_response(response)
            candidate_content = _extract_candidate_content(response)
            # CRITICAL: preserve the exact candidate Content object (including thought signatures!)
            if candidate_content is not None:
                contents.append(candidate_content)

            fn_calls = _extract_function_calls(response, candidate_content)
            if not fn_calls:
                final_text = _extract_text(response, candidate_content)
                stopped_reason = "completed"
                await _emit(step, f"Step {step}/{max_steps}: completed")
                return

            tool_results: list[tuple[str, str]] = []
            for tool_name, tool_args in fn_calls:
                tool_calls_made.append(tool_name)
                handler = tool_dispatcher.get(tool_name)
                if handler is None:
                    result_str = f"Error: tool '{tool_name}' is not allowed (read-only allowlist)."
                else:
                    try:
                        result_str = await asyncio.to_thread(handler, tool_args)
                    except Exception as exc:
                        result_str = f"Tool error ({tool_name}): {redact_secrets(str(exc))}"
                tool_results.append((tool_name, redact_secrets(result_str)))

            called_summary = ", ".join(name for name, _ in fn_calls)
            await _emit(step, f"Step {step}/{max_steps}: ran {called_summary}")
            contents.append(
                _build_function_response_content(tool_results, use_sdk_types=use_sdk_types)
            )

        # If loop hit max_steps without a final text response, extract any text or synthesize fallback
        if not final_text:
            final_text = (
                f"Reached max_steps ({max_steps}) after running tools: "
                + (", ".join(tool_calls_made) or "none")
                + "."
            )

    try:
        await asyncio.wait_for(_loop(), timeout=timeout_s)
    except asyncio.TimeoutError:
        stopped_reason = "timeout"
        if not final_text:
            final_text = f"Subagent timed out after {timeout_s:.0f}s at step {step}/{max_steps}."
        await _emit(step, f"Timeout after {timeout_s:.0f}s")

    return SubagentLoopResult(
        text=redact_secrets(final_text),
        steps_taken=step,
        max_steps=max_steps,
        usage=usage,
        tool_calls_made=tool_calls_made,
        stopped_reason=stopped_reason,
    )
