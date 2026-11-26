"""Optigravity hybrid MCP subagents package (`ogy-subagents`).

Confines optional `[subagents]` dependencies (`mcp`, `google-genai`, `keyring`)
outside the 100% stdlib V1 hot path (`ogy_core`, `ogy_hooks`, `ogy_statusline`).
"""

from __future__ import annotations

__all__ = [
    "gemini_client",
    "router",
    "secrets",
    "server",
    "tools_readonly",
    "worktree",
]
