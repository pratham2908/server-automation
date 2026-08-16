"""Register (or update) a channel's external video source.

Credentials never travel through the API, so this is the only way a source gets
created. Run it from the automation-server directory with the venv active:

    # Cloudflare R2
    python3 scripts/seed_video_source.py \
        --channel-id ch_abc \
        --name "Editor drop bucket" \
        --provider r2 \
        --bucket finished-reels \
        --endpoint-url https://<account_id>.r2.cloudflarestorage.com \
        --access-key-id AAA... \
        --secret-access-key BBB... \
        --prefix ready/

    # AWS S3
    python3 scripts/seed_video_source.py \
        --channel-id ch_abc \
        --name "Studio S3" \
        --provider s3 \
        --bucket studio-exports \
        --region ap-south-1 \
        --access-key-id AKIA... \
        --secret-access-key ...

Re-running with the same --channel-id and --name updates that source in place
rather than creating a duplicate. Pass --test to verify the credentials by
listing the bucket before writing anything.
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
from app.services.video_source_service import build_client  # noqa: E402
from app.timezone import now_ist  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Register a channel's external video source")
    p.add_argument("--channel-id", required=True)
    p.add_argument("--name", required=True, help="Display label for this source")
    p.add_argument("--provider", required=True, choices=["r2", "s3"])
    p.add_argument("--bucket", required=True)
    p.add_argument("--access-key-id", required=True)
    p.add_argument("--secret-access-key", required=True)
    p.add_argument("--endpoint-url", default=None, help="Required for R2")
    p.add_argument("--region", default=None, help="Real region for S3; defaults to 'auto' for R2")
    p.add_argument("--prefix", default="", help="Restrict browsing to keys under this prefix")
    p.add_argument("--disabled", action="store_true", help="Register but hide from the import UI")
    p.add_argument("--test", action="store_true", help="Verify credentials before writing")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()

    region = args.region or ("auto" if args.provider == "r2" else "us-east-1")

    try:
        source = VideoSource(
            source_id=str(uuid.uuid4()),
            channel_id=args.channel_id,
            name=args.name,
            provider=args.provider,
            bucket=args.bucket,
            access_key_id=args.access_key_id,
            secret_access_key=args.secret_access_key,
            endpoint_url=args.endpoint_url,
            region=region,
            prefix=args.prefix,
            enabled=not args.disabled,
        )
    except ValueError as exc:
        print(f"Invalid source config: {exc}")
        return 1

    if args.test:
        print(f"Testing connection to {args.provider}://{args.bucket}/{args.prefix} …")
        try:
            page = await asyncio.to_thread(
                build_client(source.model_dump()).list_objects_page, args.prefix, "/", None, 5
            )
        except Exception as exc:
            print(f"Connection FAILED: {type(exc).__name__}: {exc}")
            return 1
        print(f"Connection OK — {len(page['folders'])} folder(s), {len(page['files'])} object(s) in first page")
        for f in page["files"][:5]:
            print(f"    {f['key']}  ({f['size']:,} bytes)")

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
        print(f"Updated existing source '{args.name}' ({existing['source_id']}) for channel {args.channel_id}")
    else:
        await db.video_sources.insert_one(doc)
        print(f"Created source '{args.name}' ({doc['source_id']}) for channel {args.channel_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
