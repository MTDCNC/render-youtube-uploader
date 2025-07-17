import os
import requests
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

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
    print(f"Downloading {video_url}...")
    video_response = requests.get(video_url, stream=True)
    video_response.raise_for_status()

    buffer = io.BytesIO()
    for chunk in video_response.iter_content(chunk_size=1048576):
        buffer.write(chunk)
    buffer.seek(0)

    youtube = get_authenticated_service()

    body = {
        'snippet': {'title': title, 'description': description},
        'status': {'privacyStatus': privacy}
    }

    media = MediaIoBaseUpload(buffer, mimetype='video/*', chunksize=-1, resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    print(f"✅ Upload complete! YouTube video ID: {response['id']}")
    return f"https://youtu.be/{response['id']}"
