# AI Call Cost Observability — Implementation Plan

**Goal:** After every Gemini API call, capture input/output token counts, compute cost using current Vertex AI pricing, persist a log record to MongoDB, and surface the data through a frontend dashboard showing per-model and per-task spend, daily trends, and a live call feed.

**Pricing basis:** Vertex AI standard tier (the app uses `genai.Client(vertexai=True, ...)`), fetched 2026-07-24.

---

## Current State

| What exists | Gap |
|-------------|-----|
| `metrics_service.record_ai_call(model, duration_ms, success)` | No token counts, no task label, no cost, in-memory only (last 20 calls) |
| `_generate()` and `_generate_with_video()` call `record_ai_call` | `response.usage_metadata` token counts are never read |
| `db.metrics_snapshots` — hourly snapshot of totals | No per-call log, no token breakdown, no cost |
| `analyze_thumbnail()` has its own inline Gemini loop | Same gaps as above |
| Backend-rendered `/dashboard` HTML page | No cost/token data surfaced |
| Next.js frontend — no observability section | Nothing to change yet |

---

## Gemini Pricing (Vertex AI Standard, 2026-07-24)

```python
GEMINI_PRICING = {
    # model_id → {input_per_1m, output_per_1m, input_per_1m_long?, output_per_1m_long?, context_threshold?}
    "gemini-2.5-pro": {
        "input_per_1m":       1.25,
        "output_per_1m":     10.00,
        "input_per_1m_long":  2.50,   # >200k context window
        "output_per_1m_long":15.00,
        "context_threshold": 200_000,
    },
    "gemini-2.5-flash": {
        "input_per_1m":  0.30,
        "output_per_1m": 2.50,
    },
    "gemini-2.5-flash-lite": {
        "input_per_1m":  0.10,
        "output_per_1m": 0.40,
    },
    # Preview — no published pricing; proxy as 2.5-flash
    "gemini-3-flash-preview": {
        "input_per_1m":  0.30,
        "output_per_1m": 2.50,
    },
}
```

For video/image input tokens (used in `_generate_with_video` and `analyze_thumbnail`): billed at the same per-token rate as text input for all 2.5-series models.

---

## Task Labels

All 12 `_generate()` / `_generate_with_video()` / inline call sites in `gemini.py` must be tagged:

| Method | Line(s) | Task label |
|--------|---------|------------|
| `analyze_videos` | 129 | `"channel_analysis"` |
| `analyze_single_video` | 159 | `"single_video_analysis"` |
| `cluster_video_topics` | 190 | `"topic_clustering"` |
| `generate_video_content` | 262 | `"content_generation"` |
| `analyze_video_retention` (video) | 760 | `"retention_analysis"` |
| `generate_platform_packaging` | 941 | `"platform_packaging"` |
| `classify_comment_sentiment` | 981 | `"comment_sentiment"` |
| `generate_comment_reply` | 1022 | `"comment_reply"` |
| `analyze_comments` | 1079 | `"comment_analysis"` |
| `generate_scorecard` | 1418 | `"scorecard_generation"` |
| `extract_video_intelligence` | 1576 | `"content_intelligence"` |
| `compare_content_patterns` | 1673 | `"pattern_comparison"` |
| `analyze_thumbnail` (inline loop) | ~1260–1307 | `"thumbnail_analysis"` |

---

## MongoDB Schema

**Collection:** `ai_call_logs`

```json
{
  "_id":          "<ObjectId>",
  "timestamp":    "<datetime UTC>",
  "task":         "retention_analysis",
  "model":        "gemini-2.5-pro",
  "input_tokens":  5420,
  "output_tokens": 1200,
  "total_tokens":  6620,
  "cost_usd":      0.018970,
  "duration_ms":   4231.5,
  "success":       true
}
```

**Indexes to create** (via `create_index` at startup or in a migration):

```python
await db.ai_call_logs.create_index([("timestamp", -1)])
await db.ai_call_logs.create_index([("task", 1), ("timestamp", -1)])
await db.ai_call_logs.create_index([("model", 1), ("timestamp", -1)])
```

---

## API Contract (pre-specified so Agent C can build the frontend in parallel with Agent B)

### `GET /api/v1/observability/ai-calls`

Auth: `X-API-Key: {global_key}` or `?api_key=`

Query params:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | — | Filter to one model |
| `task` | string | — | Filter to one task |
| `date_from` | ISO datetime | — | Inclusive lower bound |
| `date_to` | ISO datetime | — | Inclusive upper bound |
| `page` | int | 1 | 1-indexed |
| `limit` | int | 50 | Max 200 |

Response:
```json
{
  "calls": [
    {
      "timestamp":    "2026-07-24T12:00:00Z",
      "task":         "retention_analysis",
      "model":        "gemini-2.5-pro",
      "input_tokens":  5420,
      "output_tokens": 1200,
      "total_tokens":  6620,
      "cost_usd":      0.01897,
      "duration_ms":   4231.5,
      "success":       true
    }
  ],
  "total": 142,
  "page":  1,
  "limit": 50
}
```

---

### `GET /api/v1/observability/ai-costs/summary`

Auth: same as above

Query params:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 30 | Look-back window |

Response:
```json
{
  "total_cost_usd":       12.45,
  "total_calls":          842,
  "total_input_tokens":   14200000,
  "total_output_tokens":  3800000,
  "success_rate_pct":     97.2,
  "avg_cost_per_call":    0.01478,
  "by_model": [
    {
      "model":         "gemini-2.5-pro",
      "calls":         320,
      "cost_usd":      9.10,
      "input_tokens":  8200000,
      "output_tokens": 2100000
    }
  ],
  "by_task": [
    {
      "task":     "retention_analysis",
      "calls":    180,
      "cost_usd": 5.40
    }
  ],
  "daily": [
    { "date": "2026-07-24", "cost_usd": 0.85, "calls": 42 }
  ]
}
```

---

## Execution Plan

### Phase 1 — Agent A (runs alone, no dependencies)

**Scope:** backend plumbing — token capture, cost engine, MongoDB persistence

**Files to create:**

`automation-server/app/services/ai_call_logger.py`

```
- GEMINI_PRICING dict (as above)
- compute_cost(model, input_tokens, output_tokens) -> float
  - handles the 2.5-pro tiered pricing using context_threshold
  - falls back to 0.0 for unknown models with a warning log
- _bound_db: AsyncIOMotorDatabase | None = None  (same pattern as error_reporting.py)
- bind_ai_logger_db(db)  — call at lifespan startup, pass None at shutdown
- async def log_ai_call(task, model, input_tokens, output_tokens, duration_ms, success)
  - reads _bound_db; silently skips if None (e.g. during tests)
  - inserts one doc into db.ai_call_logs
  - wraps in try/except so a DB failure never kills the Gemini call path
```

**Files to modify:**

`automation-server/app/services/gemini.py`

```
_generate(prompt, specific_model=None, task="unknown"):
  - After successful response:
      input_tokens  = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
      output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
      metrics_service.record_ai_call(model, duration, True, task, input_tokens, output_tokens)
      asyncio.create_task(log_ai_call(task, model, input_tokens, output_tokens, duration, True))
  - On failure:
      metrics_service.record_ai_call(model, duration, False, task, 0, 0)
      asyncio.create_task(log_ai_call(task, model, 0, 0, duration, False))

_generate_with_video(video_path, prompt, task="retention_analysis"):
  - Same token capture + logging pattern as above

analyze_thumbnail (inline Gemini loop ~line 1260):
  - Same token capture + logging pattern, task="thumbnail_analysis"

All 13 call sites updated to pass task= (see Task Labels table above).
```

`automation-server/app/services/metrics.py`

```
record_ai_call(model, duration_ms, success, task="unknown", input_tokens=0, output_tokens=0):
  - compute cost_usd via compute_cost(model, input_tokens, output_tokens)
  - self.ai_total_cost_usd += cost_usd
  - self.ai_task_usage[task] = self.ai_task_usage.get(task, 0) + 1
  - self.ai_task_cost[task] = self.ai_task_cost.get(task, 0.0) + cost_usd
  - ai_last_calls deque entry gains: task, input_tokens, output_tokens, cost_usd

get_summary():
  - ai section gains: total_cost_usd, task_usage, task_cost fields

New fields on MetricsService.__init__:
  self.ai_total_cost_usd = 0.0
  self.ai_task_usage: dict[str, int] = {}
  self.ai_task_cost: dict[str, float] = {}
```

`automation-server/app/main.py`

```
Lifespan startup: 
  from app.services.ai_call_logger import bind_ai_logger_db
  bind_ai_logger_db(db)

Lifespan shutdown:
  bind_ai_logger_db(None)
```

---

### Phase 2 — Agent B + Agent C (run in parallel after Phase 1 is merged)

These two agents are independent of each other. Agent C uses the API contract above and can mock the endpoints while developing.

---

### Phase 2A — Agent B: Query API endpoints

**Files to modify:** `automation-server/app/routers/observability.py`

Add two new routes (auth via existing `verify_api_key` or `verify_api_key_flexible` — use whichever the existing dashboard uses):

```
GET /api/v1/observability/ai-calls
  - Build MongoDB filter from query params (model, task, date_from, date_to)
  - Sort by timestamp descending
  - Paginate: skip = (page-1)*limit, limit = min(limit, 200)
  - Get total count with count_documents()
  - Return {calls, total, page, limit}

GET /api/v1/observability/ai-costs/summary
  - Compute date_from = now - timedelta(days=days)
  - Use MongoDB aggregation pipeline:
      $match: {timestamp: {$gte: date_from}}
      $group: {_id: null, total_cost: $sum, total_calls: $count, ...}
  - Separate pipelines for by_model (group by model), by_task (group by task)
  - Daily breakdown: group by date (truncate timestamp to day), last `days` entries
  - Return full summary object
```

No new files needed; both routes go into `observability.py` alongside existing ones.

---

### Phase 2B — Agent C: Frontend AI Cost Dashboard

**Files to create:**

`analyzer/src/components/features/observability/AICostSection.tsx`

Layout (top-to-bottom):
1. **Summary bar** — 4 stat cards: Total Cost (30d), Total Calls (30d), Avg Cost/Call, Success Rate
2. **Breakdowns row** — two side-by-side panels:
   - By Model: horizontal bar chart + table (model | calls | cost | % of total)
   - By Task: horizontal bar chart + table (task | calls | cost | % of total)
3. **Daily trend** — line chart of cost_usd per day over the last 30 days
4. **Call log** — paginated table: timestamp | task | model | in tokens | out tokens | cost | latency | status

Use whatever chart library is already in the codebase. If none, use plain CSS bar charts (percentage-width divs) — do NOT add a chart library dependency.

`analyzer/src/lib/api/observability.ts` (new or add to existing)

```typescript
export async function getAICalls(params: {
  model?: string;
  task?: string;
  date_from?: string;
  date_to?: string;
  page?: number;
  limit?: number;
}): Promise<AiCallsResponse>

export async function getAICostSummary(days?: number): Promise<AiCostSummary>
```

Wire `AICostSection` into the app's navigation. Add it wherever system/admin-level sections live (settings, dashboard, or a new "System" sidebar group).

---

## Dependency Graph

```
                 ┌─────────────────────────────────┐
                 │  Agent A — backend core          │
                 │  gemini.py + metrics.py +        │
                 │  ai_call_logger.py + main.py     │
                 └────────────────┬────────────────┘
                                  │ merged to main
              ┌───────────────────┼───────────────────┐
              │                                       │
    ┌─────────▼──────────┐              ┌─────────────▼──────┐
    │  Agent B           │              │  Agent C            │
    │  Query endpoints   │              │  Frontend dashboard │
    │  in observability  │              │  AICostSection.tsx  │
    │  .py               │              │  + API client       │
    └────────────────────┘              └─────────────────────┘
         can start as soon as A merges      uses API contract above;
                                            mock or stub during dev
```

---

## Acceptance Criteria

### Agent A
- Every successful Gemini call writes one doc to `ai_call_logs` within the same async task cycle
- `response.usage_metadata.prompt_token_count` and `candidates_token_count` are captured (fall back to 0 if the field is None/missing — some preview models may not return it)
- `cost_usd` is 0.0 for failed calls (no tokens billed on errors)
- A DB failure in `log_ai_call` does NOT propagate to the Gemini call — it must be caught and logged
- All 13 call sites pass a non-`"unknown"` task label
- `metrics_service.get_summary()["ai"]["total_cost_usd"]` is populated and accurate

### Agent B
- `GET /ai-calls` with no filters returns the 50 most recent calls
- `?model=gemini-2.5-flash` returns only that model's calls
- `?task=retention_analysis` returns only that task's calls
- `?date_from=...&date_to=...` bounds are inclusive
- `GET /ai-costs/summary?days=7` returns correct aggregation scoped to 7 days
- `by_model` and `by_task` arrays are sorted by `cost_usd` descending
- `daily` array has one entry per day in ascending date order

### Agent C
- Summary cards update on load with real data from `GET /ai-costs/summary`
- Model and task bar charts show relative widths proportional to cost
- Call log table is paginated; clicking next/prev loads the correct page
- Date filters on the call log wire through to the `date_from`/`date_to` query params
- No new npm dependencies added (use existing chart library or CSS-only bars)
- Works in both light and dark theme
