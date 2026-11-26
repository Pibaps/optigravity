"""Configuration loader for Optigravity (100% stdlib)."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OgyConfig:
    """Resolved runtime configuration for Optigravity."""

    config_path: Path
    db_path: Path
    agy_cli_dir: Path
    agy_config_dir: Path
    stash_dir: Path
    state_dir: Path
    overwrite_verified_ok: bool = True
    enterprise_cache_pricing: bool = False
    budget_hard_cap_usd: float | None = None
    default_plan_tier: str = "pro"
    language: str = "en"
    tui_enter_behavior: str = "submit"
    firebreak_file_max_bytes: int = 24_576
    firebreak_doc_max_bytes: int = 49_152
    firebreak_data_max_bytes: int = 8_192
    firebreak_loop_threshold: int = 3
    statusline_debounce_s: float = 10.0
    subagents_privacy_mode: str = "tier1_only"
    subagents_fallback_to_tier1: bool = True
    subagents_default_model: str = "gemini-3.5-flash-lite"
    subagents_max_steps: int = 8
    subagents_timeout_s: float = 45.0
    subagents_worktree_enabled: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def default_config_path() -> Path:
    """Return ~/.config/optigravity/config.toml (never relies on env vars)."""
    return Path.home() / ".config" / "optigravity" / "config.toml"


def default_db_path() -> Path:
    """Return ~/.local/share/optigravity/ogy.db, overridable via OGY_DB_PATH."""
    override = os.environ.get("OGY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "optigravity" / "ogy.db"


def load_config(config_path: Path | None = None) -> OgyConfig:
    """Load config.toml if present, falling back to safe defaults."""
    home = Path.home()
    cfg_path = config_path or default_config_path()
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            with cfg_path.open("rb") as fh:
                loaded = tomllib.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, tomllib.TOMLDecodeError):
            data = {}

    general = data.get("general", {}) if isinstance(data.get("general"), dict) else {}
    hooks_cfg = data.get("hooks", {}) if isinstance(data.get("hooks"), dict) else {}
    pricing_cfg = data.get("pricing", {}) if isinstance(data.get("pricing"), dict) else {}
    budget_cfg = data.get("budget", {}) if isinstance(data.get("budget"), dict) else {}
    tui_cfg = data.get("tui", {}) if isinstance(data.get("tui"), dict) else {}
    sub_cfg = data.get("subagents", {}) if isinstance(data.get("subagents"), dict) else {}

    env_db = os.environ.get("OGY_DB_PATH", "").strip()
    if env_db:
        db_path = Path(env_db).expanduser()
    elif isinstance(general.get("db_path"), str) and general["db_path"].strip():
        db_path = Path(general["db_path"]).expanduser()
    else:
        db_path = home / ".local" / "share" / "optigravity" / "ogy.db"

    hard_cap_raw = budget_cfg.get("hard_cap_usd")
    hard_cap: float | None = None
    if isinstance(hard_cap_raw, (int, float)) and hard_cap_raw > 0:
        hard_cap = float(hard_cap_raw)

    lang_raw = os.environ.get("OGY_LANGUAGE", "").strip() or str(general.get("language", "en")).strip()
    language = lang_raw.lower() if lang_raw.lower() in ("en", "fr") else "en"

    enter_raw = str(tui_cfg.get("enter_behavior", "submit")).strip().lower()
    tui_enter_behavior = enter_raw if enter_raw in ("submit", "newline") else "submit"

    priv_raw = str(sub_cfg.get("privacy_mode", "tier1_only")).strip().lower()
    subagents_privacy_mode = priv_raw if priv_raw in ("tier1_only", "free_first", "disabled") else "tier1_only"

    return OgyConfig(
        config_path=cfg_path,
        db_path=db_path,
        agy_cli_dir=home / ".gemini" / "antigravity-cli",
        agy_config_dir=home / ".gemini" / "config",
        stash_dir=home / ".cache" / "optigravity" / "stash",
        state_dir=Path(tempfile.gettempdir()) / "ogy-state",
        overwrite_verified_ok=bool(hooks_cfg.get("overwrite_verified_ok", True)),
        enterprise_cache_pricing=bool(pricing_cfg.get("enterprise_cache", False)),
        budget_hard_cap_usd=hard_cap,
        default_plan_tier=str(general.get("plan_tier", "pro")),
        language=language,
        tui_enter_behavior=tui_enter_behavior,
        firebreak_file_max_bytes=int(hooks_cfg.get("file_max_bytes", 24_576)),
        firebreak_doc_max_bytes=int(hooks_cfg.get("doc_max_bytes", 49_152)),
        firebreak_data_max_bytes=int(hooks_cfg.get("data_max_bytes", 8_192)),
        firebreak_loop_threshold=int(hooks_cfg.get("loop_threshold", 3)),
        statusline_debounce_s=float(hooks_cfg.get("statusline_debounce_s", 10.0)),
        subagents_privacy_mode=subagents_privacy_mode,
        subagents_fallback_to_tier1=bool(sub_cfg.get("fallback_to_tier1", True)),
        subagents_default_model=str(sub_cfg.get("default_model", "gemini-3.5-flash-lite")),
        subagents_max_steps=max(1, int(sub_cfg.get("max_steps", 8))),
        subagents_timeout_s=max(5.0, float(sub_cfg.get("timeout_s", 45.0))),
        subagents_worktree_enabled=bool(sub_cfg.get("worktree_enabled", False)),
        raw=data,
    )
