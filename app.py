# app.py
# Purpose: Async upload service for YouTube and WordPress with robust logging.
# - Returns {"status":"processing","job_id":...} immediately to avoid Zapier timeouts.
# - Background threads do the real work and write progress/results to local JSON status files.
# - Render logs show: request IDs, percent progress for downloads/uploads, clear error reasons.

from flask import Flask, request, jsonify, g, has_request_context
import os
import sys
import json
import uuid
import logging
import traceback
import threading
import mimetypes
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# ---------- Ensure immediate logs to stdout (Render) ----------
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

app = Flask(__name__)

# ---------- Logging setup ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(request_id)s] %(message)s",
    stream=sys.stdout,
)

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        # default if nothing supplied
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        # prefer Flask's request-scoped id when a request context exists
        if has_request_context() and hasattr(g, "request_id"):
            record.request_id = g.request_id
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

def job_logger(job_id: str) -> logging.LoggerAdapter:
    # Stable 8-char id printed in each threaded log line
    return logging.LoggerAdapter(app.logger, {"request_id": job_id[:8]})

def now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

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

# ---------- Status persistence (YouTube) ----------
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

# ---------- Status persistence (WordPress) ----------
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

# ---------- YouTube client per channel ----------
def get_authenticated_service(channel_key: str):
    mapping = {
        "UK":   ("YT_UK_CLIENT_ID", "YT_UK_CLIENT_SECRET", "YT_UK_REFRESH_TOKEN"),
        "US":   ("YT_US_CLIENT_ID", "YT_US_CLIENT_SECRET", "YT_US_REFRESH_TOKEN"),
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
        # Surface invalid/expired tokens early
        creds.refresh(Request())
    except RefreshError as e:
        app.logger.error(f"OAuth refresh failed for {channel_key}: {e}")
        raise
    return build("youtube", "v3", credentials=creds)

# ---------- Streamed download with % logs ----------
def streamed_download_to_file(logger: logging.Logger, url: str, dest_path: str, job_tag: str, chunk_size=1_048_576):
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

# ---------- File wrapper to log WP upload progress ----------
class LoggingFile:
    def __init__(self, file_obj, size: int, logger: logging.Logger, job_tag: str, step_label="upload", log_step=10):
        self._f = file_obj
        self._size = max(size or 0, 1)
        self._logger = logger
        self._job_tag = job_tag
        self._step_label = step_label
        self._next_pct = 0
        self._sent = 0
        self._log_step = max(1, log_step)

    def read(self, amt=None):
        data = self._f.read(amt)
        if data:
            self._sent += len(data)
            pct = int((self._sent / self._size) * 100)
            if pct >= self._next_pct:
                self._logger.info(f"{self._job_tag} {self._step_label} {pct}%")
                self._next_pct += self._log_step
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)

# ---------- Async uploader (YouTube) ----------
def async_upload_to_youtube(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at=None):
    logger = job_logger(job_id)
    try:
        # Friendly name & download
        parsed = urlparse(video_url)
        filename = os.path.basename(parsed.path) or f"video_{job_id}.mp4"
        temp_file = f"temp_{job_id}.mp4"

        logger.info(f"[{job_id}] YT job start channel={channel_key} file=\"{filename}\"")
        streamed_download_to_file(logger, video_url, temp_file, f"[{job_id}]")

        # Build YouTube client & request
        yt = get_authenticated_service(channel_key)
        tag_list = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]

        # Decide scheduling
        status_obj = {"privacyStatus": privacy, "madeForKids": False}
        if publish_at:
            try:
                logger.info(f"[{job_id}] publish_at received: {publish_at}")
                if "T" in publish_at:
                    if publish_at.endswith("Z"):
                        publish_at = publish_at.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(publish_at)
                else:
                    dt = datetime.strptime(publish_at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                if dt > datetime.now(timezone.utc):
                    status_obj["privacyStatus"] = "private"  # required by YT for scheduling
                    status_obj["publishAt"] = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    logger.info(f"[{job_id}] scheduling for {status_obj['publishAt']}")
                else:
                    logger.info(f"[{job_id}] publish_at is in the past; publish immediately")
            except Exception as e:
                logger.warning(f"[{job_id}] publish_at parse failed: {e}")

        body = {"snippet": {"title": title, "description": description, "tags": tag_list}, "status": status_obj}
        media = MediaFileUpload(temp_file, mimetype="video/*", resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

        # Upload progress (every ~5%)
        last_logged = -5
        progress, status = None, None
        while status is None:
            progress, status = req.next_chunk()
            if progress:
                pct = int(progress.progress() * 100)
                if pct - last_logged >= 5:
                    logger.info(f"[{job_id}] upload {pct}%")
                    last_logged = pct

        video_id = status["id"]
        youtube_url = f"https://youtu.be/{video_id}"
        logger.info(f"[{job_id}] YT upload complete -> {youtube_url}")

        # Thumbnail (non-fatal)
        if thumbnail_url:
            try:
                thumb_file = f"thumb_{job_id}.jpg"
                streamed_download_to_file(logger, thumbnail_url, thumb_file, f"[{job_id}] thumbnail")
                yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_file, mimetype="image/jpeg")).execute()
                try:
                    os.remove(thumb_file)
                except Exception:
                    pass
                logger.info(f"[{job_id}] thumbnail set")
            except Exception as e:
                logger.warning(f"[{job_id}] thumbnail error (non-fatal): {e}")

        # Cleanup local temp
        try:
            os.remove(temp_file)
        except Exception:
            pass

        # Bunny delete (non-fatal)
        if bunny_delete_url:
            try:
                logger.info(f"[{job_id}] deleting source from Bunny temp storage")
                dr = requests.delete(bunny_delete_url, headers={"AccessKey": os.environ.get("BUNNY_API_KEY")})
                info = {"ok": dr.ok, "status_code": dr.status_code, "text": (dr.text or "")[:500]}
                if dr.ok:
                    logger.info(f"[{job_id}] Bunny delete OK {info['status_code']}")
                else:
                    logger.warning(f"[{job_id}] Bunny delete failed {info}")
                write_status(job_id, {"bunny_delete": info})
            except Exception as e:
                logger.warning(f"[{job_id}] Bunny delete exception: {e}")
                write_status(job_id, {"bunny_delete": {"ok": False, "error": str(e)}})

        # Persist final state
        write_status(
            job_id,
            {
                "state": "completed",
                "youtube_url": youtube_url,
                "finished_at": now_utc_iso(),
                "channel": channel_key,
                "source_filename": filename,
            },
        )
        logger.info(f"[{job_id}] YT automation complete")

    except Exception as e:
        logger.error(f"[{job_id}] Upload error: {e}\n{traceback.format_exc()}")
        write_status(job_id, {"state": "error", "error": str(e), "finished_at": now_utc_iso()})

# ---------- WordPress helpers ----------
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

# ---------- Async uploader (WordPress) ----------
def async_upload_to_wordpress(job_id, video_url, filename, title, alt_text, post_id):
    """
    Downloads from Bunny CDN and uploads to WordPress Media Library.
    Non-fatal on metadata update failure. Persists results to wp_status.json.
    """
    logger = job_logger(job_id)
    try:
        # 1) Download Bunny file (with % logs)
        temp_path = f"wp_{job_id}"
        logger.info(f"[WP {job_id}] job start file=\"{filename or 'video.mp4'}\"")
        streamed_download_to_file(logger, video_url, temp_path, f"[WP {job_id}]")

        # 2) Upload to WP /media (multipart, with % logs using LoggingFile)
        api_base = _wp_api_base()
        media_ep = f"{api_base}/media"
        guess, _ = mimetypes.guess_type(filename or "")
        mime = guess or "video/mp4"
        up_name = filename or f"video_{job_id}.mp4"

        file_size = os.path.getsize(temp_path)
        with open(temp_path, "rb") as fh:
            lf = LoggingFile(fh, file_size, logger, f"[WP {job_id}]", step_label="upload", log_step=10)
            files = {"file": (up_name, lf, mime)}
            headers = {"Content-Disposition": f'attachment; filename="{up_name}"'}
            logger.info(f"[WP {job_id}] POST {media_ep}")
            resp = requests.post(media_ep, files=files, headers=headers, auth=_wp_auth(), timeout=900)

        logger.info(f"[WP {job_id}] response {resp.status_code}")

        # Try to parse JSON; fall back to raw text
        resp_json = None
        resp_text_preview = None
        try:
            resp_json = resp.json()
        except Exception:
            resp_text_preview = (resp.text or "")[:1200]

        if not resp.ok:
            if resp_json:
                logger.error(f"[WP {job_id}] upload failed {resp.status_code}: {json.dumps(resp_json)[:1200]}")
            else:
                logger.error(f"[WP {job_id}] upload failed {resp.status_code}: {resp_text_preview}")
            resp.raise_for_status()

        media_json = resp_json if resp_json is not None else resp.json()
        attachment_id = media_json.get("id")
        source_url = media_json.get("source_url")
        logger.info(f"[WP {job_id}] upload complete id={attachment_id} url={source_url}")

        # 3) Optional metadata / attach to post (non-fatal)
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
                logger.info(f"[WP {job_id}] updating media metadata")
                pr = requests.post(meta_ep, json=patch, auth=_wp_auth(), timeout=120)
                if not pr.ok:
                    preview = None
                    try:
                        preview = json.dumps(pr.json())[:800]
                    except Exception:
                        preview = (pr.text or "")[:800]
                    logger.warning(f"[WP {job_id}] meta update failed {pr.status_code}: {preview}")
                else:
                    logger.info(f"[WP {job_id}] metadata updated")
            except Exception as e:
                logger.warning(f"[WP {job_id}] meta update exception: {e}")

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
                "finished_at": now_utc_iso(),
                "filename": up_name,
            },
        )
        logger.info(f"[WP {job_id}] automation complete")

    except Exception as e:
        logger.error(f"[WP {job_id}] Upload error: {e}\n{traceback.format_exc()}")
        wp_write_status(job_id, {"state": "error", "error": str(e), "finished_at": now_utc_iso()})

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
            "started_at": now_utc_iso(),
        },
    )

    app.logger.info(f"[{job_id}] received YT job for channel={channel_key}")
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
            "started_at": now_utc_iso(),
        },
    )

    app.logger.info(f"[WP {job_id}] received WP job")
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
