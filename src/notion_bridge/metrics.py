"""A dependency-free Prometheus text-format registry.

A long-running service needs numbers that survive restarts of the operator's
attention, not just log lines. This keeps the footprint at zero extra
dependencies so the installer stays a single pinned requirement.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

Labels = tuple[tuple[str, str], ...]

# Seconds. Chosen for Notion inference, which is dominated by multi-second
# reasoning rather than sub-millisecond request handling.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300,
)


def _labels(values: dict[str, str | None] | None) -> Labels:
    if not values:
        return ()
    return tuple(
        sorted((key, str(value)) for key, value in values.items() if value is not None)
    )


def _render_labels(labels: Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = tuple(labels) + extra
    if not pairs:
        return ""
    body = ",".join(
        f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, value in pairs
    )
    return f"{{{body}}}"


@dataclass(slots=True)
class _Counter:
    name: str
    help_text: str
    values: dict[Labels, float] = field(default_factory=dict)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


@dataclass(slots=True)
class _Gauge:
    name: str
    help_text: str
    values: dict[Labels, float] = field(default_factory=dict)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


@dataclass(slots=True)
class _Histogram:
    name: str
    help_text: str
    buckets: tuple[float, ...]
    counts: dict[Labels, list[int]] = field(default_factory=dict)
    sums: dict[Labels, float] = field(default_factory=dict)
    totals: dict[Labels, int] = field(default_factory=dict)

    def observe(self, labels: Labels, value: float) -> None:
        counts = self.counts.setdefault(labels, [0] * len(self.buckets))
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                counts[index] += 1
        self.sums[labels] = self.sums.get(labels, 0.0) + value
        self.totals[labels] = self.totals.get(labels, 0) + 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for labels in sorted(self.counts):
            counts = self.counts[labels]
            for index, edge in enumerate(self.buckets):
                bound = ("le", f"{edge:g}")
                lines.append(
                    f"{self.name}_bucket{_render_labels(labels, (bound,))} {counts[index]}"
                )
            total = self.totals[labels]
            lines.append(
                f"{self.name}_bucket{_render_labels(labels, (('le', '+Inf'),))} {total}"
            )
            lines.append(f"{self.name}_sum{_render_labels(labels)} {self.sums[labels]:g}")
            lines.append(f"{self.name}_count{_render_labels(labels)} {total}")
        return lines


class MetricsRegistry:
    """Thread-safe counters, gauges and histograms with Prometheus rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}

    def increment(
        self,
        name: str,
        *,
        help_text: str = "",
        amount: float = 1,
        labels: dict[str, str | None] | None = None,
    ) -> None:
        key = _labels(labels)
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = self._counters[name] = _Counter(name, help_text or name)
            counter.values[key] = counter.values.get(key, 0.0) + amount

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        help_text: str = "",
        labels: dict[str, str | None] | None = None,
    ) -> None:
        key = _labels(labels)
        with self._lock:
            gauge = self._gauges.get(name)
            if gauge is None:
                gauge = self._gauges[name] = _Gauge(name, help_text or name)
            gauge.values[key] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        *,
        help_text: str = "",
        labels: dict[str, str | None] | None = None,
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        key = _labels(labels)
        with self._lock:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._histograms[name] = _Histogram(
                    name, help_text or name, buckets
                )
            histogram.observe(key, float(value))

    def render(self) -> str:
        with self._lock:
            blocks: list[str] = []
            for collection in (self._counters, self._gauges, self._histograms):
                for name in sorted(collection):
                    blocks.extend(collection[name].render())
        return "\n".join(blocks) + "\n"

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Flat view of counters and gauges, useful for tests and diagnostics."""
        with self._lock:
            return {
                "counters": {
                    f"{name}{_render_labels(labels)}": value
                    for name, counter in self._counters.items()
                    for labels, value in counter.values.items()
                },
                "gauges": {
                    f"{name}{_render_labels(labels)}": value
                    for name, gauge in self._gauges.items()
                    for labels, value in gauge.values.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


registry = MetricsRegistry()

REQUESTS = "notion_bridge_requests_total"
REQUEST_DURATION = "notion_bridge_request_duration_seconds"
INFERENCE = "notion_bridge_inference_total"
INFERENCE_DURATION = "notion_bridge_inference_duration_seconds"
TOKENS = "notion_bridge_tokens_total"
ACCOUNTS = "notion_bridge_accounts"
FAILOVERS = "notion_bridge_account_failovers_total"
CIRCUIT_OPENED = "notion_bridge_circuit_breaker_opened_total"
TOOL_CALLS = "notion_bridge_runtime_tool_calls_total"
IN_FLIGHT = "notion_bridge_requests_in_flight"
CONVERSATIONS = "notion_bridge_conversation_segments"
TURN_AFFINITIES = "notion_bridge_turn_affinities"
UP_SINCE = "notion_bridge_start_time_seconds"


def record_request(endpoint: str, status_code: int, duration_seconds: float) -> None:
    registry.increment(
        REQUESTS,
        help_text="Requests handled by the bridge, by endpoint and status class.",
        labels={"endpoint": endpoint, "status": f"{status_code // 100}xx"},
    )
    registry.observe(
        REQUEST_DURATION,
        duration_seconds,
        help_text="Wall-clock duration of bridge requests in seconds.",
        labels={"endpoint": endpoint},
    )


def record_inference(model: str, outcome: str, duration_seconds: float) -> None:
    registry.increment(
        INFERENCE,
        help_text="Notion inference attempts, by model and outcome.",
        labels={"model": model, "outcome": outcome},
    )
    registry.observe(
        INFERENCE_DURATION,
        duration_seconds,
        help_text="Notion inference duration in seconds.",
        labels={"model": model},
    )


def record_tokens(model: str, input_tokens: int, output_tokens: int) -> None:
    for direction, amount in (("input", input_tokens), ("output", output_tokens)):
        if amount:
            registry.increment(
                TOKENS,
                help_text="Tokens reported by Notion, by model and direction.",
                amount=amount,
                labels={"model": model, "direction": direction},
            )
