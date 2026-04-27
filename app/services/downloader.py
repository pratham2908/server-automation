"""YouTube downloader service for pulling synced video files."""

import asyncio
import os
import tempfile
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.r2 import R2Service

# We import yt_dlp locally inside the thread to avoid blocking imports, 
# but making it available at the module level is fine too.


def _download_and_upload_sync(youtube_video_id: str, r2_key: str, r2_service: "R2Service") -> str:
    """Download video from YouTube using yt-dlp and upload to R2 synchronously."""
    import yt_dlp
    
    # We download to a temporary directory so we can clean up easily
    with tempfile.TemporaryDirectory() as temp_dir:
        # Define the output path in the temp dir
        temp_path = os.path.join(temp_dir, "video.%(ext)s")
        
        ydl_opts = {
            "format": "best",
            "outtmpl": temp_path,
            "quiet": True,
            "no_warnings": True,
            "source_address": "0.0.0.0",  # Force IPv4
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        }

        # Look for cookies.txt in the automation-server root (the current working directory)
        cookies_path = os.path.join(os.getcwd(), "cookies.txt")
        if os.path.exists(cookies_path):
            ydl_opts["cookiefile"] = cookies_path
            print(f"Using cookies from {cookies_path}")
        else:
            print(f"No cookies.txt found at {cookies_path}, continuing without auth")
        
        video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # This downloads the file
            ydl.download([video_url])
            
        # The actual filename might have any extension
        files = os.listdir(temp_dir)
        if not files:
            raise RuntimeError("yt-dlp completed but no file was written")
        
        # Take the first file that isn't a part/temp file
        final_video_path = os.path.join(temp_dir, files[0])
            
        # Stream the downloaded file to R2
        with open(final_video_path, "rb") as f:
            r2_service.upload_video(f, r2_key)
            
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
