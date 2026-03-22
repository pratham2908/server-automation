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
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": temp_path,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        
        video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # This downloads the file
            ydl.download([video_url])
            
        # The actual filename might have '.mp4' replacing the %(ext)s
        final_mp4_path = os.path.join(temp_dir, "video.mp4")
        if not os.path.exists(final_mp4_path):
            # In case it downloaded as something else or merge failed, find the video file
            files = os.listdir(temp_dir)
            if not files:
                raise RuntimeError("yt-dlp completed but no file was written")
            final_mp4_path = os.path.join(temp_dir, files[0])
            
        # Stream the downloaded file to R2
        with open(final_mp4_path, "rb") as f:
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
