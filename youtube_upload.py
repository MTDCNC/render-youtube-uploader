# ✅ FINAL youtube_upload.py

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


def upload_to_youtube(video_url, title, description, privacy, bunny_delete_url=None, thumbnail_url=None):
    if os.path.exists("temp_video.mp4"):
        os.remove("temp_video.mp4")
        print("🗑️ Previous temp_video.mp4 deleted.")

    temp_file = "temp_video.mp4"
    print(f"⬇️ Downloading from {video_url}...")
    with requests.get(video_url, stream=True) as response:
        response.raise_for_status()
        with open(temp_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=1048576):
                f.write(chunk)
    print(f"✅ Download complete.")

    youtube = get_authenticated_service()

    body = {
        'snippet': {'title': title, 'description': description},
        'status': {'privacyStatus': privacy, 'madeForKids': False}
    }

    media = MediaFileUpload(temp_file, mimetype='video/*', resumable=True)
    request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    video_id = response['id']
    print(f"✅ Upload complete! YouTube video ID: {video_id}")

    # ✅ Optional Thumbnail
    if thumbnail_url:
        try:
            print(f"⬇️ Downloading thumbnail from {thumbnail_url}...")
            thumb_file = "temp_thumbnail.jpg"
            with requests.get(thumbnail_url, stream=True) as thumb_response:
                thumb_response.raise_for_status()
                with open(thumb_file, "wb") as f:
                    for chunk in thumb_response.iter_content(chunk_size=1024):
                        f.write(chunk)
            print("✅ Thumbnail downloaded. Uploading to YouTube...")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_file, mimetype='image/jpeg')
            ).execute()
            print("✅ Thumbnail uploaded.")
            os.remove(thumb_file)
            print("🗑️ Thumbnail file deleted.")
        except Exception as thumb_err:
            print(f"⚠️ Thumbnail failed but video uploaded fine: {thumb_err}")

    os.remove(temp_file)
    print(f"🗑️ Local video file {temp_file} deleted.")

    if bunny_delete_url:
        try:
            print(f"🗑️ Deleting Bunny file at: {bunny_delete_url}")
            delete_response = requests.delete(
                bunny_delete_url,
                headers={'AccessKey': os.environ['BUNNY_API_KEY']}
            )
            if delete_response.status_code == 200:
                print("✅ Bunny file deleted.")
            else:
                print(f"⚠️ Bunny delete failed: {delete_response.status_code}, {delete_response.text}")
        except Exception as bunny_err:
            print(f"⚠️ Bunny delete encountered an error: {bunny_err}")

    return f"https://youtu.be/{video_id}"
