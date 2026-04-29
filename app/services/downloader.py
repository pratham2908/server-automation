"""YouTube downloader service for pulling synced video files."""

import asyncio
import os
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.r2 import R2Service

# We import yt_dlp locally inside the thread to avoid blocking imports,
# but making it available at the module level is fine too.


def _download_and_upload_sync(youtube_video_id: str, r2_key: str, r2_service: "R2Service") -> str:
    """Download video from YouTube using yt-dlp and upload to R2 synchronously."""
    import requests

    # The video URL
    video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"

    # Get GitHub credentials from environment
    github_token = os.environ.get("GITHUB_TOKEN")
    github_repo = os.environ.get("GITHUB_REPO")  # Format: owner/repo

    if not github_token or not github_repo:
        raise RuntimeError("GITHUB_TOKEN or GITHUB_REPO environment variables not set. Cannot trigger GitHub Action.")

    # Trigger the GitHub Action
    api_url = f"https://api.github.com/repos/{github_repo}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {github_token}",
    }
    data = {
        "event_type": "download_video",
        "client_payload": {"youtube_url": video_url, "r2_key": r2_key},
    }

    print(f"Triggering GitHub Action for {video_url} with key {r2_key}")
    response = requests.post(api_url, headers=headers, json=data)

    if response.status_code != 204:
        raise RuntimeError(f"Failed to trigger GitHub Action: {response.status_code} {response.text}")

    print("GitHub Action triggered successfully.")

    # We return the key immediately, but the file won't be available until the action finishes
    # In a fully robust system, you'd poll R2 or wait for a webhook callback
    return r2_key


async def download_youtube_video_to_r2(youtube_video_id: str, channel_id: str, r2_service: "R2Service") -> str:
    """Async wrapper to download a YouTube video and upload it straight to R2.

    Returns the R2 object key.
    """
    new_video_id = str(uuid.uuid4())
    r2_key = f"{channel_id}/{new_video_id}.mp4"

    # Run the blocking download & upload in a background thread
    await asyncio.to_thread(_download_and_upload_sync, youtube_video_id, r2_key, r2_service)

    return r2_key
