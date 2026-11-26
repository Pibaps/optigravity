"""Live telemetry sidebar widgets: MetricGauge, MetricRow, TelemetryPanel, and subagent tree."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ProgressBar, Static

from ogy_frontends.acp_tui.i18n import ZONE_LABELS, human_quota_label, t
from ogy_frontends.acp_tui.orchestrator import SessionTelemetryState


def _format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _human_quota_label(bucket_id: str, lang: str = "fr") -> str:
    return human_quota_label(bucket_id, lang)


def _safe_widget_id(raw: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "-" for ch in raw)


class MetricGauge(Vertical):
    """Compact labeled progress gauge with dynamic severity coloring."""

    def __init__(
        self,
        label: str,
        initial_value: str = "--",
        *,
        progress: float = 0.0,
        severity: str = "accent",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label_text = label
        self._value_text = initial_value
        self._initial_progress = max(0.0, min(100.0, progress))
        self._severity = severity

    def compose(self) -> ComposeResult:
        with Horizontal(classes="gauge-header"):
            yield Label(self._label_text, classes="gauge-label", markup=False)
            yield Label(self._value_text, classes="gauge-value", markup=False)
        yield ProgressBar(total=100, show_eta=False, show_percentage=False)

    def on_mount(self) -> None:
        self.set_state(self._initial_progress, self._value_text, severity=self._severity)

    def set_label(self, label: str) -> None:
        self.query_one(".gauge-label", Label).update(label)

    def set_state(
        self,
        percent: float,
        value_text: str,
        *,
        label: str | None = None,
        severity: str = "ok",
    ) -> None:
        clamped = max(0.0, min(100.0, float(percent)))
        if label is not None:
            self.query_one(".gauge-label", Label).update(label)
        self.query_one(".gauge-value", Label).update(value_text)
        self.query_one(ProgressBar).update(progress=clamped)
        self.set_classes(f"gauge-{severity}")


class MetricRow(Horizontal):
    """Single aligned key / value telemetry row."""

    def __init__(self, key: str, value: str, *, val_id: str, row_id: str | None = None) -> None:
        super().__init__(id=row_id)
        self._key = key
        self._value = value
        self._val_id = val_id

    def compose(self) -> ComposeResult:
        yield Label(self._key, classes="metric-key", markup=False)
        yield Label(self._value, id=self._val_id, classes="metric-val", markup=False)

    def set_key(self, key: str) -> None:
        self.query_one(".metric-key", Label).update(key)

    def set_value(self, value: str, *, status: str = "") -> None:
        self.query_one(f"#{self._val_id}", Label).update(value)
        self.set_classes(f"val-{status}" if status else "")


class TelemetryPanel(VerticalScroll):
    """Live telemetry sidebar with smooth gauges, FR/EN labels, per-bucket quotas, and subagent tree."""

    def __init__(self, *, lang: str = "fr", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lang = lang
        self._quota_gauges: dict[str, MetricGauge] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            t(self.lang, "section_health"),
            id="sec-health",
            classes="panel-section panel-section-first",
            markup=False,
        )
        zone_title, zone_hint = ZONE_LABELS[self.lang][("green", "keep_warm")]
        yield Label(f"● {zone_title}", id="tel-zone", classes="zone-green", markup=False)
        yield Static(zone_hint, id="tel-zone-hint", markup=False)
        yield MetricGauge(
            t(self.lang, "gauge_context"),
            "0 / 1.0M (0%)",
            progress=0,
            severity="ok",
            id="gauge-ctx",
        )
        yield MetricGauge(
            t(self.lang, "gauge_cache"),
            "--",
            progress=0,
            severity="muted",
            id="gauge-cache",
        )

        yield Static(t(self.lang, "section_session"), id="sec-session", classes="panel-section", markup=False)
        yield MetricRow(t(self.lang, "metric_thought"), "0 tok", val_id="tel-thought", row_id="row-thought")
        yield MetricRow(t(self.lang, "metric_io"), "0 / 0", val_id="tel-io", row_id="row-io")
        yield MetricRow(t(self.lang, "metric_cost"), "$0.0000", val_id="tel-cost", row_id="row-cost")
        yield MetricRow(t(self.lang, "metric_idle"), "0s", val_id="tel-idle", row_id="row-idle")
        yield MetricRow(t(self.lang, "metric_fail"), "0", val_id="tel-fail", row_id="row-fail")

        yield Static(t(self.lang, "section_quotas"), id="sec-quotas", classes="panel-section", markup=False)
        with Vertical(id="quota-list"):
            yield Static(t(self.lang, "quotas_empty"), id="quota-empty", markup=False)

        yield Static(t(self.lang, "section_subagents"), id="sec-subagents", classes="panel-section", markup=False)
        yield MetricRow(t(self.lang, "metric_api_spend"), "$0.0000 · 0 runs", val_id="tel-api-spend", row_id="row-api-spend")
        yield Static(t(self.lang, "subagents_empty"), id="subagent-tree", markup=False)

        yield Static(t(self.lang, "glossary_hint"), id="tel-glossary-hint", markup=False)

    def apply_lang(self, lang: str) -> None:
        self.lang = lang
        self.query_one("#sec-health", Static).update(t(lang, "section_health"))
        self.query_one("#sec-session", Static).update(t(lang, "section_session"))
        self.query_one("#sec-quotas", Static).update(t(lang, "section_quotas"))
        self.query_one("#sec-subagents", Static).update(t(lang, "section_subagents"))
        self.query_one("#gauge-ctx", MetricGauge).set_label(t(lang, "gauge_context"))
        self.query_one("#gauge-cache", MetricGauge).set_label(t(lang, "gauge_cache"))
        self.query_one("#row-thought", MetricRow).set_key(t(lang, "metric_thought"))
        self.query_one("#row-io", MetricRow).set_key(t(lang, "metric_io"))
        self.query_one("#row-cost", MetricRow).set_key(t(lang, "metric_cost"))
        self.query_one("#row-idle", MetricRow).set_key(t(lang, "metric_idle"))
        self.query_one("#row-fail", MetricRow).set_key(t(lang, "metric_fail"))
        self.query_one("#row-api-spend", MetricRow).set_key(t(lang, "metric_api_spend"))
        self.query_one("#quota-empty", Static).update(t(lang, "quotas_empty"))
        self.query_one("#tel-glossary-hint", Static).update(t(lang, "glossary_hint"))

    def refresh_state(
        self,
        state: SessionTelemetryState,
        quotas: dict[str, dict[str, Any]],
        subagent_runs: list[dict[str, Any]] | None = None,
        subagent_summary: dict[str, Any] | None = None,
    ) -> None:
        health = state.assess_health()
        zone_table = ZONE_LABELS.get(self.lang, ZONE_LABELS["fr"])
        zone_title, zone_hint = zone_table.get(
            (health.zone, health.action),
            (f"{health.zone.upper()} · {health.action}", health.reason),
        )
        zone_lbl = self.query_one("#tel-zone", Label)
        zone_lbl.update(f"● {zone_title}")
        zone_lbl.set_classes(f"zone-{health.zone}")
        self.query_one("#tel-zone-hint", Static).update(zone_hint)

        ctx_pct = min(100.0, (state.context_tokens / max(1, state.context_max)) * 100.0)
        ctx_sev = "danger" if health.zone == "red" else ("warn" if health.zone == "amber" else "ok")
        self.query_one("#gauge-ctx", MetricGauge).set_state(
            ctx_pct,
            f"{_format_tokens(state.context_tokens)} / {_format_tokens(state.context_max)} ({int(ctx_pct)}%)",
            severity=ctx_sev,
        )

        if health.cache_hit_pct is not None:
            c_pct = health.cache_hit_pct
            c_sev = "ok" if c_pct >= 70.0 else ("warn" if c_pct >= 40.0 else "danger")
            self.query_one("#gauge-cache", MetricGauge).set_state(c_pct, f"{c_pct:.1f}%", severity=c_sev)
        else:
            self.query_one("#gauge-cache", MetricGauge).set_state(0.0, "--", severity="muted")

        self.query_one("#tel-thought", Label).update(f"{state.thought_tokens:,} tok")
        self.query_one("#tel-io", Label).update(
            f"{_format_tokens(state.input_tokens)} / {_format_tokens(state.output_tokens)}"
        )
        self.query_one("#tel-cost", Label).update(f"${state.total_cost_usd:.4f}")

        idle_s = int(state.idle_gap_s)
        idle_row = self.query_one("#row-idle", MetricRow)
        idle_status = "danger" if idle_s >= 180 else ("warn" if idle_s >= 150 else "")
        idle_row.set_value(f"{idle_s}s", status=idle_status)

        fail_row = self.query_one("#row-fail", MetricRow)
        fail_status = "danger" if state.consecutive_failures >= 2 else ("warn" if state.consecutive_failures == 1 else "")
        fail_row.set_value(str(state.consecutive_failures), status=fail_status)

        self._refresh_quotas(quotas)
        self._refresh_subagents(subagent_runs or [], subagent_summary or {})

    def _refresh_quotas(self, quotas: dict[str, dict[str, Any]]) -> None:
        container = self.query_one("#quota-list", Vertical)
        empty_lbl = self.query_one("#quota-empty", Static)

        valid_items: list[tuple[str, float]] = []
        for bid, info in sorted(quotas.items()):
            frac = info.get("remaining_fraction")
            if frac is not None:
                valid_items.append((bid, max(0.0, min(100.0, float(frac) * 100.0))))

        if not valid_items:
            empty_lbl.display = True
            for gauge in self._quota_gauges.values():
                gauge.display = False
            return

        empty_lbl.display = False
        seen: set[str] = set()
        for bid, pct in valid_items:
            seen.add(bid)
            label = human_quota_label(bid, self.lang)
            sev = "ok" if pct >= 50.0 else ("warn" if pct >= 20.0 else "danger")
            gauge = self._quota_gauges.get(bid)
            if gauge is None:
                gauge = MetricGauge(
                    label,
                    f"{int(round(pct))}%",
                    progress=pct,
                    severity=sev,
                    id=f"quota-{_safe_widget_id(bid)}",
                )
                self._quota_gauges[bid] = gauge
                container.mount(gauge)
            else:
                gauge.display = True
                gauge.set_state(pct, f"{int(round(pct))}%", label=label, severity=sev)

        for bid, gauge in self._quota_gauges.items():
            if bid not in seen:
                gauge.display = False

    def _refresh_subagents(
        self,
        runs: list[dict[str, Any]],
        summary: dict[str, Any] | None = None,
    ) -> None:
        s = summary or {}
        total_runs = int(s.get("runs") or len(runs))
        free_runs = int(s.get("free_runs") or sum(1 for r in runs if str(r.get("tier") or "free") == "free"))
        tier1_runs = int(s.get("tier1_runs") or max(0, total_runs - free_runs))
        api_cost = float(s.get("cost_usd") or sum(float(r.get("cost_usd") or 0.0) for r in runs))
        spend_row = self.query_one("#row-api-spend", MetricRow)
        if total_runs == 0:
            spend_row.set_value("$0.0000 · 0 runs")
        else:
            tier_tag = f"{free_runs}F/{tier1_runs}T1"
            spend_row.set_value(f"${api_cost:.4f} · {tier_tag}", status="warn" if api_cost >= 1.0 else "")

        tree_lbl = self.query_one("#subagent-tree", Static)
        if not runs:
            tree_lbl.update(t(self.lang, "subagents_empty"))
            return
        lines: list[str] = []
        for run in runs[:6]:
            tool = str(run.get("tool_name") or "subagent")
            step = int(run.get("step") or 0)
            max_steps = int(run.get("max_steps") or 1)
            raw_model = str(run.get("model_id") or "")
            short_model = raw_model.removeprefix("gemini-") if raw_model else "?"
            raw_tier = str(run.get("tier") or "free").lower()
            tier_label = "Free" if raw_tier == "free" else "Tier 1"
            status = str(run.get("status") or "running")
            icon = "●" if status == "running" else ("✓" if status == "completed" else "✗")
            lines.append(f"↳ {icon} {tool} [{step}/{max_steps}] · {short_model} · {tier_label}")
        tree_lbl.update("\n".join(lines))
