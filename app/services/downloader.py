"""YouTube / Instagram download helpers for pulling video files into R2."""

import asyncio
import os
import shutil
import tempfile
import uuid
from typing import TYPE_CHECKING

import requests

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
        print("GITHUB_TOKEN or GITHUB_REPO not set. Falling back to local download using yt-dlp.")
        return _download_and_upload_local(youtube_video_id, r2_key, r2_service)

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


def _download_and_upload_local(youtube_video_id: str, r2_key: str, r2_service: "R2Service") -> str:
    """Download video from YouTube using yt-dlp locally and upload to R2."""
    import yt_dlp
    
    video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        tmp_path = tmp.name
        
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': tmp_path,
        'quiet': True,
        'noprogress': True,
        'js_runtimes': {'node': {}},
    }
    
    # Check for cookies.txt in current directory or app root
    cookie_paths = [
        "cookies.txt",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "cookies.txt")
    ]
    for cp in cookie_paths:
        if os.path.exists(cp):
            print(f"Using cookies file: {cp}")
            ydl_opts['cookiefile'] = cp
            break
    else:
        print("No cookies.txt found. Downloading without cookies.")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
            
        with open(tmp_path, "rb") as f:
            r2_service.upload_video(f, r2_key)
            
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
            
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


def _download_instagram_url_to_r2_sync(media_url: str, r2_key: str, r2_service: "R2Service") -> str:
    """Stream-download *media_url* (Graph API CDN) into R2 at *r2_key*."""
    with requests.get(media_url, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            shutil.copyfileobj(resp.raw, tmp, length=1024 * 1024)
            tmp.flush()
            tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            r2_service.upload_video(f, r2_key)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return r2_key


async def download_instagram_media_to_r2(channel_id: str, media_url: str, r2_service: "R2Service") -> str:
    """Download a reel from a Graph ``media_url`` into R2. Returns the object key."""
    new_video_id = str(uuid.uuid4())
    r2_key = f"{channel_id}/{new_video_id}.mp4"
    await asyncio.to_thread(_download_instagram_url_to_r2_sync, media_url, r2_key, r2_service)
    return r2_key


def _copy_r2_key_sync(source_key: str, dest_key: str, r2_service: "R2Service") -> str:
    r2_service.copy_video(source_key, dest_key)
    return dest_key


async def copy_r2_video_to_r2(source_key: str, target_channel_id: str, r2_service: "R2Service") -> str:
    """Server-side copy within R2 to a new key under *target_channel_id*."""
    new_id = str(uuid.uuid4())
    dest_key = f"{target_channel_id}/{new_id}.mp4"
    await asyncio.to_thread(_copy_r2_key_sync, source_key, dest_key, r2_service)
    return dest_key
