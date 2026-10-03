# Today's video by callback — contract for source apps (GeoRank)

The automation server asks `GET /api/ext/videos/today` about an hour before a
slot. Until now, a `generating` answer meant we asked again every ~5 minutes until
the slot's time. With a callback, the app tells us once, as soon as the video is
done or has failed, and we stop asking.

Nothing here is required: an app that ignores the new headers keeps working
exactly as before (we keep polling it).

## 1. The ask carries a callback offer

Every `/today` request now includes two extra **headers**:

| Header | Value |
|---|---|
| `X-Callback-Url` | `https://automation-server.tryalgoviz.com/api/v1/source-callbacks/{callback_id}` |
| `X-Callback-Token` | a random one-time password (43 URL-safe characters) |

The token is only ever in a header, never in a URL, so it does not end up in
access logs. We store only its SHA-256 hash.

## 2. The app answers as today, plus one field

- **`200 ready`** → unchanged. Ignore the callback headers.
- **`503 unavailable`** → unchanged. Ignore the callback headers.
- **`202 generating`** → if you will call back, store the URL and token against
  the work now in flight and add **`"callbackAccepted": true`** to the body:

```json
{ "status": "generating", "source": "format", "retryAfterSeconds": 180, "callbackAccepted": true }
```

Only the JSON literal `true` counts. Without it we assume you will not call and
keep polling.

Once you accept, **we stop asking**, except for one final ask at the slot's time
(a safety net for a callback that got lost). Our asks used to be what retried a
failed render on your side. That nudge is gone, so **retry failed renders
yourself**, or report the failure (below).

## 3. When the video is settled, POST the callback URL

```
POST {X-Callback-Url}
Authorization: Bearer {X-Callback-Token}
Content-Type: application/json
```

**Ready** — the same shape as a `/today` 200 body. You can post exactly that
object; extra fields are ignored:

```json
{ "status": "ready", "video": { "id": "<renderId>", "title": "optional" }, "reason": "optional, as on /today" }
```

`video.id` must be the id `/api/ext/videos/{renderId}` serves, because we import
through that route as usual.

**Failed** — you will not produce a video for this ask:

```json
{ "status": "failed", "error": "Render timed out 9 times: waiting for the page to render the React component" }
```

The error text goes into the daily email, so make it say what went wrong.

## 4. Our answers

| Code | Meaning | Retry? |
|---|---|---|
| `200 {"received": true, "action": …}` | Taken. `action` is `importing`, `failed_recorded`, or `polling` (we could not import the video you named, e.g. we already hold it, so we went back to asking). | No |
| `401` | Missing or wrong token. | No |
| `404` | Unknown callback id. | No |
| `409` | Already delivered. The token is single-use. | No |
| `410` | No longer wanted: the slot passed, a final ask already found the video, or the day is over. The video stays in your catalogue and a later slot can take it. | No |
| `422` | Body didn't match the schema above. The token is **not** used up, so fix the body and send it again. | Yes, fixed |
| `5xx` / network error | Our side had a problem. | Yes, with backoff |

## 5. Lifetime

- A callback is valid until **midnight IST after the slot's day**. After that it
  answers `410`, and a sweep marks any that never came as expired.
- If we ask again for the same slot (only at the deadline), that final ask
  carries **no** callback headers.
- An offer you did not accept is closed straight away. Posting to it gets `410`.
