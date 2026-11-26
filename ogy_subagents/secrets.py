"""Secure API key resolution cascade, redaction filter, and permissions audit.

Resolution order per tier (`free` = Project A Free Tier, `tier1` = Project B Paid/Credits):
1. Environment variables: `OGY_GEMINI_API_KEY_FREE` / `OGY_GEMINI_API_KEY_TIER1`, fallback `GEMINI_API_KEY`.
2. OS credential store via `keyring` (Windows Credential Manager / macOS Keychain / SecretService),
   verified non-empty to tolerate SSH / headless environments safely.
3. `~/.config/optigravity/secrets.env` (strict POSIX `0600` required; adapted check on Windows).
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
REDACTED_PLACEHOLDER = "[REDACTED_GEMINI_KEY]"

KEYRING_SERVICE = "optigravity"
TIER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "free": ("OGY_GEMINI_API_KEY_FREE", "GEMINI_API_KEY"),
    "tier1": ("OGY_GEMINI_API_KEY_TIER1", "GEMINI_API_KEY"),
}
TIER_KEYRING_NAMES: dict[str, tuple[str, ...]] = {
    "free": ("gemini_api_key_free", "gemini_api_key"),
    "tier1": ("gemini_api_key_tier1", "gemini_api_key"),
}


@dataclass(frozen=True, slots=True)
class SecretResolution:
    """Result of resolving an API key for a tier without ever logging the raw secret."""

    tier: str
    key: str | None
    source: str
    warning: str | None = None


def redact_secrets(text: str) -> str:
    """Replace any Gemini API key (`AIza...`) in text with a safe placeholder."""
    if not text:
        return text
    return API_KEY_RE.sub(REDACTED_PLACEHOLDER, text)


class RedactingFilter(logging.Filter):
    """Logging filter that strips any `AIza*` API key from log records and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (redact_secrets(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def default_secrets_env_path() -> Path:
    """Return `~/.config/optigravity/secrets.env`."""
    return Path.home() / ".config" / "optigravity" / "secrets.env"


def check_secrets_file_permissions(path: Path | None = None) -> tuple[str, str]:
    """Audit permissions on `secrets.env` with POSIX 0600 enforcement and Windows compatibility.

    Returns `(status, detail)` where status is `'ok'`, `'missing'`, `'warn'`, or `'fail'`.
    """
    target = path or default_secrets_env_path()
    if not target.is_file():
        return "missing", "not present"
    if os.name == "nt":
        # Windows uses NTFS ACLs rather than POSIX mode bits; avoid false failure.
        return "ok", f"present on Windows ({target.name})"
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as exc:
        return "fail", f"stat failed: {exc}"
    if mode & 0o077:
        return "fail", f"mode {oct(mode)} too permissive (expected 0o600)"
    return "ok", f"mode {oct(mode)}"


def _parse_secrets_env_file(path: Path) -> tuple[dict[str, str], str | None]:
    """Read KEY=VALUE pairs from `secrets.env`, refusing insecure POSIX permissions."""
    if not path.is_file():
        return {}, None
    if os.name != "nt":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            return {}, f"Cannot stat {path}: {exc}"
        if mode & 0o077:
            return {}, (
                f"Refusing to read {path}: permissions {oct(mode)} are too permissive "
                "(must be 0o600; run `chmod 600` or `ogy doctor --fix`)."
            )
    entries: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v:
                entries[k] = v
    except OSError as exc:
        return {}, f"Cannot read {path}: {exc}"
    return entries, None


def _get_from_keyring(names: tuple[str, ...], keyring_module: Any | None = None) -> tuple[str | None, str]:
    """Query OS keyring safely, returning `(value, username)` only when non-empty."""
    kr = keyring_module
    if kr is None:
        try:
            import keyring as _kr  # type: ignore[import-not-found]

            kr = _kr
        except Exception:
            return None, ""
    for uname in names:
        try:
            val = kr.get_password(KEYRING_SERVICE, uname)
        except Exception:
            # Headless / SSH / locked keyring -> fall through cleanly
            continue
        if isinstance(val, str) and val.strip():
            return val.strip(), uname
    return None, ""


def resolve_api_key(
    tier: str = "tier1",
    *,
    env: dict[str, str] | None = None,
    secrets_path: Path | None = None,
    keyring_module: Any | None = None,
    allow_shared_fallback: bool = True,
) -> SecretResolution:
    """Resolve a Gemini API key for `tier` (`'free'` or `'tier1'`) via the 3-step cascade.

    1. Environment variables (`OGY_GEMINI_API_KEY_<TIER>`, fallback `GEMINI_API_KEY`).
    2. OS keyring (`keyring.get_password('optigravity', ...)`, verified non-empty).
    3. `~/.config/optigravity/secrets.env` (POSIX 0600 strict; adapted on Windows).
    """
    norm_tier = "free" if tier.lower() == "free" else "tier1"
    env_map = env if env is not None else os.environ
    env_candidates = TIER_ENV_VARS[norm_tier] if allow_shared_fallback else (TIER_ENV_VARS[norm_tier][0],)
    kr_candidates = TIER_KEYRING_NAMES[norm_tier] if allow_shared_fallback else (TIER_KEYRING_NAMES[norm_tier][0],)

    # 1. Environment variables
    for var_name in env_candidates:
        val = (env_map.get(var_name) or "").strip()
        if val:
            return SecretResolution(tier=norm_tier, key=val, source=f"env:{var_name}")

    # 2. OS keyring (verified non-empty for SSH/headless safety)
    kr_val, kr_name = _get_from_keyring(kr_candidates, keyring_module=keyring_module)
    if kr_val:
        return SecretResolution(tier=norm_tier, key=kr_val, source=f"keyring:{kr_name}")

    # 3. ~/.config/optigravity/secrets.env
    sec_path = secrets_path or default_secrets_env_path()
    file_entries, warning = _parse_secrets_env_file(sec_path)
    for var_name in env_candidates:
        val = (file_entries.get(var_name) or "").strip()
        if val:
            return SecretResolution(tier=norm_tier, key=val, source=f"file:{sec_path.name}:{var_name}")

    return SecretResolution(tier=norm_tier, key=None, source="none", warning=warning)


def save_secret_to_env_file(
    var_name: str,
    secret_value: str,
    *,
    secrets_path: Path | None = None,
) -> Path:
    """Persist a secret into `~/.config/optigravity/secrets.env` with POSIX 0600 permissions."""
    path = secrets_path or default_secrets_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.is_file():
        # Read existing lines even if permissions are being repaired right now
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                existing[k] = v
    existing[var_name] = secret_value.strip()
    lines = ["# Optigravity API secrets (never commit; chmod 600 on POSIX)"]
    for k, v in sorted(existing.items()):
        lines.append(f'{k}="{v}"')
    content = "\n".join(lines) + "\n"
    if os.name != "nt":
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o600)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def prompt_and_store_secret(
    tier: str = "tier1",
    *,
    prefer_keyring: bool = True,
    secrets_path: Path | None = None,
) -> str:
    """Interactively prompt via `getpass.getpass()` (no echo) and store in keyring or `secrets.env`."""
    norm_tier = "free" if tier.lower() == "free" else "tier1"
    var_name = TIER_ENV_VARS[norm_tier][0]
    kr_name = TIER_KEYRING_NAMES[norm_tier][0]
    val = getpass.getpass(f"Enter Gemini API key for {norm_tier} ({var_name}): ").strip()
    if not val:
        raise ValueError("Empty API key; nothing saved.")
    if prefer_keyring:
        try:
            import keyring  # type: ignore[import-not-found]

            keyring.set_password(KEYRING_SERVICE, kr_name, val)
            if keyring.get_password(KEYRING_SERVICE, kr_name) == val:
                return f"keyring:{kr_name}"
        except Exception:
            pass
    saved = save_secret_to_env_file(var_name, val, secrets_path=secrets_path)
    return f"file:{saved}"


def probe_gemini_api_key(
    api_key: str,
    *,
    model_id: str = "gemini-3.5-flash-lite",
    timeout: float = 4.0,
) -> tuple[str, str]:
    """Zero-token connectivity probe (`GET /v1beta/models/<model>`) without leaking the key."""
    if not api_key or not api_key.strip():
        return "skip", "not configured"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}"
    req = urllib.request.Request(url, headers={"x-goog-api-key": api_key.strip()}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return "ok", f"HTTP 200 (0 tokens, model={model_id})"
            return "warn", f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and model_id != "gemini-2.5-flash":
            return probe_gemini_api_key(api_key, model_id="gemini-2.5-flash", timeout=timeout)
        return "fail", redact_secrets(f"HTTP {exc.code}")
    except Exception as exc:
        return "warn", redact_secrets(f"unreachable ({type(exc).__name__})")
