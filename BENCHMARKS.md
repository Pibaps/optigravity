# Optigravity (`ogy`) — A/B Benchmarks

Rigorous A/B benchmarks comparing **native Antigravity (`agy`)** against **Optigravity (`ogy`)** under identical model, effort, and isolated workspace conditions.

## Methodology & Isolation

* **CLI & Model**: `agy 1.5.16`, `--model gemini-4-hydrogen-medium`, `--effort medium`.
* **Environment Isolation**:
  * **Baseline (`agy` natif)**: `ogy-hygiene.md`, `ogy-budget` skill, `mcp_config.json`, and `ogy-firebreaks` disabled/removed; only passive telemetry (`ogy-telemetry-and-budget`) active to record metrics in `ogy.db`.
  * **Optigravity (`ogy` sans firebreaks)**: `ogy run` with `ogy-hygiene.md`, `ogy-budget`, and `mcp_config.json` enabled; `ogy-firebreaks` disabled.
  * **Optigravity (`ogy` avec firebreaks)**: `ogy run` with all rules/skills/MCP and `ogy-firebreaks` (`PreToolUse` returning `{"decision": "allow"}` / `deny`) enabled.
* **Metrics**: Extracted via `ogy stats --json`, `ogy waste --json`, `ogy.db` (`tool_calls`, `hook_events`), wall-clock execution time, `git diff --stat`, and independent `pytest` verification.

---

## 1. Synthetic Multi-Turn Scenarios (Greenfield TDD & Noisy Repo)

* **Scenario 1 (Greenfield TDD + Refactor)**: Create a Python `ledger` package (`models.py`, `calculator.py`, `exporter.py`), write ≥ 12 `pytest` tests, run `pytest -vv`, refactor `apply_discount` to `decimal.Decimal` with `rounding` mode, and re-run `pytest -vv`.
* **Scenario 2 (Noisy Repo Inspection & Fix)**: Repo containing a **209 KB** `package-lock.json` (600 packages), a **122 KB** `logs/app.log` (1,205 lines with `PaymentGatewayTimeout` at line 847), and a failing `scripts/check.py` caused by `GATEWAY_TIMEOUT_MS = 50` in `src/config.py`.

| Metric | S1 `agy` | S1 `ogy` (FB ON) | Delta S1 | S2 `agy` | S2 `ogy` (FB ON) | Delta S2 |
| --- | --- | --- | --- | --- | --- | --- |
| **Session ID** | `0917f7cb` | `06e64272` | — | `e879a1b4` | `ca7efac1` | — |
| **Wall Latency** | 73.10 s | 74.16 s | +1.5 % | 32.35 s | **26.43 s** | **-18.3 %** |
| **Turns (`turn_count`)** | 8 | 8 | 0.0 % | 5 | **4** | **-20.0 %** |
| **Tool Calls** | 36 | 36 | 0.0 % | 22 | **12** | **-45.5 %** |
| **Input Tokens** | 31,972 | **31,334** | **-2.0 %** | 48,011 | **19,784** | **-58.8 %** |
| **Output Tokens** | 6,718 | **6,602** | **-1.7 %** | 1,973 | **1,626** | **-17.6 %** |
| **Estimated Cost (USD)** | \$0.0983 | **\$0.0965** | **-1.9 %** | \$0.0868 | **\$0.0419** | **-51.8 %** |
| **External Verification** | 26/26 PASS | 28/28 PASS | 100 % | `CHARGE_OK` | `CHARGE_OK` | 100 % |

**Takeaway**: On clean greenfield code, `ogy` is neutral (-2.0 % tokens). On noisy repositories, `agy` read `logs/app.log` (122 KB) directly via `read_file` before still needing `Select-String`, whereas `ogy` targeted both `logs/app.log` and `package-lock.json` directly with `Select-String`, cutting input tokens by **-58.8 %** and cost by **-51.8 %**.

---

## 2. Real Medium Repository: `pallets/click` (3-Arm A/B/C)

* **Repo**: `pallets/click` (`src/click/core.py` = 160 KB, 2,556 tests).
* **Task**: Implement command `aliases` support on `click.Group` (`__init__`, `get_command`, `format_commands`, `--help` display, edge-case handling) and add `tests/test_aliases.py`.

| Metric | 1. `agy` natif | 2. `ogy` (no firebreaks) | 3. `ogy` (with firebreaks) |
| --- | --- | --- | --- |
| **Session ID** | `a6fea731` | `24f11caa` | `701c0ec8` |
| **Wall Latency** | 107.80 s | 100.25 s (-7.0 %) | **89.08 s (-17.4 %)** |
| **Turns (`turn_count`)** | 11 | **9 (-18.2 %)** | 10 (-9.1 %) |
| **Tool Calls** | 34 | 31 | **30 (-11.8 %)** |
| **Input Tokens** | 44,088 | **39,563 (-10.3 %)** | 41,318 (-6.3 %) |
| **Output Tokens** | 8,721 | **7,840 (-10.1 %)** | 8,235 (-5.6 %) |
| **Estimated Cost (USD)** | \$0.1315 | **\$0.1181 (-10.2 %)** | \$0.1237 (-5.9 %) |
| **`git diff` (`core.py`)** | `+26 / -2` | `+22 / -2` | `+26 / -2` |
| **Full `pytest` Suite** | **2,556 passed** | **2,556 passed** | **2,556 passed** |

**Takeaway**: Zero quality loss — all 3 arms passed 100 % of the 2,556 existing tests plus 6 new alias tests. On a clean ~17-file codebase, `ogy-hygiene.md` alone saved **-10.3 %** input tokens, while adding firebreaks reduced latency by **-17.4 %** (~8.7 ms/hook overhead).

---

## 3. Large Real Repository: `yt-dlp/yt-dlp` (3-Arm A/B/C)

* **Repo**: `yt-dlp/yt-dlp` (1,500+ Python files; `YoutubeDL.py` = 200 KB, `utils/_utils.py` = 180 KB, `options.py` = 100 KB).
* **Task**: Add `--max-title-length` across `yt_dlp/options.py`, `yt_dlp/__init__.py`, `yt_dlp/YoutubeDL.py`, verify/fix `limit_length` edge cases in `yt_dlp/utils/_utils.py`, create `test/test_max_title_length.py`, and run `pytest test/test_max_title_length.py test/test_utils.py -vv`. *(For the two `ogy` arms in headless `-p` mode, `--mode accept-edits` was passed to avoid interactive `--mode plan` pausing on `implementation_plan.md`.)*

| Metric | 1. `agy` natif | 2. `ogy` (no firebreaks) | 3. `ogy` (with firebreaks) |
| --- | --- | --- | --- |
| **Session ID** | `b9611397` | `4f8ac9f7` | `f072d921` |
| **Wall Latency** | 219.75 s | **181.72 s (-17.3 %)** | *N/A* *(parent pipe stayed open after `completed`)* |
| **Turns (`turn_count`)** | 24 | 18 (-25.0 %) | **15 (-37.5 %)** |
| **Tool Calls** | 102 | **66 (-35.3 %)** | 82 (-19.6 %) |
| **Input Tokens** | 83,140 | 66,870 (-19.6 %) | **58,440 (-29.7 %)** |
| **Output Tokens** | 13,990 | 12,278 (-12.2 %) | **12,250 (-12.4 %)** |
| **Estimated Cost (USD)** | \$0.2296 | \$0.1924 (-16.2 %) | **\$0.1795 (-21.8 %)** |
| **`W6` Duplicate Read Alerts** | 7 files | **4 files** | 5 files |
| **Files Modified (`git diff`)** | 4 files (`+36`) | 4 files (`+38`) | **4 files (`+29`)** |
| **`pytest` Verification** | **129 passed** | **127 passed** | **130 passed** |

**Takeaway**: Larger repositories amplify Optigravity's gains. With firebreaks enabled, `ogy` cut turns by **-37.5 %**, input tokens by **-29.7 %**, and cost by **-21.8 %**, while producing the most concise diff (`+29` lines) and passing **130/130** tests.

---

## Observed Protocol & Telemetry Findings

1. **`PreToolUse` Allow Format (`agy 1.5.16`)**: `agy` denies any `PreToolUse` hook returning `{"decision": ""}` or `{}`. Fixing `ogy_core/firebreaks.py` and `ogy_hooks/cli.py` to return `{"decision": "allow"}` resolved the block with ~8.2–8.7 ms hook latency.
2. **Headless `-p` & `--mode plan`**: In non-interactive `-p` runs, routing to `--mode plan` causes `agy` to stop after writing `implementation_plan.md`; pass `--mode accept-edits` for headless execution.
3. **Remaining Telemetry Gaps**: `PostToolUse` in `agy 1.5.16` does not include raw tool output bytes, leaving `raw_output_bytes = 0` in `ogy.db` unless captured via `ogy wrap` or transcript ingestion; headless `-p` turns also reported `cache_read_tokens = 0`.
