from flask import Flask, request, jsonify
import os
import requests
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


def upload_to_youtube(video_url, title, description, privacy, thumbnail_url=None, bunny_delete_url=None):
    # Download temp video
    temp = 'temp_video.mp4'
    if os.path.exists(temp):
        os.remove(temp)
        print(f"🗑️ Cleared old {temp}", flush=True)

    print(f"⬇️ Fetching video from {video_url}...", flush=True)
    with requests.get(video_url, stream=True) as r:
        r.raise_for_status()
        with open(temp, 'wb') as f:
            for chunk in r.iter_content(1048576): f.write(chunk)
    print("✅ Downloaded video", flush=True)

    yt = get_authenticated_service()
    body = {
        'snippet': {'title': title, 'description': description},
        'status':  {'privacyStatus': privacy, 'madeForKids': False}
    }
    media = MediaFileUpload(temp, mimetype='video/*', resumable=True)
    req = yt.videos().insert(part=','.join(body.keys()), body=body, media_body=media)

    status = None
    while status is None:
        st, status = req.next_chunk()
        if st:
            print(f"📈 Progress {int(st.progress()*100)}%", flush=True)

    video_id = status['id']
    print(f"✅ YouTube ID: {video_id}", flush=True)

    # Optional thumbnail
    if thumbnail_url:
        try:
            tf = 'temp_thumb.jpg'
            with requests.get(thumbnail_url, stream=True) as tr:
                tr.raise_for_status()
                with open(tf, 'wb') as f: f.write(tr.content)
            print("⬇️ Thumb downloaded, setting on YouTube...", flush=True)
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(tf, mimetype='image/jpeg')).execute()
            os.remove(tf)
            print("✅ Thumb set", flush=True)
        except Exception as e:
            print(f"⚠️ Thumb error: {e}", flush=True)

    # Clean up & delete from Bunny
    os.remove(temp)
    print(f"🗑️ Removed {temp}", flush=True)
    if bunny_delete_url:
        try:
            dr = requests.delete(bunny_delete_url, headers={'AccessKey': os.environ['BUNNY_API_KEY']})
            dr.raise_for_status()
            print("✅ Bunny file deleted", flush=True)
        except Exception as e:
            print(f"⚠️ Bunny delete failed: {e}", flush=True)

    return f"https://youtu.be/{video_id}"


@app.route('/upload-to-youtube', methods=['POST'])
def upload_endpoint():
    data = request.json or {}
    url = data.get('video_url')
    title = data.get('title')
    desc = data.get('description')
    priv = data.get('privacy', 'unlisted')
    thumb = data.get('thumbnail_url')
    bdurl = data.get('bunny_delete_url')

    if not all([url, title, desc]):
        return jsonify({'error': 'Missing video_url, title, or description'}), 400

    try:
        youtube_url = upload_to_youtube(url, title, desc, priv, thumb, bdurl)
        return jsonify({
            'status': 'success',
            'youtube_url': youtube_url
        }), 200
    except Exception as e:
        print(f"❌ Upload error: {e}", flush=True)
        return jsonify({'error': str(e)}), 500

@app.route('/', methods=['GET'])
def health():
    return "YouTube Uploader is live!", 200
