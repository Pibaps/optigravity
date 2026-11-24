---
name: ogy-budget
description: Manual frugal engineering & context handoff workflow (/ogy-budget).
disable-model-invocation: true
---

# Ogy Frugal Engineering & Handoff

## 3-Step Procedure
1. Call `ogy_session_budget` to inspect `active_context_tokens`, `warm_cache_hit_pct`, `idle_gap_s`, and `advice`.
2. If `advice` is `clear_handoff` or `idle_gap_s >= 300`, call `ogy_write_handoff` (<= 30 lines, preserving negative constraints) and run `/clear`.
3. If `advice` is `compact` (same unbreakable task with >= K* turns left), run `/compact` preserving modified files, negative constraints, and next step.

## Anti-Rationalization Table
| Excuse | Reality |
| :--- | :--- |
| "I'll just read the whole file to be sure" | Wastes prefix tokens; use `grep_search` + bounded range. |
| "Let's compact after 2 failed attempts" | Summarization smooths error signals; `/rewind` or `/clear + handoff` instead. |
| "Switching model mid-session saves money" | Prefix cache busting costs more than remaining turns. |
