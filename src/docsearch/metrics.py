"""A small Prometheus-format metrics registry (counters, gauges and histograms), no dependencies.

Enough for what the service needs: request counts and latencies per route, and gauges that are
computed from the database when the endpoint is scraped (queue depth, documents, postings). The
output follows the Prometheus text exposition format (version 0.0.4).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping

Labels = tuple[tuple[str, str], ...]
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(values: Mapping[str, str]) -> Labels:
    return tuple(sorted(values.items()))


def _escape(value: str) -> str:
    """Label values escape backslash, double quote and newline (exposition format 0.0.4)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: Labels, extra: Iterable[tuple[str, str]] = ()) -> str:
    pairs = [*labels, *extra]
    if not pairs:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value)}"' for name, value in pairs) + "}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = {}
        self._histograms: dict[str, dict[Labels, tuple[list[int], float, int]]] = {}
        self._help: dict[str, tuple[str, str]] = {}
        self._gauges: list[Callable[[], Iterable[tuple[str, Mapping[str, str], float]]]] = []
        self._buckets: dict[str, tuple[float, ...]] = {}

    def describe(self, name: str, kind: str, text: str) -> None:
        self._help[name] = (kind, text)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = _labels(labels)
            series[key] = series.get(key, 0.0) + amount

    def observe(
        self, name: str, value: float, buckets: tuple[float, ...] = DEFAULT_BUCKETS, **labels: str
    ) -> None:
        with self._lock:
            self._buckets.setdefault(name, buckets)
            series = self._histograms.setdefault(name, {})
            key = _labels(labels)
            counts, total, count = series.get(key, ([0] * len(buckets), 0.0, 0))
            for index, bound in enumerate(buckets):
                if value <= bound:
                    counts[index] += 1
            series[key] = (counts, total + value, count + 1)

    def gauge_source(
        self, source: Callable[[], Iterable[tuple[str, Mapping[str, str], float]]]
    ) -> None:
        """Register a callback that yields (name, labels, value) when the registry is rendered."""
        self._gauges.append(source)

    def render(self) -> str:
        lines: list[str] = []

        def header(name: str, fallback: str) -> None:
            kind, text = self._help.get(name, (fallback, name))
            lines.append(f"# HELP {name} {text}")
            lines.append(f"# TYPE {name} {kind}")

        with self._lock:
            for name, series in sorted(self._counters.items()):
                header(name, "counter")
                lines.extend(
                    f"{name}{_render_labels(labels)} {value:g}"
                    for labels, value in sorted(series.items())
                )
            for name, hist in sorted(self._histograms.items()):
                header(name, "histogram")
                buckets = self._buckets[name]
                for labels, (counts, total, count) in sorted(hist.items()):
                    for bound, bucket_count in zip(buckets, counts, strict=True):
                        le = _render_labels(labels, [("le", f"{bound:g}")])
                        lines.append(f"{name}_bucket{le} {bucket_count}")
                    lines.append(f"{name}_bucket{_render_labels(labels, [('le', '+Inf')])} {count}")
                    lines.append(f"{name}_sum{_render_labels(labels)} {total:g}")
                    lines.append(f"{name}_count{_render_labels(labels)} {count}")
        seen: set[str] = set()
        for source in self._gauges:
            for name, gauge_labels, value in source():
                if name not in seen:
                    header(name, "gauge")
                    seen.add(name)
                lines.append(f"{name}{_render_labels(_labels(gauge_labels))} {value:g}")
        return "\n".join(lines) + "\n"
