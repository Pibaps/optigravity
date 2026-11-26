# Optigravity (`ogy`) — User Manual

**Optigravity (`ogy`)** is a token-efficiency, context-health, routing, telemetry, and hybrid subagent companion for **Google Antigravity CLI (`agy`)**.  
It reduces billable token usage while preserving the native `agy` TUI, OAuth quotas, interactive workflows, and ToS Section 6 compliance.

---

## Table of Contents

1. [Requirements & Installation](#1-requirements--installation)
2. [Quick Start](#2-quick-start)
3. [CLI Reference (`ogy`)](#3-cli-reference-ogy)
4. [Interactive ACP TUI (`ogy tui`)](#4-interactive-acp-tui-ogy-tui)
5. [Task Routing (Stage 1 & Stage 2)](#5-task-routing-stage-1--stage-2)
6. [Context Health & Pricing Policy](#6-context-health--pricing-policy)
7. [Real-Time Statusline HUD](#7-real-time-statusline-hud)
8. [Fail-Open Hooks & Zero-Friction Firebreaks](#8-fail-open-hooks--zero-friction-firebreaks)
9. [Shell Compression (`ogy wrap`, `ogy raw` & `ogy recall`)](#9-shell-compression-ogy-wrap-ogy-raw--ogy-recall)
10. [Minimal MCP Server (`ogy-mcp`)](#10-minimal-mcp-server-ogy-mcp)
11. [Hybrid Gemini API Subagents (`ogy-subagents`)](#11-hybrid-gemini-api-subagents-ogy-subagents)
12. [Configuration (`config.toml`)](#12-configuration-configtoml)
13. [Troubleshooting & Safety](#13-troubleshooting--safety)
14. [A/B Benchmarks Summary](#14-ab-benchmarks-summary)

---

## 1. Requirements & Installation

* **Python**: 3.12+ (Windows, macOS & Linux)
* **Google Antigravity CLI**: `agy` installed and available on `PATH`
* **Package installer**: [`uv`](https://docs.astral.sh/uv/) (recommended), `pipx`, or standard `pip`

Optigravity uses **modular PEP 621 packaging** so the base V1 CLI, hooks, and statusline require **zero third-party dependencies** (`100%` Python stdlib, `< 20 ms` hot path):

| Target | Command | Included Capabilities |
| :--- | :--- | :--- |
| **Base (V1 stdlib)** | `uv tool install .` or `pip install .` | `ogy run`, `ogy-hook`, `statusline`, `wrap`/`raw`/`recall`, `init`/`uninstall`/`doctor`/`config`, `stats`/`waste` |
| **Interactive TUI** | `uv tool install ".[tui]"` | Adds `ogy tui` / `ogy chat` (`textual>=8.0`, `agent-client-protocol>=1.0`) |
| **Minimal MCP** | `uv tool install ".[mcp]"` | Adds `ogy-mcp` (`mcp>=2,<3`) |
| **Hybrid Subagents** | `uv tool install ".[subagents]"` | Adds `ogy-subagents` (`google-genai[aiohttp]`, `mcp>=2,<3`, `keyring`) |
| **Full Suite** | `uv tool install ".[all]"` | Everything (`mcp` + `tui` + `subagents`) |
| **Development** | `uv sync --all-extras` | Full suite + `pytest` & `pytest-asyncio` |

This installs four entry points:
* `ogy` — Main user CLI (lazy-loads optional extras with clear install hints)
* `ogy-hook` — Fail-open hook & statusline dispatcher (100 % Python stdlib)
* `ogy-mcp` — Minimal stdio MCP server (`[mcp]`)
* `ogy-subagents` — Hybrid Gemini API subagent MCP server (`[subagents]`)

---

## 2. Quick Start

```bash
# 1. Verify your local environment, configs, SQLite WAL & optional Gemini API key
ogy doctor --probe-agy

# 2. Preview or run the guided setup wizard (creates *.ogy.bak backups & config.toml)
ogy init --dry-run
ogy init

# 3. Launch agy through the Optigravity router (native TUI)
ogy run "Fix typo in README"

# 4. Or launch the interactive V2/V3 ACP TUI with live diffs, @file & subagent telemetry
ogy tui
```

---

## 3. CLI Reference (`ogy`)

* `ogy --version` (`-V`) — Print the installed Optigravity version (single source of truth via `importlib.metadata`).

### `ogy run [OPTIONS] [PROMPT]`
Routes your prompt through the 2-stage router (using latest quota snapshots and `pending_tasks` correlation), sets `AGY_CLI_DISABLE_AUTO_UPDATE=true`, `OGY_TASK_ID`, and `OGY_SESSION_META`, and launches `agy` with direct TTY inheritance (no PTY proxy).

* `--model <slug>` — Override routed model (e.g., `gemini-4-hydrogen`, `gemini-4-neon`, `claude-opus-5.5`)
* `--effort <level>` — Override effort (`low`, `medium`, `high`, `xhigh`)
* `--mode <mode>` — Override execution mode (`plan`, `accept-edits`, `auto`)
* `-p`, `--print` — Non-interactive print mode
* `--sandbox` — Pass `--sandbox` to `agy`
* `--dry-run` — Print the resolved `agy` command and routing metadata without launching

### `ogy tui [OPTIONS] [PROMPT]` (alias: `ogy chat`)
Launches the interactive Textual 8+ TUI backed by `agy acp` (ACP v1 JSON-RPC 2.0 over stdio). See [Interactive ACP TUI (`ogy tui`)](#4-interactive-acp-tui-ogy-tui).

* `--cwd <path>` — Workspace directory (default: `.`)
* `--sandbox`, `--add-dir <path>` (repeatable), `--agent <name>`, `--log-file <path>`
* `--auth-mode <mode>` — Auth mode (`oauth-personal` default, or `api-key`)
* `--model <slug>` / `--effort <level>` — Pin initial model/effort (skips 1st-prompt Stage 1 auto-routing)
* `--lang <fr|en>` — Interface language (`fr` default, or `en`; switchable live via `/lang`)

### `ogy init [OPTIONS]` & `ogy uninstall [OPTIONS]`
* `ogy init [--dry-run] [--enable-firebreaks] [--non-interactive] [--lang en|fr] [--privacy-mode tier1_only|free_first|disabled]`
  Guided, idempotent setup installing `statusLine`, `hooks.json`, lazy MCP entries (`optigravity` + optional `ogy-subagents`, tagged `ogy-managed`), `ogy-hygiene.md`, `ogy-budget/SKILL.md`, and a commented `~/.config/optigravity/config.toml`.
* `ogy uninstall [--purge-data] [--restore-backups] [--dry-run]`
  Cleanly removes only `ogy-managed` entries without touching other user extensions, optionally restoring `.ogy.bak` backups or purging `ogy.db` / stash.

### `ogy doctor [--probe-agy] [--fix] [-y]`
Checks `agy` availability, JSON validity (`settings.json`, `hooks.json`, `mcp_config.json`), `config.toml` validation, `secrets.env` permissions (`0600` on POSIX), SQLite WAL integrity (`PRAGMA quick_check` + `user_version`), measured `ogy-hook` hot-path latency (`< 25 ms` target), and 0-token Gemini API connectivity. Pass `--fix` to repair deterministic anomalies automatically.

### `ogy config [show|get|set|validate|edit] [key] [value] [--json]`
Inspects typed configuration values with provenance (`default`, `config.toml`, `env`), updates keys with validation, or opens `config.toml` in `$EDITOR`.

### `ogy stats [--session <id>] [--last N] [--since 7d|24h|YYYY-MM-DD] [--repo <subpath>] [--json]`
Displays an ROI-first dashboard (read-only SQLite connection, respects `NO_COLOR`) showing estimated tokens/cost saved via cache & `ogy wrap`, OAuth session KPIs (`v_session_kpis`), and a separate **MCP Subagents** block (Free Tier vs Tier 1 calls and API USD cost).

### `ogy waste [--session <id>] [--last N] [--since ...] [--repo ...] [--json]`
Runs the 8 automated waste-detection queries (`W1`–`W8`: low cache hit, retry loops, obese tool outputs, reasoning over-effort, context rot risk, duplicate reads, MCP schema bloat, subagent drift) and presents actionable remediation advice sorted by impact.

### `ogy router-eval [--category <cat>] [--min-samples <n>] [--json]`
Runs an offline shadow evaluation of Stage 2 bandit arms vs Stage 1 on historical `routing_decisions` in `ogy.db`.

### `ogy audit mcp [--json]`
Inspects `mcp_config.json` files for `eager: true` servers and UI-only tools (`_meta.ui.visibility: ["app"]`) leaking into CLI context.

### `ogy wrap -- <cmd>`, `ogy raw -- <cmd>` & `ogy recall <hash> [--lines A:B] [--stream stdout|stderr|both]`
See [Shell Compression](#9-shell-compression-ogy-wrap-ogy-raw--ogy-recall).

---

## 4. Interactive ACP TUI (`ogy tui`)

`ogy tui` (modularized into `screens/`, `widgets/`, `state/`, and `styles.tcss`) connects directly to `agy acp` over JSON-RPC 2.0 (`stdio`) with a non-blocking read-only SQLite telemetry worker (`@work(thread=True, exclusive=True)`).

### Key Capabilities
* **Pro Multiline Composer (`PromptTextArea`)**: Auto-growing 1–8 line editor (`Enter` submits or inserts newline via `tui.enter_behavior`; `Shift+Enter`, `Ctrl+J`, `Alt+Enter`, `\`+`Enter` for newlines; `Ctrl+G` opens `$EDITOR`), SQLite-persisted prompt history (`↑`/`↓` cursor-first, `Ctrl+R` reverse search), **paste-collapse** (`>= 8` lines or `>= 600` chars collapsed into `[Pasted #N · X lines]`), and async **`@file` fuzzy autocomplete** (with file sizes, firebreak threshold indicator, and `Tab` tree view — inserts relative path only, **0 token bloat**).
* **Fluid Streaming & Zero Scroll Hijacking**: Uses Textual 8+ `Markdown.get_stream()` + `Widget.anchor()` / `release_anchor()` with a floating `↓ New messages · End` pill when scrolling up, plus raw unwrapped Markdown copy actions.
* **Quiet-by-Default Transparency**: Consecutive exploration calls (`read`/`search`/`fetch`) fold into a single-line `ActivityGroup` (`Ctrl+O` toggles `low`/`high` verbosity). Every `ToolCard` is expandable even without `rawOutput` (reveals `rawInput`), file edits render **inline colored unified diffs**, errors stay expanded automatically, and `ThoughtBlock` auto-collapses once output starts.
* **Keyboard-First Modals**:
  * `PermissionModal`: displays exact shell command or colored diff with risk badge and 1-key shortcuts (`Y`/`1` allow once, `A`/`2` allow always, `N`/`Esc` reject once, `X` reject always, `F` fullscreen).
  * `ElicitationModal`: full ACP `form` (dynamic JSON Schema fields) and `url` (explicit hostname consent) support.
  * `SessionPickerModal` (`Ctrl+S` / `/resume`) & `HandoffReviewModal` (`/rotate`): strictly isolated `SessionTranscript` per `session_id` via `ContentSwitcher`, editable handoff preview with token counter.
* **Live Telemetry Sidebar & Concrete `[D] > 60K` Delegation**: Gauges for Context, Prompt Cache, and OAuth quotas (`Gemini 5h/7d`, `3p-5h`/`3p-weekly`), live `parent → subagent` progress tree, separate **OAuth Quota** vs **Gemini API USD** counters, and 1-key `[D]` delegation modal.

### Slash Commands & Shortcuts
* `/model [slug]`, `/effort [low|medium|high|xhigh|max]`, `/mode [default|accept-edits|auto|plan]`
* `/btw <question>` — Concise side question without modifying files or derailing the active task
* `/lang [fr|en]` — Live FR/EN interface switch
* `/rotate` (`/clear`), `/handoff`, `/new` (`Ctrl+N`), `/sessions`, `/resume <id>` (`Ctrl+S`)
* `/glossary` (`F1`), `/help`
* `Ctrl+P` Command Palette · `Ctrl+O` Verbosity toggle · `Ctrl+B` Telemetry sidebar · `Ctrl+C` Safe turn cancel (`session/cancel`) · `Ctrl+Q` Clean quit

---

## 5. Task Routing (Stage 1 & Stage 2)

Optigravity follows a **stickiness rule**: model and effort are chosen at launch (or subagent boundary) and never changed mid-session to preserve the prompt prefix cache.

* **Stage 1 (Deterministic Heuristic, `< 5 ms`)**: Maps 8 features + latest quota snapshots to `trivial_edit`, `lookup_qa`, `routine_code`, `bugfix_local`, `bugfix_complex`, `refactor_multifile`, or `architecture_security` (auto-downgrading effort by one step when active quota `< 15%`, except `architecture_security`).
* **Stage 2 (SQLite UCB1 Contextual Bandit)**: Activates after `>= 15` qualified tasks per category (`eta = 0.92`, excluding arms `< 80%` success rate), fed in V1 via atomic `pending_tasks` correlation between `ogy run` and `session-start`.

---

## 6. Context Health & Pricing Policy

* **Green Zone** (`< 60K` tokens, `< 15` turns, cache hit `>= 60%`): `keep_warm`.
* **Amber Zone** (`60K–100K` tokens or `15–25` turns): `frugal_btw`.
* **Red Zone** (`>= 100K` tokens, `exceeds_200k`, `idle_gap > 180s` above `60K`, or `>= 2` consecutive failures): `clear_handoff` by default (`compact` only if remaining turns `>= K*` without errors; `rewind` after `>= 2` consecutive failures).
* **Pricing Catalogue (`ogy_core/pricing.py`)**: Covers Gemini 4 (`Hydrogen`, `Neon`, `Carbon`/`Argon` with `2026-12-01` switch), Claude 5.5 (`Opus`, `Sonnet`, `Haiku` with `100K` 5× cliff), and Gemini API subagent routes (`gemini-3.5-flash-lite` `$0.30/$2.50`, `gemini-3.1-flash-lite`, `gemini-3.6/3.8-flash` with `2027-01-01` 2× switch, `gemini-3.1-pro`) with separate `billing_source` tracking (`oauth_quota`, `gemini_api_free`, `gemini_api_tier1`) and full `thoughts_token_count` billing.

---

## 7. Real-Time Statusline HUD

Configured in `~/.gemini/antigravity-cli/settings.json` (`ogy-hook statusline`), rendering a 1-line ANSI HUD (respects `NO_COLOR`, adapts to terminal width) with 10-second debounced SQLite snapshots and read-only DB fallback.

```text
ogy | cache 92% | ctx ██░░░░ 64K (13%) | $0.24 | 5h ████░ 88% | ▲ WARN
```

---

## 8. Fail-Open Hooks & Zero-Friction Firebreaks

`ogy-hook <event>` imports **only the Python standard library** and wraps `main()` in `try/except BaseException` so a hook failure never blocks `agy`.

* `session-start` — Registers the session, correlates `pending_tasks` from `ogy run`, and injects `.ogy/handoff.md` once via `injectSteps`.
* `pre-tool` — Always returns `{"decision": "allow"}` on pass-through (verified for `agy 1.5.16`), with zero-friction rules:
  1. **Anti-loop**: denies after `>= 3` identical failing tool calls.
  2. **Adaptive file reads**: blocks lockfiles and unbounded reads exceeding typed thresholds (**48 KB** docs/config, **24 KB** source code, **8 KB** logs/JSON/data), reporting exact file line counts and tool-aware range parameters (`StartLine`/`EndLine`).
  3. **Lexical shell tokenizer & `bounded-aware` wrap**: splits compound sequences (`&&`, `||`, `;`, `|`) surgically, passes through redirections/subshells/PowerShell blocks and already-bounded commands (`pytest -q/--tb=short`, `git log -n/--oneline`, `git diff --stat`, `git status -s`), supports `OGY_NO_WRAP=1` / `ogy raw --`, and silently rewrites verbose segments via `overwrite.CommandLine`.
* `post-tool`, `pre-invocation`, `post-invocation`, `stop` — Telemetry ingestion (with `camelCase`/`snake_case` normalization and read-only `conversations/<id>.db` enrichment), hard budget cap enforcement, reward calculation, and WAL maintenance.

---

## 9. Shell Compression (`ogy wrap`, `ogy raw` & `ogy recall`)

* `ogy wrap -- <cmd>` drains `stdout` and `stderr` in parallel via dual daemon threads (deadlock-free on Windows & POSIX), preserves exact exit codes, displays a live single-line spinner on TTY `stderr` (0 ANSI when non-TTY), passes through small outputs (`< 40` lines and `< 3 KB`) untouched, stashes separated `stdout.log` / `stderr.log` / `meta.json` under `~/.cache/optigravity/stash/<hash>/`, and runs opportunistic LRU garbage collection (`7 days` / `200 MB`).
* `ogy raw -- <cmd>` executes a command directly without compression.
* `ogy recall <hash_prefix> [--lines A:B] [--stream stdout|stderr|both]` retrieves bounded slices of stashed output.

---

## 10. Minimal MCP Server (`ogy-mcp`)

Registered with `eager: false` in `mcp_config.json` and exposes 3 concise tools:
* `ogy_session_budget(session_id?)` — Active context tokens, warm cache hit %, idle gap, top waste, and recommended action.
* `ogy_recall(hash_prefix, start_line=1, end_line=100)` — Bounded read of stashed `ogy wrap` output.
* `ogy_write_handoff(...)` — Writes a normalized `<= 30`-line `.ogy/handoff.md`.

---

## 11. Hybrid Gemini API Subagents (`ogy-subagents`)

Keeps your main agent on `oauth-personal` (Google AI Pro/Ultra) while offloading high-volume, read-only, or summarization workloads to a dedicated local MCP server authenticated via `GEMINI_API_KEY` (Free Tier or Tier 1 Cloud Credits, defaulting to **`gemini-3.5-flash-lite`** at `$0.30/$2.50` per 1M tokens).

* **Level 1 (Stateless, 0 disk/shell access)**:
  * `ogy_summarize_stash(hash_prefix, focus_query?)` — Concise 15–20 line sourced summary of a large `ogy wrap` log.
  * `ogy_build_handoff(...)` — Semantic `.ogy/handoff.md` calibrated to 600–900 tokens.
* **Level 2 (Read-only async `google-genai` loop, `max_steps=8`, `timeout=45s`, `readOnlyHint: true`)**:
  * `ogy_research_repo(question, scope_paths?)` & `ogy_triage_failure(error_summary, stash_hash?)` using whitelisted read-only tools (`rg_search`, `read_slice`, `list_files`, `git_status`) with `thinking_level`, `store=False`, and intact thought signatures.
* **429 Resilience, Privacy & Secrets**:
  * 3-step secrets cascade: Env (`OGY_GEMINI_API_KEY_FREE`, `OGY_GEMINI_API_KEY_TIER1`, `GEMINI_API_KEY`) → OS `keyring` → `~/.config/optigravity/secrets.env` (`0600` on POSIX), plus `AIza*` redaction filter. Zero keys stored in `.agents/mcp_config.json`.
  * Explicit `subagents.privacy_mode`: `tier1_only` (recommended for private code) vs `free_first` (auto-fallback Project A Free Tier → Project B Tier 1 on `429` RPD circuit-breaker).
  * Dual progress reporting: MCP `ctx.report_progress` / `ctx.info` + SQLite WAL `subagent_runs` table feeding the live `ogy tui` sidebar and `ogy stats`.
  * Opt-in isolated `git worktree` runner (`ogy_subagents/worktree.py`, disabled by default) returning a structured JSON diff contract for human review.

---

## 12. Configuration (`config.toml`)

Managed safely via `ogy config show|get|set|validate|edit` at `~/.config/optigravity/config.toml`:

```toml
[general]
language = "en"              # "en" (default) or "fr"
plan_tier = "pro"            # "free", "pro", "ultra", "enterprise"
# db_path = "~/.local/share/optigravity/ogy.db"

[hooks]
overwrite_verified_ok = true # Silent rewrite via agy 1.5.16 overwrite
file_max_bytes = 24576       # 24 KB for source code files
doc_max_bytes = 49152        # 48 KB for markdown/config docs
data_max_bytes = 8192        # 8 KB for logs/JSON/data files
loop_threshold = 3
statusline_debounce_s = 10.0

[pricing]
enterprise_cache = false

[budget]
# hard_cap_usd = 5.00

[tui]
enter_behavior = "submit"    # "submit" or "newline"

[subagents]
privacy_mode = "tier1_only"  # "tier1_only", "free_first", or "disabled"
fallback_to_tier1 = true
default_model = "gemini-3.5-flash-lite"
max_steps = 8
timeout_s = 45.0
worktree_enabled = false
```

---

## 13. Troubleshooting & Safety

* **Running tests**: `uv run pytest -q --tb=short` (57 unit & Textual Pilot tests).
* **SQLite concurrency**: Schema migrations are gated by `PRAGMA user_version`; read paths (`stats`, `waste`, TUI telemetry worker, statusline fallback) use `mode=ro` read-only connections.
* **Windows paths**: Never use `~` or single backslashes `\` inside `settings.json` or `hooks.json`; `ogy init` handles this automatically.

---

## 14. A/B Benchmarks Summary

Controlled A/B benchmarks (`agy 1.5.16`, `gemini-4-hydrogen-medium`, `--effort medium`) show that Optigravity's savings scale with repository size and noise while preserving 100 % test integrity:

| Benchmark | Input Tokens vs `agy` | Cost vs `agy` | Turns / Latency vs `agy` | Quality / Tests |
| :--- | :--- | :--- | :--- | :--- |
| **Noisy Repo** (209 KB lockfile, 122 KB log) | **-58.8 %** | **-51.8 %** | -20.0 % turns / -18.3 % latency | 100 % PASS (`CHARGE_OK`) |
| **`pallets/click`** (160 KB `core.py`) | **-10.3 %** *(no FB)* / **-6.3 %** *(FB)* | **-10.2 %** / **-5.9 %** | **-17.4 %** latency *(FB)* | **2,556 / 2,556 PASS** |
| **`yt-dlp/yt-dlp`** (1,500+ files, 200 KB modules) | **-19.6 %** *(no FB)* / **-29.7 %** *(FB)* | **-16.2 %** / **-21.8 %** | **-37.5 %** turns *(FB)* | **130 / 130 PASS** |

See [BENCHMARKS.md](BENCHMARKS.md) for the full 3-arm tables, methodology, and observed protocol notes.
