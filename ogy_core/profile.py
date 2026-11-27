"""V3.1 profile isolation, OAuth linking, and full .gemini backup/restore (100% stdlib)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ogy_core.config import OgyConfig

BACKUP_SCHEMA_VERSION = 1

# Core JSON files relative to ~/.gemini
TRACKED_JSON_FILES: tuple[str, ...] = (
    "antigravity-cli/settings.json",
    "config/hooks.json",
    "config/mcp_config.json",
)

# Directories relative to ~/.gemini whose regular files are snapshotted & restored
TRACKED_DIRS: tuple[str, ...] = (
    "config/rules",
    "config/skills",
)

# OAuth/auth files in ~/.gemini linked into isolated profile (never duplicated unless fallback)
OAUTH_FILE_TARGETS: tuple[str, ...] = (
    "oauth_creds.json",
    "google_accounts.json",
    "installation_id",
)

MANAGED_KEY = "ogy-managed"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_gemini_state(gemini_dir: Path) -> dict[str, dict[str, Any]]:
    """Inventory tracked files and directories under gemini_dir."""
    files: dict[str, dict[str, Any]] = {}

    for rel in TRACKED_JSON_FILES:
        p = gemini_dir / Path(rel)
        if p.is_file():
            files[rel] = {
                "existed": True,
                "sha256": _sha256_file(p),
                "size": p.stat().st_size,
            }
        else:
            files[rel] = {
                "existed": False,
                "sha256": None,
                "size": 0,
            }

    for rel_dir in TRACKED_DIRS:
        base = gemini_dir / Path(rel_dir)
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if item.is_file():
                rel_posix = item.relative_to(gemini_dir).as_posix()
                files[rel_posix] = {
                    "existed": True,
                    "sha256": _sha256_file(item),
                    "size": item.stat().st_size,
                }

    return files


def _compute_tree_hash(files: dict[str, dict[str, Any]]) -> str:
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def list_gemini_backups(backups_dir: Path) -> list[dict[str, Any]]:
    """Return all valid snapshots sorted chronologically (oldest first)."""
    if not backups_dir.is_dir():
        return []
    snapshots: list[dict[str, Any]] = []
    for child in sorted(backups_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        files_map = data.get("files") if isinstance(data.get("files"), dict) else {}
        existing_count = sum(1 for v in files_map.values() if isinstance(v, dict) and v.get("existed"))
        snapshots.append(
            {
                "snapshot_id": str(data.get("snapshot_id") or child.name),
                "path": child,
                "created_at": str(data.get("created_at") or ""),
                "profile_mode": str(data.get("profile_mode") or "shared"),
                "reason": str(data.get("reason") or "manual"),
                "tree_hash": str(data.get("tree_hash") or ""),
                "file_count": existing_count,
                "manifest": data,
            }
        )
    return snapshots


def create_gemini_backup(
    gemini_dir: Path,
    backups_dir: Path,
    *,
    profile_mode: str = "shared",
    reason: str = "manual",
    force: bool = False,
) -> dict[str, Any]:
    """Create a timestamped full snapshot of tracked .gemini files with SHA-256 manifest.

    Uses `datetime.now(timezone.utc)` for `time-machine` determinism.
    Deduplicates against the latest snapshot unless `force=True`.
    """
    files = _collect_gemini_state(gemini_dir)
    tree_hash = _compute_tree_hash(files)

    existing = list_gemini_backups(backups_dir)
    if not force and existing and existing[-1]["tree_hash"] == tree_hash:
        latest = existing[-1]
        return {
            "snapshot_id": latest["snapshot_id"],
            "path": latest["path"],
            "created": False,
            "file_count": latest["file_count"],
            "tree_hash": tree_hash,
        }

    now = datetime.now(timezone.utc)
    base_id = now.strftime("%Y%m%d-%H%M%S")
    snapshot_id = base_id
    snap_dir = backups_dir / snapshot_id
    suffix = 1
    while snap_dir.exists():
        snapshot_id = f"{base_id}-{suffix}"
        snap_dir = backups_dir / snapshot_id
        suffix += 1

    files_root = snap_dir / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    for rel_posix, meta in files.items():
        if not meta.get("existed"):
            continue
        src = gemini_dir / Path(rel_posix)
        dst = files_root / Path(rel_posix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gemini_dir": gemini_dir.as_posix(),
        "profile_mode": profile_mode,
        "reason": reason,
        "tree_hash": tree_hash,
        "files": files,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    existing_count = sum(1 for v in files.values() if v.get("existed"))
    return {
        "snapshot_id": snapshot_id,
        "path": snap_dir,
        "created": True,
        "file_count": existing_count,
        "tree_hash": tree_hash,
    }


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def restore_gemini_backup(
    gemini_dir: Path,
    backups_dir: Path,
    *,
    snapshot_id: str | None = None,
    latest: bool = False,
    oldest: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Restore .gemini exactly to a saved snapshot (byte-for-byte + delete files added since snapshot)."""
    snapshots = list_gemini_backups(backups_dir)
    if not snapshots:
        raise FileNotFoundError(f"No backups found in {backups_dir}")

    target_snap: dict[str, Any] | None = None
    if snapshot_id:
        for s in snapshots:
            if s["snapshot_id"] == snapshot_id:
                target_snap = s
                break
        if target_snap is None:
            raise FileNotFoundError(f"Snapshot '{snapshot_id}' not found in {backups_dir}")
    elif oldest:
        target_snap = snapshots[0]
    elif latest:
        target_snap = snapshots[-1]
    else:
        raise ValueError("Specify a snapshot_id, latest=True, or oldest=True.")

    snap_dir: Path = target_snap["path"]
    manifest: dict[str, Any] = target_snap["manifest"]
    manifest_files: dict[str, Any] = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    files_root = snap_dir / "files"

    actions: list[str] = []

    # 1. Restore files that existed in snapshot, or remove tracked files that did NOT exist
    for rel_posix, meta in sorted(manifest_files.items()):
        if not isinstance(meta, dict):
            continue
        dst = gemini_dir / Path(rel_posix)
        if meta.get("existed"):
            src = files_root / Path(rel_posix)
            if not src.is_file():
                raise FileNotFoundError(f"Backup payload missing file: {src}")
            actions.append(f"Restore {rel_posix}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        else:
            if dst.exists() or dst.is_symlink():
                actions.append(f"Delete {rel_posix} (did not exist in snapshot)")
                if not dry_run:
                    dst.unlink(missing_ok=True)

    # 2. Delete any files currently under tracked dirs (rules/, skills/) that were not in snapshot
    for rel_dir in TRACKED_DIRS:
        base = gemini_dir / Path(rel_dir)
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if item.is_file() or item.is_symlink():
                rel_posix = item.relative_to(gemini_dir).as_posix()
                if rel_posix not in manifest_files or not manifest_files[rel_posix].get("existed"):
                    actions.append(f"Delete {rel_posix} (added after snapshot)")
                    if not dry_run:
                        item.unlink(missing_ok=True)
        if not dry_run:
            _prune_empty_dirs(base)

    return {
        "snapshot_id": target_snap["snapshot_id"],
        "actions": actions,
        "dry_run": dry_run,
    }


def _is_loop_or_same(src: Path, dst: Path) -> bool:
    """Return True if linking src -> dst would create a self-link or directory cycle."""
    try:
        src_res = src.resolve()
        dst_parent_res = dst.parent.resolve()
    except OSError:
        return False
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src_res:
                # If dst is already a link pointing to src, not a cycle — handled by caller
                return False
        except OSError:
            pass
    # Prevent linking an ancestor directory into its own descendant
    if src.is_dir():
        try:
            dst_parent_res.relative_to(src_res)
            return True
        except ValueError:
            pass
    return False


def _create_dir_link(src: Path, dst: Path) -> str:
    """Link directory src -> dst using NTFS junction on Windows or symlink on POSIX."""
    if os.name == "nt":
        # NTFS junction works without Administrator or Developer Mode on Windows
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return "junction"
    os.symlink(src, dst, target_is_directory=True)
    return "symlink"


def _create_file_link(src: Path, dst: Path) -> str:
    """Link file src -> dst using symlink, NTFS hardlink fallback, or copy fallback."""
    try:
        os.symlink(src, dst)
        return "symlink"
    except OSError:
        pass
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        pass
    shutil.copy2(src, dst)
    return "copy"


def link_oauth_to_isolated_profile(
    shared_gemini_dir: Path,
    isolated_gemini_dir: Path,
    *,
    extra_dir_targets: tuple[str, ...] = (),
) -> dict[str, str]:
    """Link OAuth files (and optional dirs) from shared ~/.gemini into isolated profile.

    Returns mapping of target_name -> status ('symlink'|'hardlink'|'junction'|'copy'|'already_linked'|'missing'|'skipped_loop').
    """
    isolated_gemini_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}

    for name in OAUTH_FILE_TARGETS:
        src = shared_gemini_dir / name
        dst = isolated_gemini_dir / name
        if not src.is_file():
            results[name] = "missing"
            continue
        if _is_loop_or_same(src, dst):
            results[name] = "skipped_loop"
            continue
        if dst.exists() or dst.is_symlink():
            try:
                if dst.samefile(src):
                    results[name] = "already_linked"
                    continue
            except OSError:
                pass
            dst.unlink(missing_ok=True)
        results[name] = _create_file_link(src, dst)

    for rel_dir in extra_dir_targets:
        src = shared_gemini_dir / Path(rel_dir)
        dst = isolated_gemini_dir / Path(rel_dir)
        if not src.is_dir():
            results[rel_dir] = "missing"
            continue
        if _is_loop_or_same(src, dst):
            results[rel_dir] = "skipped_loop"
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            try:
                if dst.samefile(src):
                    results[rel_dir] = "already_linked"
                    continue
            except OSError:
                pass
            if dst.is_symlink() or (os.name == "nt" and dst.is_dir()):
                try:
                    dst.rmdir()
                except OSError:
                    shutil.rmtree(dst, ignore_errors=True)
            else:
                shutil.rmtree(dst, ignore_errors=True)
        results[rel_dir] = _create_dir_link(src, dst)

    return results


def check_oauth_links(shared_gemini_dir: Path, isolated_gemini_dir: Path) -> tuple[bool, list[str]]:
    """Verify OAuth link integrity in isolated mode. Returns (ok, details)."""
    details: list[str] = []
    ok = True
    any_src = False

    for name in OAUTH_FILE_TARGETS:
        src = shared_gemini_dir / name
        dst = isolated_gemini_dir / name
        if not src.is_file():
            continue
        any_src = True
        if not dst.exists() and not dst.is_symlink():
            ok = False
            details.append(f"{name}: missing in isolated profile")
            continue
        if dst.is_symlink() and not dst.exists():
            ok = False
            details.append(f"{name}: broken symlink")
            continue
        try:
            if dst.samefile(src):
                kind = "symlink" if dst.is_symlink() else "hardlink"
                details.append(f"{name}: {kind} OK")
                continue
        except OSError:
            pass
        # Fallback copy check
        if dst.is_file() and _sha256_file(dst) == _sha256_file(src):
            details.append(f"{name}: synced copy OK")
        else:
            ok = False
            details.append(f"{name}: out of sync with {src}")

    if not any_src:
        details.append("no OAuth files present in shared ~/.gemini yet")
    return ok, details


def detect_ogy_residues_in_shared_gemini(shared_gemini_dir: Path) -> list[str]:
    """Detect any ogy-managed entries still present in shared ~/.gemini/."""
    residues: list[str] = []

    settings_path = shared_gemini_dir / "antigravity-cli" / "settings.json"
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            sl = data.get("statusLine") if isinstance(data, dict) else None
            if isinstance(sl, dict) and (sl.get(MANAGED_KEY) is True or sl.get("command") == "ogy-hook statusline"):
                residues.append("antigravity-cli/settings.json (statusLine)")
        except (OSError, json.JSONDecodeError):
            pass

    hooks_path = shared_gemini_dir / "config" / "hooks.json"
    if hooks_path.is_file():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                found = [
                    k
                    for k, v in data.items()
                    if (isinstance(v, dict) and v.get(MANAGED_KEY) is True)
                    or k in ("ogy-telemetry-and-budget", "ogy-firebreaks")
                ]
                if found:
                    residues.append(f"config/hooks.json ({', '.join(found)})")
        except (OSError, json.JSONDecodeError):
            pass

    mcp_path = shared_gemini_dir / "config" / "mcp_config.json"
    if mcp_path.is_file():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            srvs = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(srvs, dict):
                found = [
                    k
                    for k, v in srvs.items()
                    if isinstance(v, dict)
                    and (v.get(MANAGED_KEY) is True or v.get("command") in ("ogy-mcp", "ogy-subagents"))
                ]
                if found:
                    residues.append(f"config/mcp_config.json ({', '.join(found)})")
        except (OSError, json.JSONDecodeError):
            pass

    if (shared_gemini_dir / "config" / "rules" / "ogy-hygiene.md").is_file():
        residues.append("config/rules/ogy-hygiene.md")
    if (shared_gemini_dir / "config" / "skills" / "ogy-budget" / "SKILL.md").is_file():
        residues.append("config/skills/ogy-budget/SKILL.md")

    return residues


def build_agy_subprocess_env(
    cfg: OgyConfig,
    *,
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct child environment for `agy` / `agy acp` with V3.1 isolation and OGY_ACTIVE=1."""
    env = dict(os.environ if base_env is None else base_env)
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["OGY_ACTIVE"] = "1"

    if cfg.profile_mode == "isolated" or os.environ.get("OGY_GEMINI_HOME", "").strip():
        profile_root = cfg.isolated_profile_root or cfg.agy_cli_dir.parent.parent
        gemini_dir = cfg.gemini_home or cfg.agy_cli_dir.parent
        real_home = cfg.shared_gemini_dir.parent if cfg.shared_gemini_dir else Path.home()

        env["OGY_REAL_HOME"] = str(real_home)
        env["OGY_DB_PATH"] = str(cfg.db_path)
        env["OGY_CONFIG_PATH"] = str(cfg.config_path)
        env["OGY_GEMINI_HOME"] = str(gemini_dir)
        env["USERPROFILE"] = str(profile_root)
        env["HOME"] = str(profile_root)

    if extra_env:
        env.update(extra_env)
    return env
