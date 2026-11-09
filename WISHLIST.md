# Wishlist

Instagram and YouTube features we want but do not have today, either because the
platform API does not offer them or because we have not built them yet. Look here
before starting a platform feature, and add to it when a "can we do X?" turns out to
be no.

Written 9 Nov 2026, corrected the same day against our own probe notes (6 to 31 Oct 2026). "API" means the Instagram Graph API (both login types) and the
YouTube Data API v3. Entries marked *(unverified)* come from memory and should be
checked against the platform docs before anyone plans work around them.

## The list

| # | Feature | Instagram | YouTube | What we do instead |
|---|---------|-----------|---------|--------------------|
| 1 | Pin a comment | No API. Probed live on both logins (30 Oct 2026): no pin field to read, and the comment update call accepts only `hide`. The unofficial in-app call (`pin_comment`) was never tried; see below. | No API. Studio only. | Pin by hand in the app. |
| 2 | Add a song to a carousel | Carousels and images: refused. The audio parameter (`audio_configuration`, a JSON object) is accepted on reels only. Even there it takes an audio asset id or an original-audio name, and the call that searches for audio assets was never found (probed 6 Oct 2026). | n/a (Shorts music is Studio/app only) | Publish the carousel, then add the song in the Instagram app. Reels get their song added in the app at publish time. |
| 3 | Hide like counts | **The parameter exists, but our app is refused.** `like_and_view_counts_disabled` is a real, validated parameter on reel creation, but any value (true or false) returns "Unknown method" on both logins, the same gate as scheduled publishing (probed 31 Oct 2026). Nothing can read the setting back, and nothing sets it after publishing. Carousels not tested. In the app it is per post, reels included *(unverified on a real reel)*. | No API. `status.publicStatsViewable` is the public stats page, not the like count. | Set by hand in the app. |
| 4 | Archive a post (hide from profile, restorable) | No API. The only API action is permanent delete (Facebook Login only, needs `instagram_manage_contents`). | No archive. Unlisted or private via `set_video_privacy` is the closest, and we have it. | Our own "archived" status is a tombstone in our database. It does not archive anything on Instagram. |
| 5 | Edit a comment | No API. Delete and re-post is the only API route. The app reportedly lets people edit comments now *(unverified)*. | The API can: `comments.update` edits comments written by the connected channel. Not built. | Delete and repost on Instagram. |
| 6 | Edit a caption | **Maybe, on Facebook Login only, untested.** On Instagram Login `POST /{media-id}` takes only `comment_enabled`. On Facebook Login the error for an empty call says it requires one of `caption`, `comment_enabled`, `status` (probed 30 Oct 2026). Nobody has tried it because it changes a live caption. A same-text edit on a throwaway post would settle it. | Works: `PATCH /videos/{video_id}` (title, description, tags, publish time). Built. | On Instagram, edit in the app, or accept that the caption is fixed at publish. Deleting and republishing loses reach and is not a fix. |

### Notes on the list

- **Pin comment (1).** Neither platform has an official call. Instagram's own app uses a
  private endpoint; the idea was to call it from a signed-in browser tab. Untested, and
  against Instagram's terms, so treat it as a risk before building.
- **Comment replies we already post** can be hidden (`hide_comment`) and deleted, which
  covers moderation; it does not cover pinning or editing.
- **Caption edit (6)** is the one with a real cost: a typo in an Instagram caption cannot
  be fixed by us today. If the Facebook Login probe works it becomes a small feature, so
  it is the first thing on this list worth testing.

## What the API can and cannot change on a published post

### Instagram

Can change through the API after publishing:

- Comments on or off for the post (`comment_enabled`). The call has been seen returning
  success without taking effect, so read the setting back to confirm.
- Delete the post (Facebook Login channels only).

Cannot change through the API, only in the app:

- Caption on Instagram Login; on Facebook Login it may be possible, see row 6
- Alt text
- Tagged people, location, collaborators
- Pin or edit a comment
- Archive or restore, pin to profile grid
- Hide like count after publishing, turn off sharing or remixing
- Cover frame, add or remove or reorder carousel slides
- Music from the library

Set only when creating the post (not after): caption, user tags, location, alt text on
images, cover frame and thumbnail offset on reels, share-to-feed, original-audio name,
trial reel settings, and collaborators. Two creation-time options exist but our app is not
allowed to use them: scheduled publishing (`scheduled_publish_time`) and hiding like counts. We do not send collaborators yet (see the
"Collaborators field gap" note); the API does accept them.

### YouTube

Can change through the API after publishing (`videos.update`, we use it): title,
description, tags, category, privacy, scheduled publish time, "made for kids", the
altered/synthetic-content disclosure *(unverified)*; thumbnails (`thumbnails.set`);
playlists; captions; delete the video.

Studio only: pin or heart a comment, hide the like count, end screens and cards,
monetisation and ad placement, the audio library and the in-Studio editor (trim, blur),
community posts, channel branding.

## Everything the apps can do that the APIs cannot

### Instagram app only

- Edit caption, alt text, tags, location, collaborators on a live post
- Pin a comment; edit a comment *(reported)*
- Archive and unarchive; pin a post to the profile grid
- Hide like count (at publish, the API parameter is blocked for us); turn off reposts, remixes, sharing
- Add library music to carousels, images and reels; use Reels audio remix and templates
- Story stickers (polls, questions, links, countdowns, "Add yours"), Close Friends
  audience, add a story to a Highlight
- Broadcast channels, Notes, Live, collab invites after publishing
- Boost a post (the Marketing API is a separate product)
- Profile edits (name, bio, picture, links), hidden-words filters, restrict and block
- Native scheduling: the API has a `scheduled_publish_time` parameter but only
  allow-listed apps may use it (ours gets "User must be on whitelist"), so our own
  scheduler holds the post and publishes at the time. Posts scheduled in the Instagram app
  are invisible to the API
- The "AI info" label: not settable through the API as far as we know *(unverified)*

### YouTube Studio only

- Pin or heart a comment
- Hide the like count
- End screens and cards
- Monetisation, ad breaks, paid-promotion settings
- Audio library swaps and the video editor
- Community posts, channel layout and branding, Shorts remix settings

## Adding to this list

One row in the table: what we want, what each platform allows, what we do today. If the
answer is "the API can, we have not built it", say so in the platform's cell, as in rows
5 and 6.
