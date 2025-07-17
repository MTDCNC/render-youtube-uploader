import os
import requests
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

def get_authenticated_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ['YOUTUBE_REFRESH_TOKEN'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=os.environ['YOUTUBE_CLIENT_ID'],
        client_secret=os.environ['YOUTUBE_CLIENT_SECRET'],
        scopes=SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())
    return build('youtube', 'v3', credentials=creds)

def upload_to_youtube(video_url, title, description, privacy):
    # Clean up any leftover temp files
    if os.path.exists("temp_video.mp4"):
        os.remove("temp_video.mp4")
        print("🗑️ Previous temp_video.mp4 deleted to free up space.")
        
    temp_file = "temp_video.mp4"

    print(f"⬇️ Downloading {video_url} to disk...")
    with requests.get(video_url, stream=True) as response:
        response.raise_for_status()
        with open(temp_file, "wb") as out_file:
            for chunk in response.iter_content(chunk_size=1048576):
                out_file.write(chunk)
    print(f"✅ Download complete, starting YouTube upload.")

    youtube = get_authenticated_service()

    body = {
        'snippet': {'title': title, 'description': description},
        'status': {'privacyStatus': privacy}
    }

    media = MediaFileUpload(temp_file, mimetype='video/*', resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    print(f"✅ Upload complete! YouTube video ID: {response['id']}")

    # Cleanup
    os.remove(temp_file)
    print(f"🗑️ Deleted local file {temp_file}.")

    return f"https://youtu.be/{response['id']}"
