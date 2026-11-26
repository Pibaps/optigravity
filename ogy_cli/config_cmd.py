"""`ogy config [show|get|set|validate|edit]` implementation (100% stdlib)."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from ogy_core.config import default_config_path, load_config

# Schema of supported dotted keys: key -> (type_name, default, validator_choices_or_none, env_var_or_none)
CONFIG_SCHEMA: dict[str, tuple[str, Any, tuple[Any, ...] | None, str | None]] = {
    "general.language": ("str", "en", ("en", "fr"), "OGY_LANGUAGE"),
    "general.plan_tier": ("str", "pro", ("free", "pro", "ultra", "enterprise"), None),
    "general.db_path": ("str", "", None, "OGY_DB_PATH"),
    "hooks.overwrite_verified_ok": ("bool", True, None, None),
    "hooks.file_max_bytes": ("int", 24_576, None, None),
    "hooks.doc_max_bytes": ("int", 49_152, None, None),
    "hooks.data_max_bytes": ("int", 8_192, None, None),
    "hooks.loop_threshold": ("int", 3, None, None),
    "hooks.statusline_debounce_s": ("float", 10.0, None, None),
    "pricing.enterprise_cache": ("bool", False, None, None),
    "budget.hard_cap_usd": ("float_opt", None, None, None),
    "tui.enter_behavior": ("str", "submit", ("submit", "newline"), None),
    "subagents.privacy_mode": ("str", "tier1_only", ("tier1_only", "free_first", "disabled"), None),
    "subagents.fallback_to_tier1": ("bool", True, None, None),
    "subagents.default_model": ("str", "gemini-3.5-flash-lite", None, None),
    "subagents.max_steps": ("int", 8, None, None),
    "subagents.timeout_s": ("float", 45.0, None, None),
    "subagents.worktree_enabled": ("bool", False, None, None),
}


def render_default_config_toml(
    *,
    language: str = "en",
    overwrite_verified_ok: bool = True,
    privacy_mode: str = "tier1_only",
) -> str:
    """Return a clean, commented config.toml template."""
    return (
        "# Optigravity configuration (~/.config/optigravity/config.toml)\n"
        "# Manage safely with: ogy config show | get | set | validate | edit\n\n"
        "[general]\n"
        f'language = "{language}"  # "en" (default) or "fr"\n'
        'plan_tier = "pro"  # "free", "pro", "ultra", "enterprise"\n\n'
        "[hooks]\n"
        f"overwrite_verified_ok = {'true' if overwrite_verified_ok else 'false'}  # Silent rewrite via agy 1.5.16 overwrite\n"
        "file_max_bytes = 24576  # 24 KB limit for source code files without StartLine/EndLine\n"
        "doc_max_bytes = 49152  # 48 KB limit for markdown/config docs\n"
        "data_max_bytes = 8192  # 8 KB limit for logs/JSON/data files\n"
        "loop_threshold = 3  # Deny after N identical failing tool calls\n"
        "statusline_debounce_s = 10.0  # Minimum seconds between statusline DB writes\n\n"
        "[pricing]\n"
        "enterprise_cache = false  # Set true for Vertex AI / Enterprise cache write pricing\n\n"
        "[budget]\n"
        "# hard_cap_usd = 5.0  # Optional hard cap per session\n\n"
        "[tui]\n"
        'enter_behavior = "submit"  # "submit" or "newline"\n\n'
        "[subagents]\n"
        f'privacy_mode = "{privacy_mode}"  # "tier1_only" (recommended for private code), "free_first", or "disabled"\n'
        "fallback_to_tier1 = true  # Auto-fallback Free Tier -> Tier 1 on 429 RPD\n"
        'default_model = "gemini-3.5-flash-lite"  # Default Gemini API model for MCP subagents\n'
        "max_steps = 8  # Maximum tool-calling turns for Level 2 read-only subagents\n"
        "timeout_s = 45.0  # Timeout per Level 2 subagent run\n"
        "worktree_enabled = false  # Opt-in isolated git worktree edits (disabled by default)\n"
    )


def _parse_value(key: str, raw_val: str) -> Any:
    if key not in CONFIG_SCHEMA:
        raise ValueError(f"Unknown config key: '{key}'. Valid keys: {', '.join(sorted(CONFIG_SCHEMA))}")
    typ, _default, choices, _env = CONFIG_SCHEMA[key]
    s = raw_val.strip()
    if typ == "bool":
        if s.lower() in ("true", "1", "yes", "on"):
            val: Any = True
        elif s.lower() in ("false", "0", "no", "off"):
            val = False
        else:
            raise ValueError(f"Key '{key}' expects a boolean (true/false), got '{raw_val}'")
    elif typ == "int":
        try:
            val = int(s)
        except ValueError as exc:
            raise ValueError(f"Key '{key}' expects an integer, got '{raw_val}'") from exc
        if val <= 0:
            raise ValueError(f"Key '{key}' must be > 0, got {val}")
    elif typ == "float":
        try:
            val = float(s)
        except ValueError as exc:
            raise ValueError(f"Key '{key}' expects a float, got '{raw_val}'") from exc
        if val < 0:
            raise ValueError(f"Key '{key}' must be >= 0, got {val}")
    elif typ == "float_opt":
        if s.lower() in ("none", "null", ""):
            val = None
        else:
            try:
                val = float(s)
            except ValueError as exc:
                raise ValueError(f"Key '{key}' expects a positive float or 'none', got '{raw_val}'") from exc
            if val <= 0:
                raise ValueError(f"Key '{key}' must be > 0 or 'none', got {val}")
    else:
        val = s

    if choices is not None and val not in choices:
        raise ValueError(f"Key '{key}' must be one of {choices}, got '{val}'")
    return val


def _format_toml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = ["# Optigravity configuration (managed by `ogy config`)\n"]
    # Top-level scalars first if any
    for k, v in data.items():
        if not isinstance(v, dict) and v is not None:
            lines.append(f"{k} = {_format_toml_value(v)}\n")
    for section, tbl in data.items():
        if not isinstance(tbl, dict):
            continue
        items = [(k, v) for k, v in tbl.items() if v is not None]
        if not items:
            continue
        lines.append(f"\n[{section}]\n")
        for k, v in items:
            lines.append(f"{k} = {_format_toml_value(v)}\n")
    return "".join(lines)


def resolve_config_entries(config_path: Path | None = None) -> list[dict[str, Any]]:
    """Return list of {key, value, source} with provenance ('default' | 'config.toml' | 'env')."""
    cfg_path = config_path or default_config_path()
    cfg = load_config(cfg_path)
    raw = cfg.raw if isinstance(cfg.raw, dict) else {}
    entries: list[dict[str, Any]] = []

    for key, (_typ, default, _choices, env_var) in CONFIG_SCHEMA.items():
        section, field = key.split(".", 1)
        sec_dict = raw.get(section) if isinstance(raw.get(section), dict) else {}

        source = "default"
        value = default
        if field in sec_dict:
            value = sec_dict[field]
            source = "config.toml"
        if env_var and os.environ.get(env_var, "").strip():
            value = os.environ[env_var].strip()
            source = f"env:{env_var}"
        if key == "general.db_path" and not value:
            value = str(cfg.db_path)

        entries.append({"key": key, "value": value, "source": source})
    return entries


def validate_config_file(config_path: Path | None = None) -> list[str]:
    """Validate config.toml syntax, keys, types, and ranges. Returns list of error messages."""
    cfg_path = config_path or default_config_path()
    if not cfg_path.is_file():
        return []
    try:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"TOML parse error in {cfg_path}: {exc}"]

    if not isinstance(data, dict):
        return ["Root TOML document must be a table."]

    errors: list[str] = []
    for section, tbl in data.items():
        if not isinstance(tbl, dict):
            errors.append(f"Unexpected top-level scalar '{section}'; expected a [section] table.")
            continue
        for field, val in tbl.items():
            key = f"{section}.{field}"
            if key not in CONFIG_SCHEMA:
                errors.append(f"Unknown key '{key}'.")
                continue
            typ, _default, choices, _env = CONFIG_SCHEMA[key]
            if typ == "bool" and not isinstance(val, bool):
                errors.append(f"Key '{key}' must be bool, got {type(val).__name__}.")
            elif typ == "int" and (isinstance(val, bool) or not isinstance(val, int) or val <= 0):
                errors.append(f"Key '{key}' must be positive int, got {val!r}.")
            elif typ == "float" and (isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0):
                errors.append(f"Key '{key}' must be non-negative float, got {val!r}.")
            elif typ == "float_opt" and (isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0):
                errors.append(f"Key '{key}' must be positive float, got {val!r}.")
            elif typ == "str" and not isinstance(val, str):
                errors.append(f"Key '{key}' must be str, got {type(val).__name__}.")
            if choices is not None and val not in choices:
                errors.append(f"Key '{key}' must be one of {choices}, got {val!r}.")
    return errors


def set_config_value(key: str, raw_val: str, *, config_path: Path | None = None) -> Any:
    """Validate and persist a dotted key into config.toml."""
    parsed = _parse_value(key, raw_val)
    cfg_path = config_path or default_config_path()
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            loaded = tomllib.load(fh)
        if isinstance(loaded, dict):
            data = loaded

    section, field = key.split(".", 1)
    sec_tbl = data.get(section)
    if not isinstance(sec_tbl, dict):
        sec_tbl = {}
        data[section] = sec_tbl
    if parsed is None:
        sec_tbl.pop(field, None)
    else:
        sec_tbl[field] = parsed

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".toml.tmp")
    tmp.write_text(_dump_toml(data), encoding="utf-8")
    tmp.replace(cfg_path)
    return parsed


def cmd_config(
    action: str = "show",
    *,
    key: str | None = None,
    value: str | None = None,
    as_json: bool = False,
    config_path: Path | None = None,
) -> int:
    """CLI handler for `ogy config [show|get|set|validate|edit]`."""
    cfg_path = config_path or default_config_path()

    if action == "show":
        entries = resolve_config_entries(cfg_path)
        if as_json:
            print(json.dumps({"config_path": str(cfg_path), "entries": entries}, indent=2))
            return 0
        print(f"Config file: {cfg_path} ({'exists' if cfg_path.is_file() else 'not created yet'})")
        w_key = max(len(e["key"]) for e in entries)
        w_val = max(len(str(e["value"])) for e in entries)
        print(f"{'KEY'.ljust(w_key)}  {'VALUE'.ljust(w_val)}  SOURCE")
        print(f"{'-' * w_key}  {'-' * w_val}  ------")
        for e in entries:
            print(f"{e['key'].ljust(w_key)}  {str(e['value']).ljust(w_val)}  {e['source']}")
        return 0

    if action == "get":
        if not key:
            print("Usage: ogy config get <section.key>")
            return 2
        entries = {e["key"]: e for e in resolve_config_entries(cfg_path)}
        if key not in entries:
            print(f"Unknown key: {key}")
            return 1
        e = entries[key]
        if as_json:
            print(json.dumps(e, indent=2))
        else:
            print(f"{e['value']}  ({e['source']})")
        return 0

    if action == "set":
        if not key or value is None:
            print("Usage: ogy config set <section.key> <value>")
            return 2
        try:
            parsed = set_config_value(key, value, config_path=cfg_path)
        except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
            print(f"[ogy config] Error: {exc}")
            return 1
        print(f"Updated {key} = {parsed!r} in {cfg_path}")
        return 0

    if action == "validate":
        errors = validate_config_file(cfg_path)
        if errors:
            for err in errors:
                print(f"[INVALID] {err}")
            return 1
        print(f"[OK] {cfg_path} is valid.")
        return 0

    if action == "edit":
        if not cfg_path.is_file():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(render_default_config_toml(), encoding="utf-8")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
        try:
            subprocess.run([editor, str(cfg_path)], check=False)
        except OSError as exc:
            print(f"[ogy config] Failed to launch editor '{editor}': {exc}")
            return 1
        errors = validate_config_file(cfg_path)
        if errors:
            for err in errors:
                print(f"[INVALID] {err}")
            return 1
        print(f"[OK] Saved and validated {cfg_path}.")
        return 0

    print(f"Unknown config action: {action}")
    return 2
