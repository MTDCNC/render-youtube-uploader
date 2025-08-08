from flask import Flask, request, jsonify
import os
import requests
import json
import threading
import sys
import uuid
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request


# Ensure immediate logs
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

# OAuth / YouTube config
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]
STATUS_FILENAME = "youtube_status.json"

# Channel selection logic
def select_channel_by_location(location: str) -> str:
    loc = (location or "").lower()
    if "united kingdom" in loc:
        return "UK"
    if "north america" in loc:
        return "US"
    if "asia" in loc:
        return "Asia"
    return "UK"

# Load and save status for async checks
def load_status():
    try:
        with open(STATUS_FILENAME, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_status(job_id, youtube_url):
    statuses = load_status()
    statuses[job_id] = youtube_url
    with open(STATUS_FILENAME, 'w') as f:
        json.dump(statuses, f)
        f.flush()
        os.fsync(f.fileno())

# Instantiate YouTube client per channel
def get_authenticated_service(channel_key: str):
    # Map channel key to environment variable names
    mapping = {
        'UK':   ('YT_UK_CLIENT_ID', 'YT_UK_CLIENT_SECRET', 'YT_UK_REFRESH_TOKEN'),
        'US':   ('YT_US_CLIENT_ID', 'YT_US_CLIENT_SECRET', 'YT_US_REFRESH_TOKEN'),
        'Asia': ('YT_ASIA_CLIENT_ID', 'YT_ASIA_CLIENT_SECRET', 'YT_ASIA_REFRESH_TOKEN'),
    }
    client_id_key, client_secret_key, refresh_token_key = mapping[channel_key]
    creds = Credentials(
        token=None,
        refresh_token=os.environ[refresh_token_key],
        token_uri=os.environ.get('YOUTUBE_TOKEN_URI', 'https://oauth2.googleapis.com/token'),
        client_id=os.environ[client_id_key],
        client_secret=os.environ[client_secret_key],
        scopes=SCOPES
    )
    try:
        creds.refresh(Request())   # was: creds.refresh()
    except RefreshError as e:
        app.logger.error(f"OAuth refresh failed for {channel_key}: {e}")
        # TODO: integrate with alert system (Slack, email, etc.)
        raise
    return build('youtube', 'v3', credentials=creds)

# Core upload logic (async)
def async_upload_to_youtube(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key):
    try:
        temp_file = f'temp_{job_id}.mp4'
        app.logger.info(f"[{job_id}] Downloading {video_url}")
        with requests.get(video_url, stream=True) as r:
            r.raise_for_status()
            with open(temp_file, 'wb') as f:
                for chunk in r.iter_content(1048576):
                    f.write(chunk)
        app.logger.info(f"[{job_id}] Download complete")

        yt = get_authenticated_service(channel_key)
        tag_list = [t.strip() for t in raw_tags.split(',') if t.strip()]
        body = {
            'snippet': {'title': title, 'description': description, 'tags': tag_list},
            'status':  {'privacyStatus': privacy, 'madeForKids': False}
        }
        media = MediaFileUpload(temp_file, mimetype='video/*', resumable=True)
        req = yt.videos().insert(part='snippet,status', body=body, media_body=media)

        progress, status = None, None
        while status is None:
            progress, status = req.next_chunk()
            if progress:
                app.logger.info(f"[{job_id}] Upload {int(progress.progress()*100)}%")

        video_id = status['id']
        youtube_url = f"https://youtu.be/{video_id}"
        app.logger.info(f"[{job_id}] YouTube URL: {youtube_url}")

        # Set thumbnail if provided
        if thumbnail_url:
            try:
                thumb_file = f'thumb_{job_id}.jpg'
                with requests.get(thumbnail_url, stream=True) as tr:
                    tr.raise_for_status()
                    open(thumb_file, 'wb').write(tr.content)
                yt.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumb_file, mimetype='image/jpeg')
                ).execute()
                os.remove(thumb_file)
                app.logger.info(f"[{job_id}] Thumbnail set")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Thumbnail error: {e}")

        # Cleanup
        os.remove(temp_file)
        if bunny_delete_url:
            try:
                dr = requests.delete(bunny_delete_url, headers={'AccessKey': os.environ.get('BUNNY_API_KEY')})
                dr.raise_for_status()
                app.logger.info(f"[{job_id}] Bunny file deleted")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Bunny delete failed: {e}")

        save_status(job_id, youtube_url)
    except Exception as e:
        app.logger.error(f"[{job_id}] Upload error: {e}")

# Endpoint for uploading
@app.route('/upload-to-youtube', methods=['POST'])
def upload_endpoint():
    data = request.json or {}
    video_url       = data.get('video_url')
    title           = data.get('title')
    description     = data.get('description')
    raw_tags        = data.get('tags', '')
    privacy         = data.get('privacy', 'unlisted')
    thumbnail_url   = data.get('thumbnail_url')
    bunny_delete_url= data.get('bunny_delete_url')
    raw_loc         = data.get('location', '')
    channel_key     = select_channel_by_location(raw_loc)

    if not all([video_url, title, description]):
        return jsonify({'error': 'Missing video_url, title, or description'}), 400

    job_id = str(uuid.uuid4())
    app.logger.info(f"[{job_id}] Received job for channel {channel_key}")
    thread = threading.Thread(
        target=async_upload_to_youtube,
        args=(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key),
        daemon=True
    )
    thread.start()
    return jsonify({'status': 'processing', 'job_id': job_id}), 202

# Status check endpoint
@app.route('/status-check', methods=['GET'])

def status_check():
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'error': 'Missing job_id parameter'}), 400

    statuses = load_status()
    youtube_url = statuses.get(job_id)
    if youtube_url:
        return jsonify({'youtube_url': youtube_url}), 200
    return jsonify({'error': 'Not found'}), 404

# Health check
@app.route('/', methods=['GET'])
def health():
    return "YouTube Uploader is live!", 200
