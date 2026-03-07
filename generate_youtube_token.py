"""Generate a YouTube OAuth token for a specific channel.

Creates a token file at youtube_tokens/{channel_id}.json with all required
scopes (upload, readonly, analytics). Opens a browser for Google OAuth consent.

Usage:
    python generate_youtube_token.py <channel_id>

Example:
    python generate_youtube_token.py physicsasmr_official

Sign in with the Google account that OWNS the YouTube channel.
"""

import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_youtube_token.py <channel_id>")
        print("Example: python generate_youtube_token.py physicsasmr_official")
        sys.exit(1)

    channel_id = sys.argv[1]
    token_path = f"youtube_tokens/{channel_id}.json"

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET not found in .env")
        sys.exit(1)

    os.makedirs("youtube_tokens", exist_ok=True)

    if os.path.exists(token_path):
        print(f"Token already exists at {token_path}")
        resp = input("Overwrite? (y/N): ").strip().lower()
        if resp != "y":
            print("Aborted.")
            sys.exit(0)

    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print(f"\nGenerating token for channel: {channel_id}")
    print(f"Token will be saved to: {token_path}")
    print("\nIMPORTANT: Sign in with the Google account that OWNS this channel.\n")

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    print(f"\nToken saved to {token_path}")
    print("You can now use this channel with the automation server.")


if __name__ == "__main__":
    main()
