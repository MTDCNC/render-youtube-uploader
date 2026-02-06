# app.py — minimal, low-memory uploader (YouTube + WordPress) with simple 504 verification + structured JSON logs.

from flask import Flask, request, jsonify, send_from_directory
import os, sys, json, uuid, threading, time, mimetypes, logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

app = Flask(__name__)

# ---- ETG Product Scraper
from etg_routes import etg_bp
app.register_blueprint(etg_bp, url_prefix="/etg")


# ---- YouTube deps ----
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from zoneinfo import ZoneInfo  # Py3.9+
import re

#Import linkedIn image Processor functions
from image_processor import process_linkedin_image as process_linkedin_image_helper

#Import Product IMage Processor Functions
from image_processor import process_product_image as process_product_image_helper

# Flush logs immediately on Render
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

app = Flask(__name__)

# ---------------- tiny utils ----------------
def iso_now():
    return datetime.utcnow().isoformat() + "Z"

def read_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default()

def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())

def pjoin(*parts):
    return os.path.join(*parts)

# Ephemeral files (by design; no disk requirement)
YT_STATUS_FILE = "youtube_status.json"

# ---------------- structured logging helpers ----------------
def setup_logging():
    root = logging.getLogger()
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    root.setLevel(lvl)
    h = logging.StreamHandler()  # stdout -> visible in Render Logs
    h.setFormatter(logging.Formatter('%(message)s'))  # we emit JSON ourselves
    root.handlers = [h]

setup_logging()

def jlog(event, **fields):
    """Emit one JSON log line; searchable in Render. Always includes ts + event.
    Common fields you may pass: job_id, filename, pct, status, reason, youtube_url, attachment_id
    """
    try:
        payload = {"ts": iso_now(), "event": event, **fields}
        logging.getLogger("json").info(json.dumps(payload, ensure_ascii=False))
    except Exception as _:
        # Never let logging crash the worker
        pass

# ---------------- YouTube helpers ----------------
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

UK_TZ = ZoneInfo("Europe/London")

def parse_publish_at_uk(s: str) -> datetime:
    s = (s or "").strip()

    # If an explicit numeric offset is present (+HH:MM / -HH:MM), respect it.
    if re.search(r'[+-]\d{2}:\d{2}$', s):
        return datetime.fromisoformat(s.replace('Z', '+00:00'))

    # Otherwise (no offset OR trailing Z), treat as UK wall time.
    # Accept "YYYY-MM-DD HH:mm", "YYYY-MM-DDTHH:mm", and optional :ss
    s_clean = s.replace('T', ' ').rstrip('Z')
    if len(s_clean) >= 19:
        dt_naive = datetime.strptime(s_clean[:19], "%Y-%m-%d %H:%M:%S")
    else:
        dt_naive = datetime.strptime(s_clean[:16], "%Y-%m-%d %H:%M")
    return dt_naive.replace(tzinfo=UK_TZ)

def select_channel_by_location(location: str) -> str:
    """
    Priority: UK → US → Spain → Asia
    - Exact values come from Monday.com dropdowns.
    - Supports multi-select strings (comma/newline separated).
    - Default: UK if nothing relevant is selected.
    """
    text = (location or "").strip().lower()

    # Split on commas and newlines to support multi-select fields
    parts = [p.strip() for p in re.split(r'[,\n]+', text) if p.strip()]

    # Normalise to canonical tokens
    canon = set()
    for p in parts:
        if p == "united kingdom":
            canon.add("UK")
        elif p == "north america":
            canon.add("US")
        elif p == "north america (spanish)":
            canon.add("ES")     # route to Spain channel
        elif p == "asia":
            canon.add("Asia")
        # Europe, Middle East, South America → ignored (fall through to default)

    # Priority: UK → US → Spain → Asia
    if "UK" in canon:
        return "UK"
    if "US" in canon:
        return "US"
    if "ES" in canon:
        return "Spain"
    if "Asia" in canon:
        return "Asia"

    # Default if only Europe/Middle East/South America/empty/etc.
    return "UK"


def yt_service(channel_key: str):
    mapping = {
        "UK":   ("YT_UK_CLIENT_ID", "YT_UK_CLIENT_SECRET", "YT_UK_REFRESH_TOKEN"),
        "US":   ("YT_US_CLIENT_ID", "YT_US_CLIENT_SECRET", "YT_US_REFRESH_TOKEN"),
        "Spain": ("YT_ES_CLIENT_ID", "YT_ES_CLIENT_SECRET", "YT_ES_REFRESH_TOKEN"),
        "Asia": ("YT_ASIA_CLIENT_ID", "YT_ASIA_CLIENT_SECRET", "YT_ASIA_REFRESH_TOKEN"),
    }
    cid_key, csec_key, rtok_key = mapping[channel_key]
    creds = Credentials(
        token=None,
        refresh_token=os.environ[rtok_key],
        token_uri=os.environ.get("YOUTUBE_TOKEN_URI","https://oauth2.googleapis.com/token"),
        client_id=os.environ[cid_key],
        client_secret=os.environ[csec_key],
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        app.logger.error(f"[YT] OAuth refresh failed for {channel_key}: {e}")
        jlog("yt.oauth.error", channel=channel_key, reason=str(e))
        raise
    return build("youtube", "v3", credentials=creds)

# ---------------- Downloader with progress callback ----------------
def streamed_download(url: str, dest_path: str, log_prefix: str, chunk=1_048_576, on_progress=None):
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0")) or None
        next_pct, read = 0, 0
        with open(dest_path, "wb") as f:
            for c in r.iter_content(chunk):
                if not c: 
                    continue
                f.write(c)
                if total:
                    read += len(c)
                    pct = int(read*100/total)
                    if pct >= next_pct:
                        app.logger.info(f"{log_prefix} download {pct}%")
                        if on_progress:
                            try: on_progress(pct)
                            except Exception: pass
                        next_pct += 10
    app.logger.info(f"{log_prefix} download complete -> {dest_path}")

# ---------------- YouTube worker ----------------
def async_upload_to_youtube(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at=None):
    filename = None
    try:
        parsed = urlparse(video_url)
        filename = os.path.basename(parsed.path) or f"video_{job_id}.mp4"
        tmp = f"yt_{job_id}.mp4"

        app.logger.info(f"[{job_id}] YT start file=\"{filename}\" channel={channel_key}")
        jlog("yt.start", job_id=job_id, filename=filename, channel=channel_key, title=title)

        # Download from Bunny with JSON progress
        streamed_download(
            video_url, tmp, f"[{job_id}]",
            on_progress=lambda p: jlog("yt.download.progress", job_id=job_id, filename=filename, pct=p)
        )

        yt = yt_service(channel_key)
        tags = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]

        # Decide scheduling
        status_obj = {'privacyStatus': privacy, 'madeForKids': False}
        if publish_at:
            try:
                dt = parse_publish_at_uk(publish_at)  # UK wall time
                now_utc = datetime.now(timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
                if dt_utc > now_utc:
                    status_obj['privacyStatus'] = 'private'
                    status_obj['publishAt'] = dt_utc.isoformat().replace('+00:00', 'Z')
                    app.logger.info(f"[{job_id}] Scheduled (UK wall time) -> {status_obj['publishAt']}")
                    jlog("yt.schedule", job_id=job_id, filename=filename, publishAt=status_obj['publishAt'])
                else:
                    jlog("yt.schedule.skip", job_id=job_id, filename=filename, reason="publish_at_in_past")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Could not parse publish_at '{publish_at}': {e}")
                jlog("yt.schedule.error", job_id=job_id, filename=filename, reason=str(e))

        media = MediaFileUpload(tmp, mimetype="video/*", resumable=True, chunksize=8*1024*1024)
        req = yt.videos().insert(
            part="snippet,status",
            body={"snippet":{"title":title,"description":description,"tags":tags},"status":status_obj},
            media_body=media,
        )

        last = -5
        progress, status = None, None
        while status is None:
            progress, status = req.next_chunk()
            if progress:
                pct = int(progress.progress()*100)
                if pct - last >= 5:
                    app.logger.info(f"[{job_id}] YT upload {pct}%")
                    jlog("yt.upload.progress", job_id=job_id, filename=filename, pct=pct)
                    last = pct

        vid = status["id"]
        url = f"https://youtu.be/{vid}"
        app.logger.info(f"[{job_id}] YT done -> {url}")
        jlog("yt.done", job_id=job_id, filename=filename, video_id=vid, youtube_url=url)

        # Thumbnail (non-fatal)
        if thumbnail_url:
            try:
                thf = f"yt_thumb_{job_id}.jpg"
                streamed_download(thumbnail_url, thf, f"[{job_id}] thumb",
                                  on_progress=lambda p: jlog("yt.thumb.download.progress", job_id=job_id, pct=p))
                yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thf, mimetype="image/jpeg")).execute()
                try: os.remove(thf)
                except: pass
                app.logger.info(f"[{job_id}] YT thumbnail set")
                jlog("yt.thumb.done", job_id=job_id)
            except Exception as e:
                app.logger.warning(f"[{job_id}] YT thumbnail error: {e}")
                jlog("yt.thumb.error", job_id=job_id, reason=str(e))

        try:
            os.remove(tmp)
        except Exception:
            pass

        # Bunny delete is optional; non-fatal
        if bunny_delete_url:
            try:
                dr = requests.delete(bunny_delete_url, headers={"AccessKey": os.environ.get("BUNNY_API_KEY")})
                app.logger.info(f"[{job_id}] Bunny delete -> {dr.status_code}")
                if dr.status_code == 200:
                    jlog("bunny.delete.success", job_id=job_id)
                else:
                    jlog("bunny.delete.failed", job_id=job_id, status_code=dr.status_code, body=(dr.text or "")[:300])
            except Exception as e:
                app.logger.warning(f"[{job_id}] Bunny delete failed: {e}")
                jlog("bunny.delete.error", job_id=job_id, reason=str(e))

        # Write status file with success
        st = read_json(YT_STATUS_FILE, dict)
        st[job_id] = {"state":"completed","youtube_url":url,"finished_at":iso_now(),"channel":channel_key,"source_filename":filename}
        write_json(YT_STATUS_FILE, st)
        jlog("yt.final", job_id=job_id, filename=filename, status="success", youtube_url=url)

    except Exception as e:
        app.logger.error(f"[{job_id}] YT error: {e}")
        jlog("yt.error", job_id=job_id, filename=filename, reason=str(e))
        st = read_json(YT_STATUS_FILE, dict)
        st[job_id] = {"state":"error","error":str(e),"finished_at":iso_now()}
        write_json(YT_STATUS_FILE, st)

# ---------------- WordPress helpers (no local status file) ----------------
def wp_api_base():
    base = os.environ.get("WP_API_BASE")  # e.g. https://example.com/wp-json/wp/v2
    if not base:
        raise RuntimeError("Missing WP_API_BASE")
    return base.rstrip("/")

def wp_auth():
    u, p = os.environ.get("WP_USER"), os.environ.get("WP_APP_PASSWORD")
    if not u or not p:
        raise RuntimeError("Missing WP_USER or WP_APP_PASSWORD")
    return HTTPBasicAuth(u, p)

def filename_base(name: str) -> str:
    base = os.path.splitext(name or "")[0]
    return "-".join([s for s in base.replace("_","-").replace(" ","-").lower().split("-") if s])

def normalize_location_for_wordpress(location: str) -> str:
    """WP wants NA(Spanish) recorded as 'North America'."""
    return "North America" if (location or "").strip().lower() == "north america (spanish)" else (location or "")


# ---------------- WordPress worker (streaming, low RAM) ----------------
def async_upload_to_wordpress(job_id, video_url, filename, title, alt_text, post_id):
    """Download from Bunny and upload to WP Media. We DO NOT write local state; WP is the ledger."""
    tmp = None
    up_name = None
    try:
        up_name = filename or f"video_{job_id}.mp4"
        tmp = f"wp_{job_id}"
        app.logger.info(f"[WP {job_id}] start file=\"{up_name}\"")
        jlog("wp.start", job_id=job_id, filename=up_name, title=title)

        # 1) download from Bunny (streamed)
        streamed_download(video_url, tmp, f"[WP {job_id}]",
                          on_progress=lambda p: jlog("wp.download.progress", job_id=job_id, filename=up_name, pct=p))

        # 2) stream multipart to WP with a unique verify token in description
        token = f"job:{job_id}"
        api = wp_api_base()
        media_ep = f"{api}/media"
        guess, _ = mimetypes.guess_type(up_name)
        mime = guess or "video/mp4"

        def _progress(m: MultipartEncoderMonitor, last=[-10]):
            if not m.len:
                return
            pct = int(100 * m.bytes_read / m.len)
            if pct - last[0] >= 10:
                app.logger.info(f"[WP {job_id}] upload {pct}%")
                jlog("wp.upload.progress", job_id=job_id, filename=up_name, pct=pct)
                last[0] = pct

        with open(tmp, "rb") as fh:
            enc = MultipartEncoder(fields={
                "file": (up_name, fh, mime),
                "title": title or up_name,
                "description": f"Uploaded by automation ({token}) | location={normalize_location_for_wordpress(title or '')}"
            })
            mon = MultipartEncoderMonitor(enc, _progress)
            headers = {
                "Content-Type": mon.content_type,
                "Content-Disposition": f'attachment; filename="{up_name}"'
            }
            app.logger.info(f"[WP {job_id}] POST {media_ep}")
            t0 = time.time()
            resp = requests.post(media_ep, data=mon, headers=headers, auth=wp_auth(), timeout=900)
            app.logger.info(f"[WP {job_id}] response {resp.status_code} in {time.time()-t0:.1f}s")

        if resp.status_code == 201:
            data = resp.json()
            attach_id, src = data.get("id"), data.get("source_url")
            app.logger.info(f"[WP {job_id}] done id={attach_id} url={src}")
            jlog("wp.done", job_id=job_id, attachment_id=attach_id, url=src)

            # optional metadata patch (tiny request)
            if any([alt_text, post_id]):
                try:
                    patch = {}
                    if alt_text: patch["alt_text"] = alt_text
                    if post_id:  patch["post"] = int(post_id)
                    requests.post(f"{api}/media/{attach_id}", json=patch, auth=wp_auth(), timeout=120)
                except Exception as e:
                    app.logger.warning(f"[WP {job_id}] meta patch failed: {e}")
                    jlog("wp.meta.error", job_id=job_id, reason=str(e))

        elif resp.status_code == 504:
            # Proxy timeout — WordPress may still finish. We'll find it later via /wp-status-by-job.
            app.logger.warning(f"[WP {job_id}] 504 proxy timeout (will rely on /wp-status-by-job)")
            jlog("wp.timeout", job_id=job_id, reason="504 proxy timeout")

        else:
            try:
                preview = json.dumps(resp.json())[:600]
            except Exception:
                preview = (resp.text or "")[:600]
            app.logger.error(f"[WP {job_id}] upload failed {resp.status_code}: {preview}")
            jlog("wp.error", job_id=job_id, filename=up_name, status_code=resp.status_code, reason=preview)

    except Exception as e:
        app.logger.error(f"[WP {job_id}] error: {e}")
        jlog("wp.error", job_id=job_id, filename=up_name, reason=str(e))
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def async_upload_image_to_wordpress(job_id, file_url, filename, title, alt_text, post_id):
    """
    Dedicated image uploader that mimics Postman behaviour:
    - downloads from a public URL (Monday S3, Bunny, etc.)
    - uploads to WP via requests' native multipart (files=)
    - stamps job:<job_id> in the description so /wp-status-by-job can find it
    This does NOT touch the existing video uploader.
    """
    tmp = None
    up_name = None
    try:
        up_name = filename or f"image_{job_id}.png"
        tmp = f"wp_img_{job_id}"

        app.logger.info(f"[WPIMG {job_id}] start file=\"{up_name}\"")
        jlog("wpimg.start", job_id=job_id, filename=up_name, title=title)

        # 1) download from source URL (streamed)
        streamed_download(
            file_url,
            tmp,
            f"[WPIMG {job_id}]",
            on_progress=lambda p: jlog("wpimg.download.progress", job_id=job_id, filename=up_name, pct=p),
        )

        # 2) upload to WP using requests' native multipart (matches Postman)
        token = f"job:{job_id}"
        api = wp_api_base()
        media_ep = f"{api}/media"
        guess, _ = mimetypes.guess_type(up_name)
        mime = guess or "image/png"  # safe default for screenshots

        with open(tmp, "rb") as fh:
            files = {
                "file": (up_name, fh, mime),
            }
            data = {
                "title": title or up_name,
                "description": f"Uploaded by automation ({token})",
            }

            app.logger.info(f"[WPIMG {job_id}] POST {media_ep} (requests files=)")
            t0 = time.time()
            resp = requests.post(
                media_ep,
                files=files,
                data=data,
                auth=wp_auth(),
                timeout=900,
            )
            app.logger.info(f"[WPIMG {job_id}] response {resp.status_code} in {time.time()-t0:.1f}s")

        if resp.status_code == 201:
            data = resp.json()
            attach_id, src = data.get("id"), data.get("source_url")
            app.logger.info(f"[WPIMG {job_id}] done id={attach_id} url={src}")
            jlog("wpimg.done", job_id=job_id, attachment_id=attach_id, url=src)

            # Optional: tiny metadata patch for alt_text / post_id
            if any([alt_text, post_id]):
                try:
                    patch = {}
                    if alt_text:
                        patch["alt_text"] = alt_text
                    if post_id:
                        patch["post"] = int(post_id)
                    requests.post(
                        f"{api}/media/{attach_id}",
                        json=patch,
                        auth=wp_auth(),
                        timeout=120,
                    )
                except Exception as e:
                    app.logger.warning(f"[WPIMG {job_id}] meta patch failed: {e}")
                    jlog("wpimg.meta.error", job_id=job_id, reason=str(e))

        elif resp.status_code == 504:
            # Proxy timeout — WP may still finish; /wp-status-by-job will pick it up
            app.logger.warning(f"[WPIMG {job_id}] 504 proxy timeout (will rely on /wp-status-by-job)")
            jlog("wpimg.timeout", job_id=job_id, reason="504 proxy timeout")

        else:
            try:
                preview = json.dumps(resp.json())[:600]
            except Exception:
                preview = (resp.text or "")[:600]
            app.logger.error(f"[WPIMG {job_id}] upload failed {resp.status_code}: {preview}")
            jlog("wpimg.error", job_id=job_id, filename=up_name, status_code=resp.status_code, reason=preview)

    except Exception as e:
        app.logger.error(f"[WPIMG {job_id}] error: {e}")
        jlog("wpimg.error", job_id=job_id, filename=up_name, reason=str(e))
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
# ---------------- Cold start free render service for LinkedIn image processing ----------------
@app.route("/health", methods=["GET"])
def health_check():
    return {"status": "ok"}, 200

# ---------------- Lookup: find WP media by Render job_id (searches Description stamp) ----------------
@app.route('/wp-status-by-job', methods=['GET'])
def wp_status_by_job():
    """
    GET /wp-status-by-job?job_id=...
    Searches WP Media for an item whose description/title contains 'job:<job_id>'.
    Returns 200 with {attachment_id, source_url, filename} when found,
    or 202 while still processing.
    """
    job_id = (request.args.get('job_id') or '').strip()
    if not job_id:
        return jsonify({'error': 'Missing job_id parameter'}), 400

    token = f"job:{job_id}"
    try:
        api = wp_api_base()
        auth = wp_auth()

        def match(m):
            desc = ((m.get('description') or {}).get('rendered') or '')
            tit  = ((m.get('title') or {}).get('rendered') or '')
            cap  = ((m.get('caption') or {}).get('rendered') or '')
            hay  = " ".join([desc, tit, cap])
            return (token in hay) or (job_id in hay)

        # Pass 1: direct search (fast)
        r = requests.get(
            f"{api}/media",
            params={'search': job_id, 'per_page': 20, 'orderby': 'date', 'order': 'desc'},
            auth=auth, timeout=60
        )
        r.raise_for_status()
        for m in (r.json() if isinstance(r.json(), list) else []):
            if match(m):
                return jsonify({
                    'state': 'completed',
                    'attachment_id': m.get('id'),
                    'source_url': m.get('source_url'),
                    'filename': ((m.get('media_details') or {}).get('file')) or m.get('slug')
                }), 200

        # Pass 2: recent items (handles brief search-index lag)
        r2 = requests.get(
            f"{api}/media",
            params={'per_page': 20, 'orderby': 'date', 'order': 'desc'},
            auth=auth, timeout=60
        )
        r2.raise_for_status()
        for m in (r2.json() if isinstance(r2.json(), list) else []):
            if match(m):
                return jsonify({
                    'state': 'completed',
                    'attachment_id': m.get('id'),
                    'source_url': m.get('source_url'),
                    'filename': ((m.get('media_details') or {}).get('file')) or m.get('slug')
                }), 200

        return jsonify({'state': 'processing', 'note': 'not found yet'}), 202

    except Exception as e:
        return jsonify({'state': 'error', 'error': str(e)}), 500

@app.route("/process-linkedin-image", methods=["POST"])
def process_linkedin_image_route():
    data = request.get_json(force=True) or {}
    url = data.get("image_url")
    filename = data.get("filename") or "linkedin_image"

    if not url:
        return jsonify({"success": False, "error": "Missing 'url'"}), 400

    base_public_url = os.environ.get("PUBLIC_BASE_URL") or "https://render-youtube-uploader.onrender.com"

    try:
        result = process_linkedin_image_helper(
            url=url,
            filename=filename,
            base_public_url=base_public_url,
        )
        return jsonify(result), 200

    except Exception as e:
        app.logger.exception("Error processing LinkedIn image")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/process-product-image", methods=["POST"])
def process_product_image_route():
    data = request.get_json(force=True) or {}
    url = data.get("image_url")
    filename = data.get("filename") or "product_image"

    if not url:
        return jsonify({"success": False, "error": "Missing 'image_url'"}), 400

    base_public_url = os.environ.get("PUBLIC_BASE_URL") or "https://render-youtube-uploader.onrender.com"

    try:
        result = process_product_image_helper(
            url=url,
            filename=filename,
            base_public_url=base_public_url,
        )
        return jsonify(result), 200

    except Exception as e:
        app.logger.exception("Error processing product image")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- Routes (keep these) ----------------
@app.route("/upload-to-wordpress", methods=["POST"])
def upload_wp():
    d = request.json or {}
    video_url = d.get("video_url")
    if not video_url:
        return jsonify({"error":"Missing video_url"}), 400

    # NEW: optional incoming location from Monday.com; normalise for WP
    wp_location = normalize_location_for_wordpress(d.get("location"))

    job_id = str(uuid.uuid4())
    threading.Thread(
        target=async_upload_to_wordpress,
        args=(job_id, video_url, d.get("filename"), d.get("title"), d.get("alt_text"), d.get("post_id")),
        daemon=True
    ).start()
    return jsonify({"status":"processing","job_id":job_id}), 202

@app.route("/upload-image-to-wordpress", methods=["POST"])
def upload_wp_image():
    """
    Lightweight JSON API just for images/screenshots.

    Expected JSON body:
    {
      "file_url": "<public HTTPS URL to the image>",
      "filename": "EMO-2025-...-SS.png",
      "title": "EMO 2025 - ... -SS",
      "alt_text": "optional alt",
      "post_id": 12345          # optional, attach to post
    }
    """
    d = request.json or {}
    file_url = d.get("file_url") or d.get("image_url") or d.get("video_url")
    if not file_url:
        return jsonify({"error": "Missing file_url / image_url"}), 400

    job_id = str(uuid.uuid4())
    threading.Thread(
        target=async_upload_image_to_wordpress,
        args=(
            job_id,
            file_url,
            d.get("filename"),
            d.get("title"),
            d.get("alt_text"),
            d.get("post_id"),
        ),
        daemon=True,
    ).start()

    return jsonify({"status": "processing", "job_id": job_id}), 202

PROCESSED_DIR = os.environ.get("PROCESSED_IMAGES_DIR", "processed_images")

@app.route("/images/<path:filename>", methods=["GET"])
def serve_processed_image(filename):
    return send_from_directory(PROCESSED_DIR, filename)


# ---------------- YouTube routes ----------------
@app.route("/upload-to-youtube", methods=["POST"])
def upload_youtube():
    d = request.json or {}
    video_url = d.get("video_url")
    title = d.get("title")
    description = d.get("description")
    raw_tags = d.get("tags","")
    privacy = d.get("privacy","unlisted")
    thumbnail_url = d.get("thumbnail_url")
    bunny_delete_url = d.get("bunny_delete_url")
    channel_key = select_channel_by_location(d.get("location",""))
    publish_at = d.get("publish_at")

    if not all([video_url, title, description]):
        return jsonify({"error":"Missing video_url, title, or description"}), 400

    job_id = str(uuid.uuid4())
    st = read_json(YT_STATUS_FILE, dict)
    st[job_id] = {"state":"processing","channel":channel_key,"title":title,"started_at":iso_now()}
    write_json(YT_STATUS_FILE, st)

    jlog("yt.enqueued", job_id=job_id, title=title, channel=channel_key)

    threading.Thread(
        target=async_upload_to_youtube,
        args=(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at),
        daemon=True
    ).start()

    return jsonify({"status":"processing","job_id":job_id,"channel":channel_key}), 202

@app.route("/status-check", methods=["GET"])
def yt_status():
    job_id = request.args.get("job_id")
    if not job_id: return jsonify({"error":"Missing job_id parameter"}), 400
    data = read_json(YT_STATUS_FILE, dict).get(job_id)
    return (jsonify(data),200) if data else (jsonify({"error":"Not found"}),404)


@app.route("/", methods=["GET", "HEAD"])
def health():
    return "YouTube Uploader is live!", 200
