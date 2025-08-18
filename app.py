# app.py
from flask import Flask, request, jsonify, g
import os
import requests
import json
import threading
import sys
import uuid
import logging
import traceback
from datetime import datetime, timezone
from urllib.parse import urlparse
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from requests.auth import HTTPBasicAuth
import mimetypes

# ---------- STDOUT line buffering so Render shows logs immediately ----------
sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)

# ---------- Verbose logging w/ request IDs ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(request_id)s] %(message)s",
    stream=sys.stdout,
)

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, "request_id", "-")
        return True

app.logger.addFilter(RequestIdFilter())

@app.before_request
def _add_request_id():
    g.request_id = str(uuid.uuid4())[:8]
    app.logger.info(f"{request.method} {request.path} received")

@app.after_request
def _after(resp):
    app.logger.info(f"{request.method} {request.path} -> {resp.status_code}")
    return resp

def log_exception(prefix: str, err: Exception):
    app.logger.error(f"{prefix}: {err}\n{traceback.format_exc()}")

# ---------- OAuth / YouTube config ----------
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
STATUS_FILENAME = "youtube_status.json"  # job_id -> dict

# ---------- Channel selection ----------
def select_channel_by_location(location: str) -> str:
    loc = (location or "").lower()
    if "united kingdom" in loc:
        return "UK"
    if "north america" in loc:
        return "US"
    if "asia" in loc:
        return "Asia"
    return "UK"

# ---------- Status persistence ----------
def load_status():
    try:
        with open(STATUS_FILENAME, "r") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def write_status(job_id: str, patch: dict):
    statuses = load_status()
    current = statuses.get(job_id, {})
    current.update(patch)
    statuses[job_id] = current
    with open(STATUS_FILENAME, "w") as f:
        json.dump(statuses, f)
        f.flush()
        os.fsync(f.fileno())

# ---------- YouTube client per channel ----------
def get_authenticated_service(channel_key: str):
    mapping = {
        "UK": ("YT_UK_CLIENT_ID", "YT_UK_CLIENT_SECRET", "YT_UK_REFRESH_TOKEN"),
        "US": ("YT_US_CLIENT_ID", "YT_US_CLIENT_SECRET", "YT_US_REFRESH_TOKEN"),
        "Asia": ("YT_ASIA_CLIENT_ID", "YT_ASIA_CLIENT_SECRET", "YT_ASIA_REFRESH_TOKEN"),
    }
    client_id_key, client_secret_key, refresh_token_key = mapping[channel_key]
    creds = Credentials(
        token=None,
        refresh_token=os.environ[refresh_token_key],
        token_uri=os.environ.get("YOUTUBE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        client_id=os.environ[client_id_key],
        client_secret=os.environ[client_secret_key],
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())  # surface revoked/expired tokens early
    except RefreshError as e:
        app.logger.error(f"OAuth refresh failed for {channel_key}: {e}")
        raise
    return build("youtube", "v3", credentials=creds)

# ---------- Helpers: streamed download with % logs ----------
def streamed_download_to_file(logger, url: str, dest_path: str, job_tag: str, chunk_size=1_048_576):
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0")) or None
        next_pct = 0
        read = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                if total:
                    read += len(chunk)
                    pct = int((read / total) * 100)
                    if pct >= next_pct:
                        logger.info(f"{job_tag} download {pct}%")
                        next_pct += 10
    logger.info(f"{job_tag} download complete -> {dest_path}")

# ---------- Async uploader (YouTube) ----------
def async_upload_to_youtube(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at=None):
    try:
        # Friendly name
        parsed = urlparse(video_url)
        filename = os.path.basename(parsed.path) or f"video_{job_id}.mp4"
        temp_file = f"temp_{job_id}.mp4"

        app.logger.info(f"[{job_id}] Starting YouTube job for channel={channel_key} file=\"{filename}\"")
        streamed_download_to_file(app.logger, video_url, temp_file, f"[{job_id}]")

        # Build YouTube client & request
        yt = get_authenticated_service(channel_key)
        tag_list = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]

        # Decide scheduling
        status_obj = {"privacyStatus": privacy, "madeForKids": False}
        if publish_at:
            try:
                app.logger.info(f"[{job_id}] Received publish_at={publish_at}")
                from datetime import datetime as _dt
                if "T" in publish_at:
                    if publish_at.endswith("Z"):
                        publish_at = publish_at.replace("Z", "+00:00")
                    dt = _dt.fromisoformat(publish_at)
                else:
                    dt = _dt.strptime(publish_at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                now_utc = _dt.now(timezone.utc)
                if dt > now_utc:
                    status_obj["privacyStatus"] = "private"  # required by YT for scheduling
                    status_obj["publishAt"] = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    app.logger.info(f"[{job_id}] Scheduling for future publish at {status_obj['publishAt']}")
                else:
                    app.logger.info(f"[{job_id}] publish_at is in the past — publishing immediately.")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Could not parse publish_at '{publish_at}': {e}")

        body = {"snippet": {"title": title, "description": description, "tags": tag_list}, "status": status_obj}
        media = MediaFileUpload(temp_file, mimetype="video/*", resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        # Upload progress every 5%
        last_logged = -5
        progress, status = None, None
        while status is None:
            progress, status = req.next_chunk()
            if progress:
                pct = int(progress.progress() * 100)
                if pct - last_logged >= 5:
                    app.logger.info(f"[{job_id}] upload {pct}%")
                    last_logged = pct

        video_id = status["id"]
        youtube_url = f"https://youtu.be/{video_id}"
        app.logger.info(f"[{job_id}] YouTube upload complete -> {youtube_url}")

        # Thumbnail (non-fatal)
        if thumbnail_url:
            try:
                thumb_file = f"thumb_{job_id}.jpg"
                streamed_download_to_file(app.logger, thumbnail_url, thumb_file, f"[{job_id}] thumbnail")
                yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_file, mimetype="image/jpeg")).execute()
                os.remove(thumb_file)
                app.logger.info(f"[{job_id}] Thumbnail set")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Thumbnail error (non-fatal): {e}")

        # Cleanup local temp
        try:
            os.remove(temp_file)
        except Exception:
            pass

        # Bunny delete (non-fatal)
        if bunny_delete_url:
            try:
                app.logger.info(f"[{job_id}] Deleting source from Bunny temp storage")
                dr = requests.delete(bunny_delete_url, headers={"AccessKey": os.environ.get("BUNNY_API_KEY")})
                info = {"ok": dr.ok, "status_code": dr.status_code, "text": (dr.text or "")[:500]}
                if dr.ok:
                    app.logger.info(f"[{job_id}] Bunny delete OK {info['status_code']}")
                else:
                    app.logger.warning(f"[{job_id}] Bunny delete failed {info}")
                write_status(job_id, {"bunny_delete": info})
            except Exception as e:
                app.logger.warning(f"[{job_id}] Bunny delete exception: {e}")
                write_status(job_id, {"bunny_delete": {"ok": False, "error": str(e)}})

        # Persist final state
        write_status(
            job_id,
            {
                "state": "completed",
                "youtube_url": youtube_url,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "channel": channel_key,
                "source_filename": filename,
            },
        )
        app.logger.info(f"[{job_id}] Automation complete")

    except Exception as e:
        log_exception(f"[{job_id}] Upload error", e)
        write_status(
            job_id,
            {"state": "error", "error": str(e), "finished_at": datetime.utcnow().isoformat() + "Z"},
        )

# ---------- WordPress async upload helpers ----------
WP_STATUS_FILENAME = "wp_status.json"  # job_id -> {attachment_id, source_url, ...}

def wp_load_status():
    try:
        with open(WP_STATUS_FILENAME, "r") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def wp_write_status(job_id: str, patch: dict):
    statuses = wp_load_status()
    current = statuses.get(job_id, {})
    current.update(patch)
    statuses[job_id] = current
    with open(WP_STATUS_FILENAME, "w") as f:
        json.dump(statuses, f)
        f.flush()
        os.fsync(f.fileno())

def _wp_api_base():
    base = os.environ.get("WP_API_BASE")  # e.g. https://example.com/wp-json/wp/v2
    if not base:
        raise RuntimeError("Missing env var WP_API_BASE")
    return base.rstrip("/")

def _wp_auth():
    user = os.environ.get("WP_USER")
    app_pass = os.environ.get("WP_APP_PASSWORD")
    if not user or not app_pass:
        raise RuntimeError("Missing WP_USER or WP_APP_PASSWORD")
    return HTTPBasicAuth(user, app_pass)

def async_upload_to_wordpress(job_id, video_url, filename, title, alt_text, post_id):
    """
    Downloads from Bunny CDN and uploads to WordPress Media Library.
    Non-fatal on metadata update failure. Persists results to wp_status.json.
    """
    try:
        # 1) Download Bunny file (with % logs)
        temp_path = f"wp_{job_id}"
        app.logger.info(f"[WP {job_id}] Starting WordPress job file=\"{filename or 'video.mp4'}\"")
        streamed_download_to_file(app.logger, video_url, temp_path, f"[WP {job_id}]")

        # 2) Upload to WP /media (multipart)
        api_base = _wp_api_base()
        media_ep = f"{api_base}/media"
        guess, _ = mimetypes.guess_type(filename or "")
        mime = guess or "video/mp4"
        up_name = filename or f"video_{job_id}.mp4"

        with open(temp_path, "rb") as fh:
            files = {"file": (up_name, fh, mime)}
            headers = {"Content-Disposition": f'attachment; filename="{up_name}"'}
            app.logger.info(f"[WP {job_id}] Uploading to {media_ep}")
            resp = requests.post(media_ep, files=files, headers=headers, auth=_wp_auth(), timeout=900)
        app.logger.info(f"[WP {job_id}] Response {resp.status_code}")

        resp.raise_for_status()
        media_json = resp.json()
        attachment_id = media_json.get("id")
        source_url = media_json.get("source_url")
        app.logger.info(f"[WP {job_id}] Upload complete id={attachment_id} url={source_url}")

        # 3) Optional metadata / attach to post
        if any([title, alt_text, post_id]):
            patch = {}
            if title:
                patch["title"] = title
            if alt_text:
                patch["alt_text"] = alt_text
            if post_id:
                patch["post"] = int(post_id)
            try:
                meta_ep = f"{api_base}/media/{attachment_id}"
                app.logger.info(f"[WP {job_id}] Updating media metadata")
                pr = requests.post(meta_ep, json=patch, auth=_wp_auth(), timeout=120)
                pr.raise_for_status()
                app.logger.info(f"[WP {job_id}] Metadata updated")
            except Exception as e:
                app.logger.warning(f"[WP {job_id}] Meta update failed: {e}")

        # 4) Cleanup temp, save status
        try:
            os.remove(temp_path)
        except Exception:
            pass

        wp_write_status(
            job_id,
            {
                "state": "completed",
                "attachment_id": attachment_id,
                "source_url": source_url,
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "filename": up_name,
            },
        )

    except Exception as e:
        log_exception(f"[WP {job_id}] Upload error", e)
        wp_write_status(
            job_id,
            {"state": "error", "error": str(e), "finished_at": datetime.utcnow().isoformat() + "Z"},
        )

# ---------- Routes ----------
@app.route("/upload-to-youtube", methods=["POST"])
def upload_endpoint():
    data = request.json or {}
    video_url = data.get("video_url")
    title = data.get("title")
    description = data.get("description")
    raw_tags = data.get("tags", "")
    privacy = data.get("privacy", "unlisted")
    thumbnail_url = data.get("thumbnail_url")
    bunny_delete_url = data.get("bunny_delete_url")
    raw_loc = data.get("location", "")
    channel_key = select_channel_by_location(raw_loc)
    publish_at = data.get("publish_at")

    if not all([video_url, title, description]):
        return jsonify({"error": "Missing video_url, title, or description"}), 400

    job_id = str(uuid.uuid4())

    # Record initial status for polling
    write_status(
        job_id,
        {
            "state": "processing",
            "channel": channel_key,
            "title": title,
            "location_raw": raw_loc,
            "started_at": datetime.utcnow().isoformat() + "Z",
        },
    )

    app.logger.info(f"[{job_id}] Received YT job for channel={channel_key}")
    thread = threading.Thread(
        target=async_upload_to_youtube,
        args=(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "processing", "job_id": job_id, "channel": channel_key}), 202

@app.route("/status-check", methods=["GET"])
def status_check():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "Missing job_id parameter"}), 400
    data = load_status().get(job_id)
    if data:
        return jsonify(data), 200
    return jsonify({"error": "Not found"}), 404

# ---------- WordPress ----------
@app.route("/upload-to-wordpress", methods=["POST"])
def upload_to_wordpress():
    data = request.json or {}
    video_url = data.get("video_url")  # Bunny CDN file
    filename = data.get("filename")    # e.g. "clip.mp4"
    title = data.get("title")          # optional
    alt_text = data.get("alt_text")    # optional
    post_id = data.get("post_id")      # optional

    if not video_url:
        return jsonify({"error": "Missing video_url"}), 400

    job_id = str(uuid.uuid4())

    # Record initial WP status for polling
    wp_write_status(
        job_id,
        {
            "state": "processing",
            "title": title,
            "started_at": datetime.utcnow().isoformat() + "Z",
        },
    )

    app.logger.info(f"[WP {job_id}] Received WP job")
    thread = threading.Thread(
        target=async_upload_to_wordpress,
        args=(job_id, video_url, filename, title, alt_text, post_id),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "processing", "job_id": job_id}), 202

@app.route("/wp-status", methods=["GET"])
def wp_status():
    job_id = request.args.get("job_id")
    if not job_id:
        return jsonify({"error": "Missing job_id parameter"}), 400
    entry = wp_load_status().get(job_id)
    if entry:
        return jsonify(entry), 200
    return jsonify({"error": "Not found"}), 404

@app.route("/", methods=["GET"])
def health():
    return "YouTube Uploader is live!", 200
