# Phase 0 spike: what the CLI actually emits

Verified against **GitHub Copilot CLI 1.0.84-2** on Windows, 2026-09-09.
Captured with `COPILOT_OTEL_FILE_EXPORTER_PATH`, no collector involved.

## The question

> Do HydraFusion legs appear as separate `chat` spans with distinct
> `gen_ai.response.model` values?

**No.** The CLI reports the turn, not the legs. The rest of this document is what it does
report, measured rather than assumed, and where the per-leg detail lives instead.

## What the spans contain

Span names are `{operation} {request_model}`, not `chat`:

| Span name | Count in a 7-round-trip turn | Duration |
|---|---|---|
| `invoke_agent` | 1 | 99.9 s |
| `chat hydrafusion` | 7 | 4-64 ms |
| `execute_tool <name>` | 10 | 0-14 ms |

In the runs observed here the `chat` span covers a few milliseconds, so it appears to
measure client-side request handling rather than the model round trip: a turn that used
13.66 AI credits over 101 seconds produced `chat` spans totalling under 150 ms. Confirm
this against your own CLI version before building a latency panel on those spans.

Attributes present on `chat hydrafusion`:

```
gen_ai.operation.name        = chat
gen_ai.provider.name         = github
gen_ai.request.model         = hydrafusion
gen_ai.conversation.id       = <session uuid>
gen_ai.request.stream        = true
gen_ai.response.finish_reasons
github.copilot.turn_id
github.copilot.interaction_id
gen_ai.tool.definitions      (large JSON blob of tool names)
```

Attributes the plan assumed, which are **not present in the span data**:

- `gen_ai.response.model` is absent. Spans carry `gen_ai.request.model=hydrafusion`, which
  is what the GenAI semantic conventions define that field to mean: the model the client
  requested. The conventions have no field for a router expanding one request into
  several model calls, so there is nothing for the CLI to populate here.
- `github.copilot.nano_aiu` is absent from spans. Cost is recorded per phase in the
  session log instead.
- `gen_ai.agent.name` is absent. Only `gen_ai.agent.id = github.copilot.default`
  appears, and only as a metric dimension.

## What the metrics contain

Six instruments, all histograms except the MCP counter:

| Metric | Dimensions |
|---|---|
| `gen_ai.client.operation.duration` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `gen_ai.invoke_agent.duration` | `gen_ai.agent.id`, `gen_ai.agent.version` |
| `gen_ai.invoke_agent.inference_calls` | none |
| `gen_ai.invoke_agent.tool_calls` | none |
| `github.copilot.agent.turn.count` | `gen_ai.operation.name` |
| `github.copilot.mcp.server.connection.count` | `outcome` |

No token metric, no cost metric, and `gen_ai.request.model` is always `hydrafusion`.

The OTel output accurately describes the turn. It records that a turn happened, how long
it took, how many inference and tool calls it made, and how the agent behaved. The
conventions have no vocabulary for recording that more than one model served the turn.

## Where the per-leg detail lives

`~/.copilot/session-state/<session-id>/events.jsonl`. Every fusion decision is recorded
there, fully structured. The file backs session resume and rewind; reading it for
observability is a second use, not its purpose.

### `session.fusion_resolved`: the routing decision

```json
{"pattern":"cascade","policy":"max","routeSource":"capi_plan",
 "primaryModel":"mai-code-1.1-flash","secondaryModel":"gpt-5.6-sol",
 "fallbackModel":"gpt-5.6-sol","followUpModel":"gpt-5.6-sol",
 "routingLatencyMs":218.05,"contractVersion":1,"planVersion":"1",
 "phasePlan":[{"kind":"primary","role":"solver","scope":"root","conditional":false}]}
```

### `assistant.fusion_phase_completed`: one event per leg, with cost

```json
{"phaseKind":"judge","role":"judge","model":"gpt-5.6-sol","status":"succeeded",
 "verdict":"reject","durationMs":4522,
 "usage":{"requestCount":1,"inputTokens":12500,"outputTokens":7,
          "cachedTokens":7680,"cacheWriteTokens":0,"totalNanoAiu":2249200000}}
```

`phaseKind` observed: `primary`, `judge`, `repair` in a `cascade`; `draft` and `critic` in
a `critique`. `role` observed: `solver`, `judge`, `critic`. Only `judge` phases carry a
`verdict`; `critic` phases do not.

### `session.fusion_handoff`: the escalation

Emitted between a rejecting judge and the repair phase.

### `session.fusion_completed`: the turn rollup

```json
{"pattern":"cascade","outcome":"completed","phaseCount":3,"requestCount":7,
 "finalSourceModel":"gpt-5.6-sol","degradedReason":null,
 "inputTokens":96926,"outputTokens":1439,"cachedTokens":81914,
 "cacheWriteTokens":14991,"totalNanoAiu":13658460000,"durationMs":101385}
```

## Fusion granularity

One fusion per **user prompt**, not per model round-trip. A single fusion covered
7 inference calls and 10 tool calls. So `phaseCount` counts fusion legs, while
`requestCount` counts inference calls across all legs. These are different
numbers and conflating them would produce a wrong dashboard.

## Observed runs

| Prompt | Pattern | Phases | Models | AIU | Wall |
|---|---|---|---|---|---|
| `Reply with exactly: SMOKE OK` | `single` | 1 | `gpt-5.6-sol` | 2.25 | 4.8 s |
| Off-by-one diagnosis in a rate limiter | `single` | 1 | `gpt-5.6-sol` | 13.66 | 101 s |
| Retry policy for a non-idempotent payments API | `cascade` | 3 | `mai-code-1.1-flash` → `gpt-5.6-sol` (judge, **reject**) → `gpt-5.6-sol` (repair) | 14.72 | 77 s |
| Mechanical rename across two files | `cascade` | 3 | `gpt-5.6-luna` → `gpt-5.6-sol` (judge, **reject**) → `gpt-5.6-sol` (repair) | 13.11 | 79 s |

The router did not match the plan's prediction on any non-trivial prompt. A hard debugging
task with a failing test routed to `single`; a two-file rename routed to `cascade`. Our
assumptions about difficulty and verifiability did not fit these runs.
`routeSource: capi_plan` says the decision is served remotely, so it can also be tuned
without a CLI release.

Over a wider run of 24 turns, three patterns and five models appeared: `single`,
`cascade`, and `critique`, using `gpt-5.6-sol`, `claude-opus-5`, `gpt-5.6-luna`,
`gpt-5.6-terra`, and `mai-code-1.1-flash`. See [FINDINGS.md](FINDINGS.md) for the
breakdown.

## Decision gate

The result follows the plan's "one span per turn" branch. The `events.jsonl` reader is the
dashboard's primary data source, while OTel spans contribute the turn skeleton, tool
timeline, and agent metrics.

Revised framing: the CLI emits conformant OpenTelemetry for the turn, and separately
records the per-leg detail in its session log for resume and rewind. Nothing is missing
from either file given its purpose; they have simply never been read together. This repo
joins them on `gen_ai.conversation.id`.

## Consequences for the build

1. A tailer for `events.jsonl` is mandatory and becomes the primary data source.
2. Cost panels read `usage.totalNanoAiu` from phase events. The double-counting
   caveat in the plan does not apply, since spans carry no cost. The real caveat is
   different: `fusion_completed.totalNanoAiu` is the turn total and
   `fusion_phase_completed.usage.totalNanoAiu` are its parts. Do not sum both.
3. `gen_ai.conversation.id` on spans equals the CLI session ID, which is also the
   `session-state` directory name. That is the join key between spans and events.
4. Panels keyed on `gen_ai_response_model` must be rebuilt on the phase model
   from events.

## Reproducing

```powershell
$env:COPILOT_OTEL_FILE_EXPORTER_PATH = "$PWD\otel.jsonl"
copilot -C .\sandbox -p "<task>" --model hydrafusion --allow-all-tools
```

`--model hydrafusion` was accepted directly in 1.0.84-2. At the time of this spike
the runs also set `COPILOT_CLI_ENABLED_FEATURE_FLAGS=HYDRAFUSION_ROLLOUT`, which was
then required for preview gating. That gating has since been lifted, so the flag is no
longer needed.
