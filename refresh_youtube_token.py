import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
TOKEN_PATH = os.getenv("YOUTUBE_TOKEN_JSON", "youtube_token.json")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

def refresh_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: YOUTUBE_CLIENT_ID or YOUTUBE_CLIENT_SECRET not found in .env")
        return

    # Delete existing token if it exists
    if os.path.exists(TOKEN_PATH):
        print(f"Deleting existing token: {TOKEN_PATH}")
        os.remove(TOKEN_PATH)

    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("Starting OAuth flow. Your browser should open automatically...")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # run_local_server will try to open a browser
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    
    print(f"Success! New token saved to {TOKEN_PATH}")

if __name__ == "__main__":
    refresh_token()
