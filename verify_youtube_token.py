import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

TOKEN_PATH = os.getenv("YOUTUBE_TOKEN_JSON", "youtube_token.json")

def verify():
    if not os.path.exists(TOKEN_PATH):
        print(f"Error: Token file not found at {TOKEN_PATH}")
        return

    try:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH)
        youtube = build("youtube", "v3", credentials=creds)
        
        # Test request: Get the authenticated user's channel information
        request = youtube.channels().list(part="snippet", mine=True)
        response = request.execute()
        
        if "items" in response and len(response["items"]) > 0:
            channel_title = response["items"][0]["snippet"]["title"]
            print(f"Verification Successful! Authenticated as channel: {channel_title}")
        else:
            print("Verification Failed: No channel found for the authenticated user.")
            
    except Exception as e:
        print(f"Verification Failed with error: {e}")

if __name__ == "__main__":
    verify()
