#!/usr/bin/env python3
"""Republish Copilot CLI HydraFusion session events as OTLP traces and metrics.

The CLI's OpenTelemetry output describes the turn: one chat operation with
gen_ai.request.model=hydrafusion, which is what the GenAI semantic conventions
define that field to mean. Those conventions have no field for a router that
fans one request across several models, so the per-leg detail has nowhere to go
(see docs/SPIKE.md). It is recorded instead in
~/.copilot/session-state/<id>/events.jsonl, which backs session resume and
rewind. This tails those files and rebuilds each fusion as a trace: one root
span per turn, one child span per phase, labelled with the model that served it.

Standard library only, by design. No pip install, no parser framework.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("COPILOT_STATE_DIR", "/session-state"))
ENDPOINT = os.environ.get("OTLP_ENDPOINT", "http://otel-collector:4318").rstrip("/")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
BACKFILL = os.environ.get("BACKFILL", "false").lower() in ("1", "true", "yes")

SPAN_KIND_INTERNAL = 1
STATUS_OK, STATUS_ERROR = 1, 2

# Tempo only indexes a trace for search if its span timestamps sit near its ingestion
# time. Replaying an old session with its original timestamps produces traces that are
# reachable by ID and invisible to every search. So a replayed fusion is stamped at
# replay time instead, keeping the durations and the ordering within the turn intact.
REPLAY_AGE_LIMIT_NS = 60 * 1_000_000_000

log = logging.getLogger("fusion-tailer")


# --- OTLP encoding ---------------------------------------------------------

def _attr(key: str, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _attrs(mapping: dict) -> list:
    return [_attr(k, v) for k, v in mapping.items() if v is not None]


def _trace_id(seed: str) -> str:
    return hashlib.sha1(f"trace:{seed}".encode()).hexdigest()[:32]


def _span_id(seed: str) -> str:
    return hashlib.sha1(f"span:{seed}".encode()).hexdigest()[:16]


def _nanos(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1e9)


def _post(path: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        log.warning("OTLP %s rejected: %s %s", path, exc.code, exc.read()[:300])
    except OSError as exc:
        log.warning("OTLP %s unreachable: %s", path, exc)


RESOURCE = {
    "attributes": _attrs(
        {"service.name": "hydrafusion-tailer", "service.version": "0.1.0"}
    )
}


def emit_spans(spans: list) -> None:
    if not spans:
        return
    _post(
        "/v1/traces",
        {
            "resourceSpans": [
                {
                    "resource": RESOURCE,
                    "scopeSpans": [
                        {"scope": {"name": "hydrafusion.tailer"}, "spans": spans}
                    ],
                }
            ]
        },
    )


# --- cumulative counters ---------------------------------------------------

class Counters:
    """Cumulative monotonic sums, keyed by metric name and label set."""

    def __init__(self) -> None:
        self._values: dict[tuple, float] = {}
        self._units: dict[str, str] = {}
        self._start = int(time.time() * 1e9)

    def add(self, name: str, labels: dict, value: float, unit: str = "1") -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items() if v is not None)))
        self._values[key] = self._values.get(key, 0.0) + value
        self._units[name] = unit

    def payload(self) -> dict | None:
        if not self._values:
            return None
        now = int(time.time() * 1e9)
        by_name: dict[str, list] = {}
        for (name, labels), value in self._values.items():
            by_name.setdefault(name, []).append(
                {
                    "attributes": [_attr(k, v) for k, v in labels],
                    "startTimeUnixNano": str(self._start),
                    "timeUnixNano": str(now),
                    "asDouble": value,
                }
            )
        metrics = [
            {
                "name": name,
                "unit": self._units[name],
                "sum": {
                    "dataPoints": points,
                    "aggregationTemporality": 2,
                    "isMonotonic": True,
                },
            }
            for name, points in by_name.items()
        ]
        return {
            "resourceMetrics": [
                {
                    "resource": RESOURCE,
                    "scopeMetrics": [
                        {"scope": {"name": "hydrafusion.tailer"}, "metrics": metrics}
                    ],
                }
            ]
        }

    def flush(self) -> None:
        payload = self.payload()
        if payload:
            _post("/v1/metrics", payload)


COUNTERS = Counters()


# --- fusion assembly -------------------------------------------------------

class Fusion:
    """Accumulates the events belonging to one fusion turn."""

    def __init__(self, session: str, data: dict) -> None:
        self.session = session
        self.id = data.get("fusionId", "")
        self.pattern = data.get("pattern", "unknown")
        self.policy = data.get("policy")
        self.route_source = data.get("routeSource")
        self.primary_model = data.get("primaryModel")
        self.secondary_model = data.get("secondaryModel")
        self.routing_latency_ms = data.get("routingLatencyMs")
        self.phases: list[dict] = []
        self.handoffs: list[dict] = []
        self.shift = 0

    def phase_span(
        self, index: int, event: dict, trace_id: str, parent: str, committed: bool
    ) -> dict:
        data = event["data"]
        usage = data.get("usage") or {}
        end = _nanos(event["timestamp"]) + self.shift
        start = end - int((data.get("durationMs") or 0) * 1e6)
        model = data.get("model") or "unknown"
        role = data.get("role") or data.get("phaseKind") or "phase"
        failed = data.get("status") not in (None, "succeeded")
        return {
            "traceId": trace_id,
            "spanId": _span_id(data.get("phaseId") or f"{self.id}:{index}"),
            "parentSpanId": parent,
            "name": f"{role} {model}",
            "kind": SPAN_KIND_INTERNAL,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": _attrs(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "github",
                    "gen_ai.request.model": "hydrafusion",
                    "gen_ai.response.model": model,
                    "gen_ai.conversation.id": self.session,
                    "gen_ai.usage.input_tokens": usage.get("inputTokens"),
                    "gen_ai.usage.output_tokens": usage.get("outputTokens"),
                    "gen_ai.usage.cached_tokens": usage.get("cachedTokens"),
                    "gen_ai.usage.cache_write_tokens": usage.get("cacheWriteTokens"),
                    "gen_ai.usage.request_count": usage.get("requestCount"),
                    "github.copilot.nano_aiu": usage.get("totalNanoAiu"),
                    "hydrafusion.aiu": (usage.get("totalNanoAiu") or 0) / 1e9,
                    "hydrafusion.fusion.id": self.id,
                    "hydrafusion.pattern": self.pattern,
                    "hydrafusion.phase.index": index,
                    "hydrafusion.phase.kind": data.get("phaseKind"),
                    "hydrafusion.phase.role": data.get("role"),
                    "hydrafusion.phase.status": data.get("status"),
                    "hydrafusion.phase.verdict": data.get("verdict"),
                    "hydrafusion.phase.scope": data.get("conversationScope"),
                    "hydrafusion.phase.committed": committed,
                }
            ),
            "status": {"code": STATUS_ERROR if failed else STATUS_OK},
        }

    def complete(self, event: dict) -> None:
        data = event["data"]
        trace_id = _trace_id(self.id)
        root_id = _span_id(f"{self.id}:root")
        raw_end = _nanos(event["timestamp"])
        if raw_end < time.time_ns() - REPLAY_AGE_LIMIT_NS:
            self.shift = time.time_ns() - raw_end
        end = raw_end + self.shift
        start = end - int((data.get("durationMs") or 0) * 1e6)
        pattern = data.get("pattern") or self.pattern
        final_model = data.get("finalSourceModel") or "unknown"
        outcome = data.get("outcome") or "unknown"

        models = sorted({p["data"].get("model") for p in self.phases if p["data"].get("model")})

        # One phase supplies the committed answer; the others are review passes and
        # superseded drafts, which still feed the context of the leg that follows.
        committed_id = data.get("finalSourcePhaseId")
        uncommitted_aiu = sum(
            ((p["data"].get("usage") or {}).get("totalNanoAiu") or 0) / 1e9
            for p in self.phases
            if p["data"].get("phaseId") != committed_id
        )

        root = {
            "traceId": trace_id,
            "spanId": root_id,
            "name": f"fusion {pattern}",
            "kind": SPAN_KIND_INTERNAL,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": _attrs(
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.provider.name": "github",
                    "gen_ai.request.model": "hydrafusion",
                    "gen_ai.response.model": final_model,
                    "gen_ai.conversation.id": self.session,
                    "gen_ai.usage.input_tokens": data.get("inputTokens"),
                    "gen_ai.usage.output_tokens": data.get("outputTokens"),
                    "gen_ai.usage.cached_tokens": data.get("cachedTokens"),
                    "gen_ai.usage.cache_write_tokens": data.get("cacheWriteTokens"),
                    "gen_ai.usage.request_count": data.get("requestCount"),
                    "github.copilot.nano_aiu": data.get("totalNanoAiu"),
                    "hydrafusion.aiu": (data.get("totalNanoAiu") or 0) / 1e9,
                    "hydrafusion.fusion.id": self.id,
                    "hydrafusion.pattern": pattern,
                    "hydrafusion.outcome": outcome,
                    "hydrafusion.degraded_reason": data.get("degradedReason"),
                    "hydrafusion.phase_count": data.get("phaseCount"),
                    "hydrafusion.models": ",".join(models),
                    "hydrafusion.distinct_models": len(models),
                    "hydrafusion.compound": len(models) > 1,
                    "hydrafusion.uncommitted_aiu": uncommitted_aiu,
                    "hydrafusion.policy": self.policy,
                    "hydrafusion.route_source": self.route_source,
                    "hydrafusion.routing_latency_ms": self.routing_latency_ms,
                    "hydrafusion.primary_model": self.primary_model,
                    "hydrafusion.secondary_model": self.secondary_model,
                }
            ),
            "status": {"code": STATUS_OK if outcome == "completed" else STATUS_ERROR},
        }

        spans = [root]
        for index, phase in enumerate(self.phases):
            committed = phase["data"].get("phaseId") == committed_id
            spans.append(self.phase_span(index, phase, trace_id, root_id, committed))
        for handoff in self.handoffs:
            root.setdefault("events", []).append(
                {
                    "timeUnixNano": str(_nanos(handoff["timestamp"]) + self.shift),
                    "name": "fusion_handoff",
                    "attributes": _attrs(
                        {
                            "hydrafusion.handoff.from": handoff["data"].get("sourcePhaseId"),
                            "hydrafusion.handoff.to": handoff["data"].get("targetPhaseId"),
                            "hydrafusion.handoff.target_model": handoff["data"].get("targetModel"),
                        }
                    ),
                }
            )

        emit_spans(spans)
        self._count(data, pattern, final_model, outcome, models, committed_id, uncommitted_aiu)
        log.info(
            "fusion %s pattern=%s phases=%d models=%s aiu=%.2f",
            self.id[:20],
            pattern,
            len(self.phases),
            ",".join(models) or "-",
            (data.get("totalNanoAiu") or 0) / 1e9,
        )

    def _count(
        self,
        data: dict,
        pattern: str,
        final_model: str,
        outcome: str,
        models: list,
        committed_id: str | None,
        uncommitted_aiu: float,
    ) -> None:
        base = {"session": self.session, "pattern": pattern}
        COUNTERS.add(
            "hydrafusion.fusion.count",
            {**base, "outcome": outcome, "final_model": final_model,
             "route_source": self.route_source, "policy": self.policy},
            1,
        )
        COUNTERS.add("hydrafusion.fusion.compound.count", base, 1 if len(models) > 1 else 0)
        COUNTERS.add("hydrafusion.fusion.distinct_models", base, len(models))
        COUNTERS.add("hydrafusion.fusion.phases", base, data.get("phaseCount") or len(self.phases))
        COUNTERS.add("hydrafusion.fusion.requests", base, data.get("requestCount") or 0)
        COUNTERS.add(
            "hydrafusion.fusion.aiu",
            {**base, "final_model": final_model},
            (data.get("totalNanoAiu") or 0) / 1e9,
            unit="{AIU}",
        )
        COUNTERS.add(
            "hydrafusion.fusion.uncommitted_aiu",
            base,
            uncommitted_aiu,
            unit="{AIU}",
        )
        COUNTERS.add(
            "hydrafusion.fusion.duration",
            base,
            (data.get("durationMs") or 0) / 1000.0,
            unit="s",
        )
        if self.routing_latency_ms is not None:
            COUNTERS.add(
                "hydrafusion.routing.latency", base, self.routing_latency_ms / 1000.0, unit="s"
            )

        for phase in self.phases:
            pdata = phase["data"]
            usage = pdata.get("usage") or {}
            labels = {
                "session": self.session,
                "pattern": pattern,
                "model": pdata.get("model") or "unknown",
                "kind": pdata.get("phaseKind") or "unknown",
                "role": pdata.get("role") or "unknown",
            }
            COUNTERS.add(
                "hydrafusion.phase.count",
                {**labels, "status": pdata.get("status"), "verdict": pdata.get("verdict") or "none"},
                1,
            )
            COUNTERS.add(
                "hydrafusion.phase.aiu",
                {
                    **labels,
                    "verdict": pdata.get("verdict") or "none",
                    "committed": str(pdata.get("phaseId") == committed_id).lower(),
                },
                (usage.get("totalNanoAiu") or 0) / 1e9,
                unit="{AIU}",
            )
            COUNTERS.add(
                "hydrafusion.phase.duration",
                labels,
                (pdata.get("durationMs") or 0) / 1000.0,
                unit="s",
            )
            for token_type, field in (
                ("input", "inputTokens"),
                ("output", "outputTokens"),
                ("cached", "cachedTokens"),
                ("cache_write", "cacheWriteTokens"),
            ):
                COUNTERS.add(
                    "hydrafusion.tokens",
                    {**labels, "type": token_type},
                    usage.get(field) or 0,
                    unit="{token}",
                )


# --- tailing ---------------------------------------------------------------

class SessionTail:
    def __init__(self, path: Path, session: str, start_at_end: bool) -> None:
        self.path = path
        self.session = session
        self.offset = path.stat().st_size if start_at_end else 0
        self.buffer = ""
        self.open_fusions: dict[str, Fusion] = {}

    def read(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.offset:  # file rotated or rewritten
            self.offset = 0
        if size == self.offset:
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            line = line.strip()
            if line:
                self.handle(line)

    def handle(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = event.get("type", "")
        if not kind.endswith(("fusion_resolved", "fusion_phase_completed",
                              "fusion_handoff", "fusion_completed")):
            return
        data = event.get("data") or {}
        fusion_id = data.get("fusionId")
        if not fusion_id:
            return

        if kind.endswith("fusion_resolved"):
            self.open_fusions[fusion_id] = Fusion(self.session, data)
            return

        fusion = self.open_fusions.get(fusion_id)
        if fusion is None:
            # Resolution happened before we attached; recover with what we have.
            fusion = Fusion(self.session, {"fusionId": fusion_id})
            self.open_fusions[fusion_id] = fusion

        if kind.endswith("fusion_phase_completed"):
            fusion.phases.append(event)
        elif kind.endswith("fusion_handoff"):
            fusion.handoffs.append(event)
        elif kind.endswith("fusion_completed"):
            fusion.complete(event)
            self.open_fusions.pop(fusion_id, None)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    log.info("state dir %s, endpoint %s, backfill=%s", STATE_DIR, ENDPOINT, BACKFILL)
    if not STATE_DIR.is_dir():
        log.error("session state directory not found; is the volume mounted?")
        return 1

    tails: dict[Path, SessionTail] = {}
    first_pass = True
    while True:
        for events in sorted(STATE_DIR.glob("*/events.jsonl")):
            if events not in tails:
                start_at_end = first_pass and not BACKFILL
                try:
                    tails[events] = SessionTail(events, events.parent.name, start_at_end)
                except OSError:
                    continue
                if not start_at_end:
                    log.info("reading %s", events.parent.name)
            tails[events].read()
        first_pass = False
        COUNTERS.flush()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
