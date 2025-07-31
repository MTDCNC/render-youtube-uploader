from flask import Flask, request, jsonify
import os
import requests
import json
import threading
import sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Ensure immediate logs
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

# OAuth / YouTube config
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]
STATUS_FILENAME = "youtube_status.json"


def load_status():
    try:
        with open(STATUS_FILENAME, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_status(title, url):
    statuses = load_status()
    statuses[title] = url
    with open(STATUS_FILENAME, 'w') as f:
        json.dump(statuses, f)
        f.flush()
        os.fsync(f.fileno())


def get_authenticated_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ['YOUTUBE_REFRESH_TOKEN'],
        token_uri=os.environ.get('YOUTUBE_TOKEN_URI', 'https://oauth2.googleapis.com/token'),
        client_id=os.environ['YOUTUBE_CLIENT_ID'],
        client_secret=os.environ['YOUTUBE_CLIENT_SECRET'],
        scopes=SCOPES
    )
    return build('youtube', 'v3', credentials=creds)


def async_upload_to_youtube(video_url, title, description, privacy, thumbnail_url, bunny_delete_url):
    try:
        temp_file = 'temp_video.mp4'
        if os.path.exists(temp_file):
            os.remove(temp_file)
            print(f"🗑️ Cleared old {temp_file}", flush=True)

        print(f"⬇️ [Async] Fetching video from {video_url}...", flush=True)
        with requests.get(video_url, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, 'wb') as f:
                for chunk in r.iter_content(1048576):
                    f.write(chunk)
        print("✅ [Async] Download complete", flush=True)

        yt = get_authenticated_service()
        body = {
            'snippet': {'title': title, 'description': description},
            'status':  {'privacyStatus': privacy, 'madeForKids': False}
        }
        media = MediaFileUpload(temp_file, mimetype='video/*', resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        status = None
        while status is None:
            progress, status = req.next_chunk()
            if progress:
                print(f"📈 [Async] Upload progress: {int(progress.progress()*100)}%", flush=True)

        video_id = status['id']
        youtube_url = f"https://youtu.be/{video_id}"
        print(f"✅ [Async] Upload complete! YouTube URL: {youtube_url}", flush=True)

        if thumbnail_url:
            try:
                thumb_file = 'temp_thumb.jpg'
                with requests.get(thumbnail_url, stream=True) as tr:
                    tr.raise_for_status()
                    with open(thumb_file, 'wb') as f:
                        f.write(tr.content)
                print("⬇️ [Async] Thumbnail downloaded, setting on YouTube...", flush=True)
                yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_file, mimetype='image/jpeg')).execute()
                os.remove(thumb_file)
                print("✅ [Async] Thumbnail set", flush=True)
            except Exception as e:
                print(f"⚠️ [Async] Thumbnail error: {e}", flush=True)

        os.remove(temp_file)
        print(f"🗑️ [Async] Removed {temp_file}", flush=True)
        if bunny_delete_url:
            try:
                dr = requests.delete(bunny_delete_url, headers={'AccessKey': os.environ['BUNNY_API_KEY']})
                dr.raise_for_status()
                print("✅ [Async] Bunny file deleted", flush=True)
            except Exception as e:
                print(f"⚠️ [Async] Bunny delete failed: {e}", flush=True)

        save_status(title, youtube_url)

    except Exception as e:
        print(f"❌ [Async] Upload error for {title}: {e}", flush=True)

@app.route('/upload-to-youtube', methods=['POST'])
def upload_endpoint():
    data = request.json or {}
    video_url = data.get('video_url')
    title = data.get('title')
    description = data.get('description')
    privacy = data.get('privacy', 'unlisted')
    thumbnail_url = data.get('thumbnail_url')
    bunny_delete_url = data.get('bunny_delete_url')

    if not all([video_url, title, description]):
        return jsonify({'error': 'Missing video_url, title, or description'}), 400

    print(f"📥 Received upload request for {title}", flush=True)
    thread = threading.Thread(
        target=async_upload_to_youtube,
        args=(video_url, title, description, privacy, thumbnail_url, bunny_delete_url),
        daemon=True
    )
    thread.start()

    return jsonify({'status': 'processing', 'title': title}), 202

@app.route('/status-check', methods=['GET'])
def status_check():
    title = request.args.get('title')
    if not title:
        return jsonify({'error': 'Missing title parameter'}), 400

    statuses = load_status()
    url = statuses.get(title)
    if url:
        return jsonify({'youtube_url': url}), 200
    return jsonify({'error': 'Not found'}), 404

@app.route('/', methods=['GET'])
def health():
    return "YouTube Uploader is live!", 200
