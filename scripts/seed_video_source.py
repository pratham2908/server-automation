"""Register (or update) the content app a channel pulls its finished videos from.

Credentials never travel through the API, so this is the only way a source gets
created. Run it from the automation-server directory with the venv active.

A source has a *kind*, and the kind decides which flags apply:

    # Export-feed app (static shared secret, cursor pagination)
    python3 scripts/seed_video_source.py georank \
        --channel-id officialgeoranking \
        --name "GeoRank renderer" \
        --base-url https://georank-server-xxx.us-central1.run.app \
        --api-key '<shared secret>' \
        --test

    # VidForge studio library (account login, page pagination)
    python3 scripts/seed_video_source.py vidforge \
        --channel-id ai_howthingswork \
        --name "VidForge clips" \
        --base-url https://video-gen-server-xxx.us-central1.run.app \
        --email official.ambience@gmail.com \
        --password '<password>' \
        --app-key clips \
        --test

Re-running with the same --channel-id and --name updates that source in place
rather than creating a duplicate. --test calls the app first and refuses to write
anything if it does not answer.

Path defaults match each app's documented contract; override them only for a
deployment that deviates.
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
from app.models.video_source import GeoRankConfig, VideoSource, VidForgeConfig  # noqa: E402
from app.services.video_sources import adapter_for, describe_http_error  # noqa: E402
from app.timezone import now_ist  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Register a channel's content app as a video source")
    sub = p.add_subparsers(dest="kind", required=True, help="Which kind of app this is")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--channel-id", required=True)
        sp.add_argument("--name", required=True, help="Display label for this source")
        sp.add_argument("--base-url", required=True, help="App origin, e.g. https://xyz.run.app")
        sp.add_argument("--disabled", action="store_true", help="Register but hide from the import UI")
        sp.add_argument("--test", action="store_true", help="Call the app before writing")

    g = sub.add_parser("georank", help="App exposing the read-only export feed contract")
    common(g)
    g.add_argument("--api-key", required=True, help="Shared secret issued by the app")
    g.add_argument("--auth-style", default="bearer", choices=["bearer", "api_key_header"])
    g.add_argument("--list-path", default="/api/ext/videos")
    g.add_argument("--detail-path", default="/api/ext/videos/{id}")
    g.add_argument(
        "--mark-imported-path",
        default="/api/ext/videos/{id}/imported",
        help="POSTed after ingest to close the pull loop; pass '' for an app without it",
    )

    v = sub.add_parser("vidforge", help="VidForge studio library")
    common(v)
    v.add_argument("--email", required=True, help="Account email")
    v.add_argument("--password", required=True, help="Account password")
    v.add_argument("--app-key", default="clips", help="Which app's profile to read; decides the library")
    v.add_argument("--video-kind", default="edited", help="'edited' = finished renders; '' for all kinds")
    v.add_argument("--status", default="completed", help="Only videos in this pipeline status; '' for all")
    v.add_argument("--sent-flag-field", default="alreadySentToChannel")
    v.add_argument("--login-path", default="/api/auth/login")
    v.add_argument("--list-path", default="/api/videos")
    v.add_argument("--detail-path", default="/api/videos/{id}")
    v.add_argument(
        "--mark-imported-path",
        default="/api/videos/{id}",
        help="PATCHed with the sent flag after ingest; pass '' for an app without it",
    )

    return p.parse_args()


def build_config(args: argparse.Namespace) -> GeoRankConfig | VidForgeConfig:
    if args.kind == "georank":
        return GeoRankConfig(
            list_path=args.list_path,
            detail_path=args.detail_path,
            mark_imported_path=args.mark_imported_path,
            api_key=args.api_key,
            auth_style=args.auth_style,
        )
    return VidForgeConfig(
        login_path=args.login_path,
        list_path=args.list_path,
        detail_path=args.detail_path,
        mark_imported_path=args.mark_imported_path,
        email=args.email,
        password=args.password,
        app_key=args.app_key,
        # An empty string means "do not filter", which is not the same as the
        # default — so it maps to None rather than being passed through.
        video_kind=args.video_kind or None,
        status=args.status or None,
        sent_flag_field=args.sent_flag_field,
    )


async def probe(source: VideoSource) -> bool:
    """Ask the app for one page through its own adapter and print what came back."""
    adapter = adapter_for(source)
    print(f"Testing {source.base_url} as '{source.kind}' ({adapter.credential_hint(source)}) …")
    try:
        page = await adapter.fetch_page(source, limit=3, cursor=None)
    except Exception as exc:
        print(f"  FAILED: {describe_http_error(exc)}")
        return False

    print(f"  OK — {len(page.videos)} video(s) in the first page, url_ttl={page.url_ttl_seconds}")
    for v in page.videos[:3]:
        secs = (v.duration_ms or 0) // 1000
        sent = " [already sent]" if v.already_sent_to_channel else ""
        print(f"    [{v.status}] {v.title}  ({secs // 60}:{secs % 60:02d})  id={v.id}{sent}")
    if not adapter.supports_mark_imported(source):
        print("  NOTE: this source cannot be told about an ingest — imports will not be marked delivered.")
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
            config=build_config(args),
            enabled=not args.disabled,
        )
    except ValueError as exc:
        print(f"Invalid source config: {exc}")
        return 1

    if args.test and not await probe(source):
        print("Refusing to write a source that does not answer. Fix the URL or credentials and retry.")
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
