"""2-Stage Task Router: Stage 1 deterministic heuristic (< 5ms) + Stage 2 SQLite bandit (100% stdlib)."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ogy_core.storage import query_all, write_tx


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Routing recommendation for a prompt/task."""

    stage: str  # "stage1_heuristic", "stage2_bandit", "manual_override"
    category: str
    model_id: str
    effort: str
    mode: str
    features: dict[str, Any]
    predicted_score: float = 0.0
    propensity: float = 1.0
    quota_downgraded: bool = False


STAGE1_TABLE: dict[str, tuple[str, str, str]] = {
    "trivial_edit": ("gemini-4-hydrogen", "low", "accept-edits"),
    "lookup_qa": ("gemini-4-hydrogen", "low", "plan"),
    "routine_code": ("gemini-4-hydrogen", "medium", "auto"),
    "bugfix_local": ("gemini-4-neon", "low", "auto"),
    "bugfix_complex": ("gemini-4-neon", "high", "auto"),
    "refactor_multifile": ("gemini-4-neon", "medium", "auto"),
    "architecture_security": ("gemini-4-neon", "high", "plan"),
}

EFFORT_ORDER = ("low", "medium", "high", "xhigh")

ARCH_SEC_RE = re.compile(
    r"\b(architect|security|auth|oauth|rbac|crypto|encrypt|migration|schema|design|threat|audit)\b",
    re.IGNORECASE,
)
BUG_COMPLEX_RE = re.compile(
    r"\b(race|deadlock|memory leak|intermittent|flaky|concurrency|segfault|corrupt|traceback)\b",
    re.IGNORECASE,
)
BUG_RE = re.compile(r"\b(fix|bug|error|exception|traceback|failing|broken|crash)\b", re.IGNORECASE)
REFACTOR_RE = re.compile(r"\b(refactor|rename|migrate|split|extract|across|multi-file|all files)\b", re.IGNORECASE)
LOOKUP_RE = re.compile(r"^(what|how|why|where|when|explain|show|list|find|is |does |can )\b|\?$", re.IGNORECASE)
TRIVIAL_RE = re.compile(r"\b(typo|comment|docstring|format|lint|rename var|bump version)\b", re.IGNORECASE)


def extract_features(
    prompt: str,
    *,
    has_test_runner: bool = False,
    git_dirty_files: int = 0,
    repo_file_count: int = 0,
    remaining_quota_fraction: float | None = None,
) -> dict[str, Any]:
    """Compute the 8 normalized routing features in < 1 ms."""
    text = (prompt or "").strip()
    at_mentions = len(re.findall(r"@[\w./\\-]+", text))
    has_stacktrace = bool(re.search(r"(Traceback|Error:|Exception:|at line \d+|FAILED)", text))
    is_arch_sec = bool(ARCH_SEC_RE.search(text))
    is_multifile = at_mentions >= 2 or git_dirty_files >= 3 or bool(REFACTOR_RE.search(text))

    return {
        "prompt_len": len(text),
        "at_mentions": at_mentions,
        "repo_file_count": repo_file_count,
        "has_test_runner": has_test_runner,
        "is_multifile": is_multifile,
        "has_stacktrace": has_stacktrace,
        "is_arch_sec": is_arch_sec,
        "quota_low": remaining_quota_fraction is not None and remaining_quota_fraction < 0.15,
    }


def classify_stage1(prompt: str, features: dict[str, Any]) -> str:
    """Deterministic category classification."""
    text = (prompt or "").strip()
    if features["is_arch_sec"]:
        return "architecture_security"
    if TRIVIAL_RE.search(text) and len(text) < 200 and not features["is_multifile"] and not features["has_stacktrace"]:
        return "trivial_edit"
    if BUG_COMPLEX_RE.search(text) or (features["has_stacktrace"] and features["is_multifile"]):
        return "bugfix_complex"
    if features["is_multifile"] and REFACTOR_RE.search(text):
        return "refactor_multifile"
    if BUG_RE.search(text) or features["has_stacktrace"]:
        return "bugfix_local"
    if LOOKUP_RE.search(text) and len(text) < 220:
        return "lookup_qa"
    return "routine_code"


def _downgrade_effort(effort: str) -> str:
    if effort in EFFORT_ORDER:
        idx = EFFORT_ORDER.index(effort)
        if idx > 0:
            return EFFORT_ORDER[idx - 1]
    return effort


def route_stage1(
    prompt: str,
    *,
    has_test_runner: bool = False,
    git_dirty_files: int = 0,
    repo_file_count: int = 0,
    remaining_quota_fraction: float | None = None,
    override_model: str | None = None,
    override_effort: str | None = None,
    override_mode: str | None = None,
) -> RouteDecision:
    """Run Stage 1 heuristic router (< 5 ms)."""
    features = extract_features(
        prompt,
        has_test_runner=has_test_runner,
        git_dirty_files=git_dirty_files,
        repo_file_count=repo_file_count,
        remaining_quota_fraction=remaining_quota_fraction,
    )
    category = classify_stage1(prompt, features)
    model_id, effort, mode = STAGE1_TABLE[category]

    quota_downgraded = False
    if features["quota_low"] and category != "architecture_security":
        new_effort = _downgrade_effort(effort)
        if new_effort != effort:
            effort = new_effort
            quota_downgraded = True

    stage = "stage1_heuristic"
    if override_model or override_effort or override_mode:
        stage = "manual_override"
        if override_model:
            model_id = override_model
        if override_effort:
            effort = override_effort
        if override_mode:
            mode = override_mode

    return RouteDecision(
        stage=stage,
        category=category,
        model_id=model_id,
        effort=effort,
        mode=mode,
        features=features,
        quota_downgraded=quota_downgraded,
    )


def route_stage2_bandit(
    conn: sqlite3.Connection,
    prompt: str,
    *,
    min_qualified_tasks: int = 15,
    decay_eta: float = 0.92,
    remaining_quota_fraction: float | None = None,
    has_test_runner: bool = False,
    git_dirty_files: int = 0,
) -> RouteDecision:
    """Stage 2 UCB1 contextual bandit over SQLite history; falls back to Stage 1 when cold."""
    s1 = route_stage1(
        prompt,
        has_test_runner=has_test_runner,
        git_dirty_files=git_dirty_files,
        remaining_quota_fraction=remaining_quota_fraction,
    )

    rows = query_all(
        conn,
        """
        SELECT rd.chosen_model, rd.chosen_effort, rd.chosen_mode, rd.actual_reward,
               tk.outcome
        FROM routing_decisions rd
        LEFT JOIN tasks tk ON tk.task_id = rd.task_id
        WHERE rd.detected_category = ?
          AND rd.actual_reward IS NOT NULL
        ORDER BY rd.decision_id ASC
        """,
        (s1.category,),
    )
    if len(rows) < min_qualified_tasks:
        return s1

    arm_stats: dict[tuple[str, str, str], dict[str, float]] = {}
    total_pulls = 0
    for r in rows:
        arm = (r["chosen_model"], r["chosen_effort"], r["chosen_mode"])
        if arm not in arm_stats:
            arm_stats[arm] = {"n": 0.0, "ewma_reward": 0.0, "successes": 0.0}
        st = arm_stats[arm]
        st["n"] += 1.0
        reward = float(r["actual_reward"])
        st["ewma_reward"] = reward if st["n"] == 1.0 else (decay_eta * st["ewma_reward"] + (1.0 - decay_eta) * reward)
        if r["outcome"] == "success":
            st["successes"] += 1.0
        total_pulls += 1

    # Quality guardrail: exclude arms with success rate < 80% (when outcome data exists)
    eligible: list[tuple[tuple[str, str, str], float]] = []
    for arm, st in arm_stats.items():
        if st["n"] >= 3 and (st["successes"] / st["n"]) < 0.80:
            continue
        ucb = st["ewma_reward"] + math.sqrt(2.0 * math.log(max(total_pulls, 2)) / st["n"])
        eligible.append((arm, ucb))

    if not eligible:
        return s1

    best_arm, best_score = max(eligible, key=lambda item: item[1])
    chosen_model, chosen_effort, chosen_mode = best_arm
    if s1.features["quota_low"] and s1.category != "architecture_security":
        chosen_effort = _downgrade_effort(chosen_effort)

    return RouteDecision(
        stage="stage2_bandit",
        category=s1.category,
        model_id=chosen_model,
        effort=chosen_effort,
        mode=chosen_mode,
        features=s1.features,
        predicted_score=round(best_score, 4),
        propensity=round(1.0 / len(eligible), 4),
        quota_downgraded=s1.quota_downgraded,
    )


def compute_reward(
    *,
    outcome: str,
    verifier_passed: bool | None,
    cost_usd: float,
    rollback_count: int = 0,
    retry_count: int = 0,
) -> float:
    """Compute normalized reward in [0.0, 1.0] for Stop hook."""
    if outcome in ("failed", "rolled_back", "aborted") or rollback_count > 0:
        return 0.0
    if verifier_passed is True and outcome == "success":
        base = 1.0
    elif outcome == "success":
        base = 0.5
    else:
        base = 0.4
    cost_penalty = min(0.25, cost_usd * 0.1)
    retry_penalty = min(0.25, retry_count * 0.05)
    return round(max(0.0, base - cost_penalty - retry_penalty), 4)


def record_routing_decision(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    decision: RouteDecision,
    task_id: str | None = None,
) -> None:
    """Persist routing decision to SQLite."""
    with write_tx(conn):
        conn.execute(
            """
            INSERT INTO routing_decisions (
              session_id, task_id, stage, detected_category, features_json,
              chosen_model, chosen_effort, chosen_mode, propensity, predicted_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                decision.stage,
                decision.category,
                json.dumps(decision.features, ensure_ascii=False),
                decision.model_id,
                decision.effort,
                decision.mode,
                decision.propensity,
                decision.predicted_score,
            ),
        )


def evaluate_bandit_offline(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Offline shadow evaluation of Stage 2 bandit vs Stage 1 baseline on historical decisions."""
    where = "WHERE rd.actual_reward IS NOT NULL"
    params: list[Any] = []
    if category:
        where += " AND rd.detected_category = ?"
        params.append(category)

    rows = query_all(
        conn,
        f"""
        SELECT rd.detected_category, rd.stage, rd.chosen_model, rd.chosen_effort,
               rd.actual_reward, rd.propensity, tk.outcome
        FROM routing_decisions rd
        LEFT JOIN tasks tk ON tk.task_id = rd.task_id
        {where}
        ORDER BY rd.decision_id ASC
        """,
        tuple(params),
    )

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_cat.setdefault(r["detected_category"], []).append(dict(r))

    report: dict[str, Any] = {"total_decisions": len(rows), "categories": {}}
    for cat, items in by_cat.items():
        if len(items) < min_samples:
            continue
        arms: dict[str, dict[str, float]] = {}
        for it in items:
            key = f"{it['chosen_model']}:{it['chosen_effort']}"
            st = arms.setdefault(key, {"count": 0.0, "reward_sum": 0.0, "successes": 0.0})
            st["count"] += 1.0
            st["reward_sum"] += float(it["actual_reward"] or 0.0)
            if it["outcome"] == "success":
                st["successes"] += 1.0

        summary = {
            arm: {
                "count": int(st["count"]),
                "mean_reward": round(st["reward_sum"] / st["count"], 4),
                "success_rate": round(st["successes"] / st["count"], 4),
            }
            for arm, st in arms.items()
        }
        best_arm = max(summary.items(), key=lambda kv: kv[1]["mean_reward"])
        report["categories"][cat] = {
            "samples": len(items),
            "best_arm": best_arm[0],
            "best_mean_reward": best_arm[1]["mean_reward"],
            "arms": summary,
        }
    return report

