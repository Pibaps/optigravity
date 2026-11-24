---
trigger: always_on
---

# Ogy Token Hygiene (< 200 tokens)
1. Locate before reading: search with `grep_search` / `find_by_name` first, then read bounded line ranges (`StartLine`/`EndLine`).
2. Never re-read unchanged files or lockfiles (`package-lock.json`, `Cargo.lock`, `uv.lock`).
3. Bound shell outputs: pipe/filter verbose test or log commands, or use `ogy wrap -- <cmd>`.
4. Batch independent tool calls in parallel in a single turn.
5. Keep final replies concise and factual without filler.
