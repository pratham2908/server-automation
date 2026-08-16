"""Register (or update) the content app a channel pulls its finished videos from.

Shared secrets never travel through the API, so this is the only way a source
gets created. Run it from the automation-server directory with the venv active:

    python3 scripts/seed_video_source.py \
        --channel-id officialgeoranking \
        --name "GeoRank renderer" \
        --base-url https://georank-server-1030625682382.us-central1.run.app \
        --api-key '<shared secret>' \
        --test

Re-running with the same --channel-id and --name updates that source in place
rather than creating a duplicate. --test calls the app's list endpoint first and
refuses to write anything if it does not answer.

Defaults match the standard export contract; override --list-path / --detail-path
only for an app that deviates from it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import connect_db  # noqa: E402
from app.models.video_source import VideoSource  # noqa: E402
from app.services.video_source_service import (  # noqa: E402
    auth_headers,
    describe_http_error,
    normalise_video,
)
from app.timezone import now_ist  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Register a channel's content app as a video source")
    p.add_argument("--channel-id", required=True)
    p.add_argument("--name", required=True, help="Display label for this source")
    p.add_argument("--base-url", required=True, help="App origin, e.g. https://xyz.run.app")
    p.add_argument("--api-key", required=True, help="Shared secret issued by the app")
    p.add_argument("--auth-style", default="bearer", choices=["bearer", "api_key_header"])
    p.add_argument("--list-path", default="/api/ext/videos")
    p.add_argument("--detail-path", default="/api/ext/videos/{id}")
    p.add_argument("--disabled", action="store_true", help="Register but hide from the import UI")
    p.add_argument("--test", action="store_true", help="Call the app before writing")
    return p.parse_args()


async def probe(source: VideoSource) -> bool:
    """Call the app's list endpoint and print what came back."""
    import httpx

    url = f"{source.base_url}{source.list_path}"
    print(f"Testing {url} …")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params={"limit": 3},
                headers=auth_headers(source.model_dump()),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        print(f"  FAILED: {describe_http_error(exc)}")
        return False

    videos = data.get("videos", [])
    print(f"  OK — {len(videos)} video(s) in the first page, url_ttl={data.get('urlTtlSeconds')}s")
    for raw in videos[:3]:
        v = normalise_video(raw)
        secs = (v.duration_ms or 0) // 1000
        print(f"    [{v.status}] {v.title}  ({secs // 60}:{secs % 60:02d})  id={v.id}")
    return True


async def main() -> int:
    args = parse_args()
    settings = get_settings()

    try:
        source = VideoSource(
            source_id=str(uuid.uuid4()),
            channel_id=args.channel_id,
            name=args.name,
            base_url=args.base_url,
            list_path=args.list_path,
            detail_path=args.detail_path,
            api_key=args.api_key,
            auth_style=args.auth_style,
            enabled=not args.disabled,
        )
    except ValueError as exc:
        print(f"Invalid source config: {exc}")
        return 1

    if args.test and not await probe(source):
        print("Refusing to write a source that does not answer. Fix the URL or secret and retry.")
        return 1

    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    channel = await db.channels.find_one({"channel_id": args.channel_id})
    if not channel:
        print(f"No channel '{args.channel_id}' exists — refusing to create an orphaned source.")
        return 1

    doc = source.model_dump()
    existing = await db.video_sources.find_one({"channel_id": args.channel_id, "name": args.name})

    if existing:
        doc.pop("source_id")
        doc.pop("created_at")
        doc["updated_at"] = now_ist()
        await db.video_sources.update_one({"_id": existing["_id"]}, {"$set": doc})
        print(f"Updated source '{args.name}' ({existing['source_id']}) for channel {args.channel_id}")
    else:
        await db.video_sources.insert_one(doc)
        print(f"Created source '{args.name}' ({doc['source_id']}) for channel {args.channel_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
