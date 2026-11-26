"""Isolated multi-session registry and read-only SQLite telemetry snapshots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ogy_core.ingest import get_latest_quota_snapshots
from ogy_core.storage import (
    connect,
    connect_readonly,
    get_subagent_spend_summary,
    list_active_subagent_runs,
    list_recent_sessions_meta,
    load_prompt_history,
    record_prompt_history,
)
from ogy_frontends.acp_tui.orchestrator import SessionTelemetryState


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """Thread-safe snapshot polled from SQLite in read-only mode."""

    quotas: dict[str, dict[str, Any]] = field(default_factory=dict)
    subagent_runs: list[dict[str, Any]] = field(default_factory=list)
    subagent_summary: dict[str, Any] = field(default_factory=dict)


def fetch_telemetry_snapshot(db_path: Path, active_session_id: str | None = None) -> TelemetrySnapshot:
    """Read quota_snapshots and subagent_runs strictly via `connect_readonly()` (run in worker thread)."""
    if not db_path.is_file():
        return TelemetrySnapshot()
    try:
        conn = connect_readonly(db_path)
        try:
            quotas = get_latest_quota_snapshots(conn)
            try:
                runs = list_active_subagent_runs(conn, parent_session_id=active_session_id)
                if not runs and active_session_id:
                    runs = list_active_subagent_runs(conn, parent_session_id=None)
                summary = get_subagent_spend_summary(conn)
            except Exception:
                runs = []
                summary = {}
            return TelemetrySnapshot(quotas=quotas, subagent_runs=runs, subagent_summary=summary)
        finally:
            conn.close()
    except Exception:
        return TelemetrySnapshot()


def load_prompt_history_readonly(db_path: Path, repo_root: str | None = None, limit: int = 200) -> list[str]:
    """Load persisted prompt history strictly read-only (safe for background worker thread)."""
    if not db_path.is_file():
        return []
    try:
        conn = connect_readonly(db_path)
        try:
            return load_prompt_history(conn, repo_root=repo_root, limit=limit)
        finally:
            conn.close()
    except Exception:
        return []


def save_prompt_history_entry(
    db_path: Path,
    *,
    repo_root: str,
    prompt_text: str,
    session_id: str | None = None,
) -> None:
    """Persist a submitted prompt to SQLite (call from background worker thread)."""
    try:
        conn = connect(db_path, init_schema=True)
        try:
            record_prompt_history(conn, repo_root=repo_root, prompt_text=prompt_text, session_id=session_id)
        finally:
            conn.close()
    except Exception:
        pass


def load_sessions_meta_readonly(db_path: Path, limit: int = 50) -> dict[str, dict[str, Any]]:
    """Load session metadata from ogy.db read-only to enrich SessionPickerModal."""
    if not db_path.is_file():
        return {}
    try:
        conn = connect_readonly(db_path)
        try:
            return list_recent_sessions_meta(conn, limit=limit)
        finally:
            conn.close()
    except Exception:
        return {}


class SessionRegistry:
    """Tracks isolated per-session telemetry state, routing flags, and pending prompt queues."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionTelemetryState] = {}
        self.active_session_id: str | None = None
        self.routed_sessions: set[str] = set()
        self.manual_override_sessions: set[str] = set()
        self.prompt_queues: dict[str, deque[str]] = {}
        self.latest_snapshot: TelemetrySnapshot = TelemetrySnapshot()

    def ensure(
        self,
        session_id: str,
        *,
        config_options: dict[str, str] | None = None,
    ) -> SessionTelemetryState:
        """Return existing telemetry state for `session_id` or initialize a fresh one."""
        if session_id not in self.sessions:
            cfg = config_options or {}
            self.sessions[session_id] = SessionTelemetryState(
                session_id=session_id,
                model_id=cfg.get("model", "gemini-4-hydrogen"),
                effort=cfg.get("thinking_level") or cfg.get("thought_level") or "medium",
                mode=cfg.get("mode", "auto"),
            )
            self.prompt_queues.setdefault(session_id, deque())
        return self.sessions[session_id]

    def set_active(
        self,
        session_id: str,
        *,
        config_options: dict[str, str] | None = None,
    ) -> SessionTelemetryState:
        self.active_session_id = session_id
        return self.ensure(session_id, config_options=config_options)

    @property
    def current(self) -> SessionTelemetryState | None:
        if not self.active_session_id:
            return None
        return self.sessions.get(self.active_session_id)

    def mark_routed(self, session_id: str) -> None:
        self.routed_sessions.add(session_id)

    def mark_manual_override(self, session_id: str) -> None:
        self.manual_override_sessions.add(session_id)

    def enqueue_prompt(self, session_id: str, text: str) -> None:
        self.prompt_queues.setdefault(session_id, deque()).append(text)

    def pop_prompt(self, session_id: str) -> str | None:
        q = self.prompt_queues.get(session_id)
        if q:
            return q.popleft()
        return None

    def min_quota_fraction(self) -> float | None:
        """Return minimum remaining_fraction from the latest non-blocking telemetry snapshot."""
        fracs = [
            float(info["remaining_fraction"])
            for info in self.latest_snapshot.quotas.values()
            if info.get("remaining_fraction") is not None
        ]
        return min(fracs) if fracs else None

    def update_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self.latest_snapshot = snapshot
