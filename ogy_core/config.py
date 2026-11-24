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
    firebreak_file_max_bytes: int = 16_384
    firebreak_loop_threshold: int = 3
    statusline_debounce_s: float = 10.0
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

    return OgyConfig(
        config_path=cfg_path,
        db_path=db_path,
        agy_cli_dir=home / ".gemini" / "antigravity-cli",
        agy_config_dir=home / ".gemini" / "config",
        stash_dir=home / ".cache" / "optigravity" / "stash",
        state_dir=Path(tempfile.gettempdir()) / "ogy-state",
        overwrite_verified_ok=bool(hooks_cfg.get("overwrite_verified_ok", False)),
        enterprise_cache_pricing=bool(pricing_cfg.get("enterprise_cache", False)),
        budget_hard_cap_usd=hard_cap,
        default_plan_tier=str(general.get("plan_tier", "pro")),
        firebreak_file_max_bytes=int(hooks_cfg.get("file_max_bytes", 16_384)),
        firebreak_loop_threshold=int(hooks_cfg.get("loop_threshold", 3)),
        statusline_debounce_s=float(hooks_cfg.get("statusline_debounce_s", 10.0)),
        raw=data,
    )
