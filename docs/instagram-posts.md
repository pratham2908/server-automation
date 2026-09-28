# Instagram posts — image, carousel, story

Contract between `automation-server` (this repo) and `analyzer` (the UI). Both
sides are built against this file; if code and this file disagree, one of them
is a bug.

Until this change the system could only publish **reels**. Every publish path
hardcoded `media_type=REELS`, the data model was one `.mp4` per `videos` record,
and Instagram sync filtered to `VIDEO`/`REEL`. Posts are a different shape — many
ordered assets, one caption — so they get their own collection, routes and worker
rather than being forced through the `videos` pipeline.

## What Instagram's API allows (verified against Meta's docs, Sep 2026)

| | API | notes |
|---|---|---|
| Single image | `media_type` omitted, `image_url` | **JPEG only**, ≤ 8 MB, width 320–1440, aspect 4:5 … 1.91:1 |
| Carousel | children with `is_carousel_item=true`, parent `media_type=CAROUSEL`, `children` | **2–10** items, images and/or videos; **every slide is cropped to the first slide's aspect ratio** |
| Story | `media_type=STORIES`, `image_url` or `video_url` | no caption |
| Caption | on the single/parent container only | ≤ 2200 chars, ≤ 30 hashtags |
| `alt_text` | image containers only | not reels, not stories |
| Per-slide captions | **impossible** | carousel children have no caption parameter |
| Music / song | **impossible** | no audio parameter exists; only `audio_name` on reels, which renames original audio |
| Native scheduling | **impossible** | no schedule parameter; **containers expire after 24 h** |
| Rate limit | 100 API-published posts / 24 h | a carousel counts as one; `GET {ig-user}/content_publishing_limit` |
| Login types | both Facebook Login and Instagram Login | resumable upload is Facebook-Login-only; images are URL-only anyway |

Container `status_code`: `IN_PROGRESS`, `FINISHED`, `ERROR`, `EXPIRED`, `PUBLISHED`.

Media URLs must be publicly fetchable: we hand Instagram presigned R2 GET URLs.
R2 CORS already allows `GET` and `PUT` from the analyzer's origins, so the browser
can upload slides directly and the phone handoff page can fetch them as blobs.

## Consequences for the design

- **Scheduling stays ours.** No native scheduling and a 24 h container lifetime
  mean a worker must create containers at publish time. The existing reel poller
  is the right model; posts get their own worker so a slow carousel cannot delay
  reels, and a 60 s tick plus a wake-up for "publish now".
- **Music is a handoff, not an API call.** See "Music: finish in the app".
- **Images are converted to JPEG in the browser** (canvas, max width 1440,
  quality ~0.9, ≤ 8 MB). The 1 GB Oracle box never decodes images.
- **Carousel crop is shown, not hidden.** The preview crops every slide to slide
  1's ratio and warns per slide, because Instagram will.

## Data model — `posts` collection

```text
post_id            str   uuid4 hex, unique index
channel_id         str   index (channel_id, status); index (status, scheduled_at)
kind               "image" | "carousel" | "story"
caption            str   "" for stories (ignored on publish)
first_comment      str | None     posted after publish, like reels
first_comment_status  None | "posted" | "failed"
music_mode         "none" | "in_app"
music_note         str | None     e.g. "Espresso — Sabrina Carpenter, from 0:32"
slides             [Slide]        ordered; order is the carousel order
status             see state machine
scheduled_at       datetime | None   stored aware; Mongo returns naive UTC -> read with assume_utc
published_at       datetime | None
instagram_media_id str | None
permalink          str | None
publish_state      { started_at, children: [{slide_id, container_id}], container_id } | None
attempts           int
last_error         str | None
handoff_sent_at    datetime | None
archived_from_status  str | None
created_at, updated_at

Slide:
slide_id           str   uuid4 hex
media_type         "image" | "video"
content_type       "image/jpeg" | "video/mp4"
r2_object_key      "{channel_id}/posts/{post_id}/{slide_id}.jpg|.mp4"
width, height      int | None
duration_seconds   float | None   videos
size_bytes         int | None
alt_text           str | None     images in image/carousel posts only
uploaded           bool           true once /complete confirmed the object exists
```

## Validation — `problems` block scheduling, `warnings` do not

Computed by a pure module on every read, returned on every `PostOut`.

Problems:
- `image`: exactly 1 slide, and it is an image. `story`: exactly 1 slide.
  `carousel`: 2–10 slides.
- Every slide uploaded.
- Image slide: `image/jpeg`, ≤ 8 MB; if dimensions known, width 320–1440 and (not
  for stories) aspect `width/height` within 0.8 … 1.91.
- Video slide: `video/mp4`, ≤ 300 MB; carousel and story videos 3–60 s when the
  duration is known.
- Caption ≤ 2200 chars and ≤ 30 hashtags (not checked for stories).
- `first_comment` ≤ 2200 chars.

`music_note` is optional even with `music_mode="in_app"`: it is a reminder for the
person finishing the post, not something Instagram receives.

Warnings:
- Carousel slide N's aspect differs from slide 1's by > 2 % → "Slide N will be
  cropped to match slide 1".
- Story slide not ~9:16 → "Stories are 9:16; this will be letterboxed".
- Image narrower than 1080 px → "may look soft on Instagram".
- Caption on a story → "Stories have no caption; it won't be posted".

## State machine

```text
draft ──schedule / publish-now──▶ scheduled ──worker, music none──▶ publishing ──▶ published
  ▲            ◀──unschedule──       │                                  │
  │                                  │ worker, music in_app,            └──▶ failed ──retry──▶ scheduled
  │                                  │ at scheduled_at − 30 min
  │                                  ▼
  └──────────unschedule──────── awaiting_manual ──mark-published / auto-detect──▶ published

archive: any state except publishing  → archived (archived_from_status kept)
restore: archived → archived_from_status; a restored "scheduled" whose time passed becomes "draft"
delete:  only draft, failed, archived (removes the R2 objects too)
edit:    caption / first_comment / music: draft, scheduled, failed, awaiting_manual
         slides / kind:                   draft, scheduled, failed
         anything else → 409
```

Editing a `scheduled` post re-validates; if it now has problems the edit is
refused (400) rather than leaving a scheduled post that cannot publish.

## HTTP API

All under `/api/v1/channels/{channel_id}/posts`, all `Depends(verify_api_key)`.
Datetimes out are IST ISO strings (`to_ist_iso`), like the videos routes. A
`scheduled_at` in may carry an offset; a naive one is IST, as for videos.
Errors are `HTTPException` with a string `detail`. The channel must exist and be
`platform == "instagram"`, else 400.

| method | path | body | returns |
|---|---|---|---|
| GET | `` | `?status=` one status, or omitted = everything except archived | `{"posts": [PostOut]}` newest-updated first |
| POST | `` | `PostCreate{kind, caption="", first_comment=None, music_mode="none", music_note=None}` | `PostOut` (201) |
| GET | `/publishing-limit` | | `{"quota_total", "quota_usage", "quota_duration_seconds"}` |
| GET | `/instagram-feed` | `?limit=24&after=<cursor>&include_reels=false` | `{"items": [FeedItem], "next_cursor"}` |
| GET | `/instagram-feed/{media_id}/insights` | | `{"media_id", "metrics": {name: int}, "unavailable": [name]}` |
| GET | `/{post_id}` | | `PostOut` |
| PATCH | `/{post_id}` | `PostUpdate{kind?, caption?, first_comment?, music_mode?, music_note?, slide_order?: [slide_id], alt_texts?: {slide_id: str}}` | `PostOut` |
| DELETE | `/{post_id}` | | `{"deleted": true}` |
| POST | `/{post_id}/slides` | `SlideCreate{media_type, content_type, size_bytes, width?, height?, duration_seconds?}` | `{"slide": SlideOut, "upload_url", "upload_headers": {"Content-Type": ...}}` |
| POST | `/{post_id}/slides/{slide_id}/complete` | | `PostOut` |
| DELETE | `/{post_id}/slides/{slide_id}` | | `PostOut` |
| POST | `/{post_id}/schedule` | `{"scheduled_at": ISO}` must be in the future | `PostOut` |
| POST | `/{post_id}/unschedule` | | `PostOut` |
| POST | `/{post_id}/publish-now` | | `PostOut` — sets `scheduled_at=now`, wakes the worker. For `in_app` it means "hand off now". |
| POST | `/{post_id}/retry` | | `PostOut` — failed → scheduled now |
| POST | `/{post_id}/mark-published` | `{"permalink"?: str}` | `PostOut` |
| POST | `/{post_id}/archive` | | `PostOut` |
| POST | `/{post_id}/restore` | | `PostOut` |
| GET | `/{post_id}/handoff` | | `HandoffOut` |

Register the fixed paths (`/publishing-limit`, `/instagram-feed…`) before
`/{post_id}` so they are not read as post ids.

```jsonc
// PostOut
{
  "post_id": "…", "channel_id": "…",
  "kind": "image" | "carousel" | "story",
  "status": "draft" | "scheduled" | "publishing" | "published" | "failed" | "awaiting_manual" | "archived",
  "caption": "…", "first_comment": null, "first_comment_status": null,
  "music_mode": "none" | "in_app", "music_note": null,
  "slides": [SlideOut],
  "scheduled_at": "2026-09-28T19:00:00+05:30" | null,
  "published_at": null, "instagram_media_id": null, "permalink": null,
  "last_error": null, "attempts": 0, "handoff_sent_at": null,
  "problems": ["…"], "warnings": ["…"],
  "created_at": "…", "updated_at": "…"
}
// SlideOut
{
  "slide_id": "…", "media_type": "image" | "video", "content_type": "image/jpeg" | "video/mp4",
  "width": 1080, "height": 1350, "duration_seconds": null, "size_bytes": 412345,
  "alt_text": null, "uploaded": true,
  "preview_url": "https://…presigned GET, 1 h…" | null   // null until uploaded
}
// FeedItem — live from Instagram, feed posts by default (reels excluded)
{
  "instagram_media_id": "…",
  "media_type": "IMAGE" | "VIDEO" | "CAROUSEL_ALBUM",
  "media_product_type": "FEED" | "REELS" | "STORY" | null,
  "caption": "…", "permalink": "…", "timestamp": "IST ISO",
  "thumbnail_url": "…" | null,            // media_url for images, thumbnail_url for video
  "like_count": 0, "comments_count": 0,
  "children": [{"media_type": "IMAGE" | "VIDEO", "url": "…"}],
  "post_id": "…" | null                  // ours, when we published it
}
// HandoffOut
{
  "post_id", "channel_id", "kind", "caption", "first_comment", "music_note",
  "status", "scheduled_at", "permalink",
  "slides": [{"slide_id", "media_type", "content_type", "filename", "download_url"}]  // presigned GET, 24 h
}
```

Upload flow (browser): `POST /slides` → `PUT upload_url` with exactly
`upload_headers` → `POST /slides/{id}/complete`. The server refuses to mint a URL
for a disallowed content type or oversized file, and `/complete` checks the object
really exists in R2 before setting `uploaded`.

## Worker — `run_post_publisher`

Its own background task (registered in `main.py` like the others), a 60 s tick and
an `asyncio.Event` that `publish-now` / `retry` set to wake it early. Every
Instagram call goes through `asyncio.to_thread`; nothing blocks the event loop.

Each tick:

1. **Hand-offs.** `music_mode="in_app"`, `status="scheduled"`,
   `scheduled_at − 30 min <= now` → email the owner a link to the handoff page,
   set `awaiting_manual` and `handoff_sent_at`. These posts are never auto-published.
2. **Auto-detect manual posts.** For channels with `awaiting_manual` posts, read
   the most recent ~25 media once and match by normalised caption (lowercase,
   collapsed whitespace, first 100 chars), timestamp after `handoff_sent_at − 2 h`,
   and not already linked to another post → `published` with media id + permalink.
3. **Publish due posts** (`music_mode="none"`, `scheduled` with `scheduled_at <= now`,
   or already `publishing`). Claim atomically (`scheduled → publishing`). Then
   advance a resumable state machine persisted in `publish_state`, so a restart or a
   slow video resumes rather than duplicates:
   - create any missing containers (carousel: one child per slide; image/story: one)
     from presigned GET URLs (6 h TTL);
   - read their `status_code`; `ERROR`/`EXPIRED` → `failed`; any `IN_PROGRESS` → stop,
     continue next tick;
   - carousel: once every child is `FINISHED`, create the parent, then wait for it;
   - `FINISHED` → `media_publish` → store media id, fetch `permalink`, `published`,
     post the first comment if set (record `first_comment_status`).
   - Paused channel → leave it `scheduled`.
   - Errors other than a container `ERROR`/`EXPIRED`: `attempts += 1`, keep the state,
     retry next tick; at 5 attempts → `failed` with `last_error`, logged through the
     error service like reels.
   - Still `publishing` 2 h after `started_at` → `failed` ("Instagram never finished
     processing"). Containers older than 23 h are discarded and recreated.

## Music: finish in the app

The API cannot add a song, and cannot schedule natively — but the Instagram app
can do both. So a post with `music_mode="in_app"` is prepared here and **finished
on the phone**:

1. At `scheduled_at − 30 min` the owner gets an email: "Time to post", the song
   note, and a link to `{ANALYZER_PUBLIC_URL}/handoff/{channel_id}/{post_id}`.
2. The handoff page (mobile-first, same login as the app) shows the slides and:
   **Copy caption** · **Share to Instagram** (Web Share API with the slide files,
   which opens Instagram's composer on phones that support it) · **Save images**
   fallback · the song note · **I've posted it**.
3. In the Instagram app the owner adds the song — and can use Instagram's own
   scheduler if they want it to go out later.
4. We detect the post by caption on the next ticks, or the owner taps **I've posted it**.

Best effort by nature: whether the share sheet hands several files to Instagram
depends on the phone. "Save images" always works.

## Also in this change

- `r2.generate_presigned_put_url` takes a `content_type` (default `video/mp4`, so
  existing callers are unchanged).
- The storage purge must not delete slides of a post that is not yet `published`
  or `archived` — they live under `{channel_id}/posts/`.
- The reel publisher's `publish_reel_from_url` call moves into `asyncio.to_thread`:
  it polls with `time.sleep` for up to ~400 s and was blocking every request while
  a reel published.
- The owner-email lookup used by the daily summary is shared, not copied.
- New setting `ANALYZER_PUBLIC_URL` (default `https://youtube-analyzer-p.netlify.app`).

## Not in this change

- **Slideshow reel with a baked-in track** — the only fully automatic way to put
  music under still images (rendered as a reel, with audio you own). Deferred: the
  server has 956 MB of RAM, and ffmpeg encoding alongside the API is an OOM risk.
- Collaborators, location, user/product tags.
- Deleting a published post on Instagram (Facebook Login only, as for reels).
