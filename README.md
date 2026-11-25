# Optigravity (`ogy`) — User Manual

**Optigravity (`ogy`)** is a token-efficiency, context-health, routing, and telemetry companion for **Google Antigravity CLI (`agy`)**.  
It reduces billable token usage while preserving the native `agy` TUI, OAuth quotas, and interactive workflows.

---

## Table of Contents

1. [Requirements & Installation](#1-requirements--installation)
2. [Quick Start](#2-quick-start)
3. [CLI Reference (`ogy`)](#3-cli-reference-ogy)
4. [Interactive ACP TUI (`ogy tui`)](#4-interactive-acp-tui-ogy-tui)
5. [Task Routing (Stage 1 & Stage 2)](#5-task-routing-stage-1--stage-2)
6. [Context Health & Pricing Policy](#6-context-health--pricing-policy)
7. [Real-Time Statusline HUD](#7-real-time-statusline-hud)
8. [Fail-Open Hooks & Firebreaks](#8-fail-open-hooks--firebreaks)
9. [Shell Compression (`ogy wrap` & `ogy recall`)](#9-shell-compression-ogy-wrap--ogy-recall)
10. [Minimal MCP Server (`ogy-mcp`)](#10-minimal-mcp-server-ogy-mcp)
11. [Configuration (`config.toml`)](#11-configuration-configtoml)
12. [Troubleshooting & Safety](#12-troubleshooting--safety)
13. [A/B Benchmarks Summary](#13-ab-benchmarks-summary)

---

## 1. Requirements & Installation

* **Python**: 3.12+ (Windows & Linux)
* **Google Antigravity CLI**: `agy` installed and available on `PATH`
* **Package installer**: [`uv`](https://docs.astral.sh/uv/) (recommended), `pipx`, or standard `pip`

### Option A — Install with `uv` (recommended)

```bash
# Clone the repository and sync dependencies
uv sync --all-extras

# Or install globally as a uv tool (exposes ogy, ogy-hook, ogy-mcp on PATH)
uv tool install .
```

### Option B — Install with `pipx` or `pip` (no `uv` required)

```bash
# Global tool install with pipx
pipx install .

# Or editable install in a standard venv
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

This installs three entry points:
* `ogy` — Main user CLI
* `ogy-hook` — Fail-open hook & statusline dispatcher (100% Python stdlib)
* `ogy-mcp` — Minimal stdio MCP server (`mcp>=2,<3`)

---

## 2. Quick Start

```bash
# 1. Verify your local environment and probe your agy build
ogy doctor --probe-agy

# 2. Preview what ogy init will configure
ogy init --dry-run

# 3. Install statusLine, hooks, rules, skills, and lazy MCP config (creates *.ogy.bak backups)
ogy init

# 4. Launch agy through the Optigravity router (native TUI)
ogy run "Fix typo in README"

# 5. Or launch the interactive ACP TUI with live thoughtTokens & session rotation
ogy tui
```

---

## 3. CLI Reference (`ogy`)

### `ogy run [OPTIONS] [PROMPT]`
Routes your prompt through the 2-stage router, sets `AGY_CLI_DISABLE_AUTO_UPDATE=true`, `OGY_TASK_ID`, and `OGY_SESSION_META`, and launches `agy` with direct TTY inheritance (no PTY proxy).

* `--model <slug>` — Override routed model (e.g., `gemini-4-hydrogen`, `gemini-4-neon`, `claude-opus-5.5`)
* `--effort <level>` — Override effort (`low`, `medium`, `high`, `xhigh`)
* `--mode <mode>` — Override execution mode (`plan`, `accept-edits`, `auto`)
* `--sandbox` — Pass `--sandbox` to `agy`
* `--dry-run` — Print the resolved `agy` command and routing metadata without launching

### `ogy tui [OPTIONS] [PROMPT]` (alias: `ogy chat`)
Launches the interactive Textual TUI backed by `agy acp` (ACP v1 JSON-RPC 2.0 over stdio), with live `thoughtTokens`, dynamic `model`/`effort`/`mode` switching, steering, and one-key session rotation. See [Interactive ACP TUI (`ogy tui`)](#4-interactive-acp-tui-ogy-tui) for the full guide.

* `--cwd <path>` — Workspace directory (default: `.`)
* `--sandbox` — Pass `--sandbox` to `agy acp`
* `--add-dir <path>` — Additional directory (`--add-dir`, repeatable)
* `--agent <name>` — Agent profile (`--agent`)
* `--log-file <path>` — ACP debug log file (`--log-file`)
* `--auth-mode <mode>` — Auth mode (`oauth-personal` default, or `api-key`)
* `--model <slug>` — Pin initial model (skips 1st-prompt Stage 1 auto-routing)
* `--effort <level>` — Pin initial effort (`low`, `medium`, `high`, `xhigh`, `max`)
* `--lang <fr|en>` — Interface language (`fr` default, or `en`; switchable live via `/lang`)

### `ogy doctor [--probe-agy]`
Checks local paths, configuration, and SQLite WAL database health. With `--probe-agy`, runs Phase 0 probes against your local `agy` binary (`--version`, `--help`, `models`, `mcp list`, read-only DB inspection, and `python -S` cold-start benchmark).

### `ogy init [--dry-run] [--enable-firebreaks]`
Reversibly installs:
* `statusLine` in `~/.gemini/antigravity-cli/settings.json`
* Hooks in `~/.gemini/config/hooks.json` (`ogy-firebreaks` is disabled by default unless `--enable-firebreaks` is passed)
* Lazy MCP entry (`eager: false`) in `~/.gemini/config/mcp_config.json`
* Always-on hygiene rule in `~/.gemini/config/rules/ogy-hygiene.md`
* Manual budget/handoff skill in `~/.gemini/config/skills/ogy-budget/SKILL.md`

Existing JSON files are backed up with a `.ogy.bak` suffix.

### `ogy stats [--session <id>] [--json]`
Displays per-session KPIs from `v_session_kpis` (turns, cache hit %, peak context tokens, estimated cost).

### `ogy waste [--session <id>] [--json]`
Runs the 8 automated waste-detection queries:
1. **Low cache hit** (`< 70%`, distinguishing server wipes `< 180s` vs TTL expiries `>= 180s`)
2. **Retry loops** (`>= 3` attempts with `>= 2` errors on the same `args_hash`)
3. **Obese tool outputs** (`> 4,000` estimated tokens or `>= 32,768` bytes)
4. **Reasoning over-effort** (`high`/`xhigh`/`max` on `trivial_edit`/`lookup_qa`, or `thinking > 5x output`)
5. **Context rot risk** (peak context `>= 120K`, `>= 25` turns, or rollbacks)
6. **Duplicate file reads** (`view_file`/`read_file` `>= 2x` on the same path without compaction)
7. **MCP schema bloat** (`tools_schema_tokens > 15,000`)
8. **Subagent drift** (`subagent_cost > parent_cost` or failed subagents)

### `ogy router-eval [--category <cat>] [--min-samples <n>] [--json]`
Runs an offline shadow evaluation of Stage 2 bandit arms vs Stage 1 on historical `routing_decisions` in `ogy.db`.

### `ogy audit mcp [--json]`
Inspects `mcp_config.json` files for `eager: true` servers and UI-only tools (`_meta.ui.visibility: ["app"]`) leaking into CLI context, and outputs a minimal `enabledTools` profile.

### `ogy wrap -- <command>`
Runs a shell command with deterministic output compression, *net-win gate* (returns raw output if compression doesn't save >= 10%), *tee-on-failure* stashing, and exact exit-code preservation.

### `ogy recall <hash_prefix> [--lines A:B]`
Retrieves raw stashed output saved by `ogy wrap` from `~/.cache/optigravity/stash/<sha256>.log`, optionally sliced to 1-based inclusive lines `A:B`.

---

## 4. Interactive ACP TUI (`ogy tui`)

`ogy tui` (or `ogy chat`) connects directly to `agy acp` over JSON-RPC 2.0 (`stdio`), reusing `ogy_core` in memory while keeping `ogy-hook` / `ogy statusline` 100 % stdlib.

### Key Capabilities
* **Pro Dark Graphite UI & Clean Stream**: Sober graphite theme without heavy chat boxes — user prompts, tool calls (`●` / `✓` / `✗`), collapsible reasoning (`agent_thought_chunk`), and notices use minimal left-accent strips.
* **Live Telemetry Sidebar with Gauges**: Smooth progress gauges for **Contexte**, **Cache prompt**, and **each quota bucket on its own line** (`Gemini (5h)`, `Gemini (7j)`, `Tiers / Claude (5h)` for `3p-5h`, `Tiers / Claude (7j)` for `3p-weekly`), alongside `Raisonnement` (`thoughtTokens`), `Entrée / Sortie`, `Coût estimé`, `Inactivité`, and `Échecs outils`.
* **Slash Autocomplete & Full `Ctrl+P` Command Palette**: Typing `/` opens a live completion popup (`Tab` / `↑` / `↓` / click) that completes both commands and arguments (`/model`, `/effort`, `/mode`, `/resume`). Pressing `Ctrl+P` exposes all Optigravity commands, models, efforts, modes, rotation, glossary, and active proposals directly.
* **Built-in Glossary (`F1` / `/glossary`)**: Explains every Optigravity term (`SAIN` / `keep_warm`, `ATTENTION` / `frugal_btw`, `SATURÉ` / `clear_handoff`, `3p` = Third-Party/Claude quotas, `Switch Tax`, `Idle Gap` 180s TTL).
* **Concurrent Steering & Safe Cancel**: Submitting a new prompt while a turn is active steers the agent in flight; pressing `Ctrl+C` sends `session/cancel` without killing the `agy acp` process (`Ctrl+Q` quits cleanly and closes `agy acp`).
* **Interactive Permissions Modal**: Handles `session/request_permission` (`allow_once`, `allow_always`, `reject_once`) with Optigravity firebreak reminders.

### Slash Commands & Shortcuts
* `/model [slug]` — Switch model via `session/set_config_option` (or list current & available models when omitted)
* `/effort [low|medium|high|xhigh|max]` — Adjust `thinking_level` / `thought_level` dynamically
* `/mode [default|accept-edits|auto|plan]` — Change execution mode (`yolo` is rejected)
* `/btw <question>` — Ask a concise side question without modifying files or derailing the active task
* `/lang [fr|en]` — Switch the TUI language live between French (`fr`) and English (`en`)
* `/rotate` (or `/clear`, `Ctrl+R`) — Clean multi-session rotation: writes `.ogy/handoff.md`, closes the saturated session, opens a fresh session, and injects the handoff
* `/handoff` — Write `.ogy/handoff.md` (`<= 30` lines) without closing the session
* `/new` (`Ctrl+N`), `/sessions`, `/resume <session_id>` — Multi-session management via `session/resume`
* `/glossary` (`F1`), `/help` — Open the interactive glossary modal or print slash command help
* `Ctrl+P` — Open the full Optigravity Command Palette
* `Ctrl+B` — Toggle the telemetry sidebar

### Assisted Orchestration & Cache Preservation
* **Switch Tax & `> 60K` Delegation Rule**: After `>= 2` consecutive tool failures on a low/medium profile, `ogy tui` calculates the prefix cache invalidation cost (**Switch Tax**). At `<= 60K` context tokens, it proposes an upgrade (`[U]`); above `60K` tokens, it proposes delegating the blocking sub-problem to a strong subagent (`[D]`) so the parent prefix cache stays intact.
* **Red-Zone Rotation**: When context reaches `>= 100K` tokens, `exceeds_200k`, or `idle_gap > 180s` above `60K`, a banner proposes a one-key clean rotation (`[R]`).
* **OAuth Personal Scope**: Under `oauth-personal`, `ogy tui` operates on Gemini 4 models by default with user-confirmed proposals. If you request Claude 5.5, `ogy tui` suggests switching cleanly to `ogy run --model claude-...` to preserve 100 % of your Pro/Ultra Claude quota.

---

## 5. Task Routing (Stage 1 & Stage 2)

Optigravity follows a **stickiness rule**: model and effort are chosen at launch (or subagent boundary) and never changed mid-session to avoid invalidating the prompt prefix cache.

* **Stage 1 (Deterministic Heuristic, < 5 ms)**: Extracts 8 features (prompt length, `@`-mentions, repo size, test runner presence, multi-file scope, stacktraces, architecture/security keywords, quota pressure) and maps to:
  * `trivial_edit` → `gemini-4-hydrogen` / `low` / `accept-edits`
  * `lookup_qa` → `gemini-4-hydrogen` / `low` / `plan`
  * `routine_code` → `gemini-4-hydrogen` / `medium` / `auto`
  * `bugfix_local` → `gemini-4-neon` / `low` / `auto`
  * `bugfix_complex` → `gemini-4-neon` / `high` / `auto`
  * `refactor_multifile` → `gemini-4-neon` / `medium` / `auto`
  * `architecture_security` → `gemini-4-neon` / `high` / `plan`
  * Under low quota (`remaining_fraction < 0.15`), effort is automatically downgraded by one step (except `architecture_security`).
* **Stage 2 (SQLite UCB1 Contextual Bandit)**: Activates after `>= 15` qualified tasks in a category, using exponentially weighted rewards ($\eta = 0.92$) and excluding any arm whose success rate drops below `80%`.

---

## 6. Context Health & Pricing Policy

* **Green Zone** (`< 60K` tokens, `< 15` turns, cache hit `>= 60%`): Keep warm (`keep_warm`).
* **Amber Zone** (`60K–100K` tokens or `15–25` turns): Stay frugal, use `/btw` for side questions (`frugal_btw`).
* **Red Zone** (`>= 100K` tokens, `exceeds_200k`, `idle_gap > 180s` above `60K`, or `>= 2` consecutive failures):
  * Default: write `.ogy/handoff.md` and run `/clear` (`clear_handoff`).
  * Exception: if remaining turns `>= K*` (`5` for Gemini 4, `9` for Claude) and no consecutive errors, `/compact` (`compact`).
  * After `>= 2` consecutive failures: never compact; `/rewind` or `/clear` (`rewind`).
* **Pricing Catalogue**: Supports Gemini 4 (`Hydrogen`, `Neon`, `Carbon`/`Argon` including the `2026-12-01` price switch) and Claude 5.5 (`Opus`, `Sonnet`, `Haiku` with its `100K` 5x cliff).

---

## 7. Real-Time Statusline HUD

Configured in `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "ogy-hook statusline",
    "enabled": true,
    "padding": 0,
    "stack_with_default": true
  }
}
```

Displays a 1-line HUD adapted to terminal width (respects `NO_COLOR`):

```text
ogy | cache 92% | ctx 64K (13%) | $0.24 | 5h:88% | WARN
```

Snapshots are persisted to SQLite with a 10-second debounce.

---

## 8. Fail-Open Hooks & Firebreaks

`ogy-hook <event>` imports **only the Python standard library** and wraps `main()` in `try/except BaseException` so a hook failure never blocks `agy` (always returns valid JSON and exits `0`).

* `session-start` — Registers the session and injects `.ogy/handoff.md` once via `injectSteps`, then archives it.
* `pre-tool` — Returns `{"decision": "allow"}` nominally (never `{}` or `""`), or `deny` / `overwrite` for:
  1. **Anti-loop**: blocks repeated failing tool calls (`>= 3` errors on the same `args_hash`).
  2. **Obese reads**: blocks lockfiles and files `> 16 KB` read without `StartLine`/`EndLine`.
  3. **Shell compression**: wraps verbose commands (`pytest`, `cargo test`, `git log/diff/status`, etc.) into `ogy wrap -- <cmd>`, while strictly excluding mutating commands (`git commit`, `git push`, `git rebase`, `rm`, etc.).
* `post-tool` — Records `tool_calls` and errors; returns `{}`.
* `pre-invocation` — Silent `{}` nominally and whenever `invocationNum > 1` (preserves prompt cache). Only terminates if `budget.hard_cap_usd` is exceeded.
* `post-invocation` — Incrementally ingests `transcript_full.jsonl` from `transcript_byte_offset`.
* `stop` — Finalizes ingestion, computes `routing_decisions.actual_reward`, and runs `PRAGMA wal_checkpoint(PASSIVE)` + `PRAGMA incremental_vacuum(200)`.

---

## 9. Shell Compression (`ogy wrap` & `ogy recall`)

`ogy wrap -- pytest -v` filters ANSI noise and passing chatter while keeping errors, tracebacks, and summaries. On any reduction or non-zero exit code, the full raw output is saved to `~/.cache/optigravity/stash/<sha256>.log` and a short recall hash is printed:

```text
[ogy wrap] exit=1 raw=14280B out=612B recall=a3f9c012e4b1
```

Retrieve the raw lines anytime with:

```bash
ogy recall a3f9c012e4b1 --lines 1:80
```

---

## 10. Minimal MCP Server (`ogy-mcp`)

Registered with `eager: false` in `mcp_config.json` and exposes 3 concise tools:
* `ogy_session_budget(session_id?)` — Active context tokens, warm cache hit %, idle gap, top waste, and recommended action.
* `ogy_recall(hash_prefix, start_line=1, end_line=100)` — Bounded read of stashed `ogy wrap` output.
* `ogy_write_handoff(goal, modified_files, checks, negative_constraints, next_step, workspace_path=".")` — Writes a normalized `<= 30`-line `.ogy/handoff.md`.

---

## 11. Configuration (`config.toml`)

Optional configuration file at `~/.config/optigravity/config.toml`:

```toml
[general]
plan_tier = "pro"
# db_path = "~/.local/share/optigravity/ogy.db"

[pricing]
enterprise_cache = false

[budget]
# hard_cap_usd = 5.00

[hooks]
overwrite_verified_ok = false
file_max_bytes = 16384
loop_threshold = 3
statusline_debounce_s = 10.0
```

You can also override the SQLite database location with the `OGY_DB_PATH` environment variable.

---

## 12. Troubleshooting & Safety

* **Running tests**: `pytest -v` (or `uv run pytest -v`)
* **Windows paths**: Never use `~` or single backslashes `\` inside `settings.json` or `hooks.json`; `ogy init` handles this automatically.
* **Firebreaks disabled by default**: `ogy init` installs `ogy-firebreaks` with `"enabled": false` until you have verified `ogy doctor --probe-agy` and opt in with `ogy init --enable-firebreaks`.

---

## 13. A/B Benchmarks Summary

Controlled A/B benchmarks (`agy 1.5.16`, `gemini-4-hydrogen-medium`, `--effort medium`) show that Optigravity's savings scale with repository size and noise while preserving 100 % test integrity:

| Benchmark | Input Tokens vs `agy` | Cost vs `agy` | Turns / Latency vs `agy` | Quality / Tests |
| --- | --- | --- | --- | --- |
| **Noisy Repo** (209 KB lockfile, 122 KB log) | **-58.8 %** | **-51.8 %** | -20.0 % turns / -18.3 % latency | 100 % PASS (`CHARGE_OK`) |
| **`pallets/click`** (160 KB `core.py`) | **-10.3 %** *(no FB)* / **-6.3 %** *(FB)* | **-10.2 %** / **-5.9 %** | **-17.4 %** latency *(FB)* | **2,556 / 2,556 PASS** |
| **`yt-dlp/yt-dlp`** (1,500+ files, 200 KB modules) | **-19.6 %** *(no FB)* / **-29.7 %** *(FB)* | **-16.2 %** / **-21.8 %** | **-37.5 %** turns *(FB)* | **130 / 130 PASS** |

See [BENCHMARKS.md](BENCHMARKS.md) for the full 3-arm tables, methodology, and observed protocol notes.
