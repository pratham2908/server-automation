# Channel Pause — Design

**Date:** 2026-07-24
**Status:** Approved

## Problem

Every per-channel background cron selects channels with an unfiltered
`db.channels.find()`. There is no way to stop the system working on a single
channel short of deleting it or revoking its credentials.

This surfaced with a disconnected channel that had no YouTube token: the
sync cron retried it every run and filed 72 identical error-queue entries.
Silencing that error stopped the noise but not the pointless work.

## Scope

A single master switch per channel that stops background work only.

**Paused stops** — all five background operations:

| Cron | Enforcement point |
| --- | --- |
| `sync_analysis_cron` (video sync + AI analysis) | `db.channels.find()` |
| `growth_cron` (growth snapshots) | `db.channels.find()` |
| `comment_analysis_cron` (sentiment) | `db.channels.find()` |
| `comment_reply_cron` (auto-replies) | `db.channels.find()` |
| `auto_publisher` (publishes due videos) | per-video channel lookup |

**Paused does not stop** manual and API-triggered actions. "Sync now",
"Publish now", uploads, and scheduling all keep working. Pause means *stop
acting on your own*, not *freeze the channel* — you can still deliberately
work on a paused channel.

Scheduled videos stay scheduled. Nothing is dropped or rescheduled; they
publish when the channel is resumed.

### Rejected alternatives

**Per-operation toggles.** Six independent switches (sync, analysis, growth,
comments, replies, publish) were considered and rejected as unnecessary for
the actual need. A master switch can gain granularity later without breaking
the field; the reverse is harder.

**Blocking manual actions too.** Rejected: it would make a paused channel
impossible to work on without unpausing first.

**Uniform in-loop guards instead of a query filter.** Rejected as the primary
mechanism: it loads every channel only to discard some, and a future cron
that forgets the guard silently processes paused channels. Filtering at the
query means a new cron using the shared helper gets the behaviour for free.

## Data

One field on the channel document:

```python
paused: bool = Field(
    False,
    description="When true, background crons skip this channel entirely",
)
```

Defaults to `False`. Existing documents predate the field and therefore lack
it, so all matching is absence-tolerant — no migration or backfill.

## Components

Two helpers in `app/database.py`, alongside the existing
`get_channel_platform`, so "what counts as active" is defined once:

- `not_paused_query() -> dict` returns `{"paused": {"$ne": True}}`.
  `$ne` rather than `False` so documents written before the field existed
  still count as active.
- `is_channel_paused(channel: dict) -> bool` for callers that already hold
  the document — `auto_publisher` looks its channel up per due video, so a
  query filter does not apply there.

Each skip logs at info level naming the channel, so a paused channel reads
as deliberately idle rather than mysteriously broken.

## API

No new endpoints. `PATCH /api/v1/channels/{channel_id}` already applies
partial updates, so `{"paused": true}` works as-is. Dedicated
`pause`/`resume` endpoints would be a second way to do the same thing.

## UI

`analyzer` gets a pause toggle on the channel settings page and a "Paused"
badge on the channel, so a stopped channel is visibly stopped. Without this
the feature is not reachable from the product.

## Testing

- `not_paused_query()` matches channels where `paused` is absent or `False`,
  excludes `True`
- `is_channel_paused()` across all three document shapes
- Per cron: a paused channel is skipped and an active one is not
- `auto_publisher` skips a due video whose channel is paused, and leaves the
  video scheduled rather than failing it

## Risks

The field is absent on every existing document. Any check written as
`channel["paused"]` or `{"paused": False}` would wrongly treat every current
channel as paused and halt the entire system. Both helpers are absence-
tolerant, and tests cover the absent case specifically.
