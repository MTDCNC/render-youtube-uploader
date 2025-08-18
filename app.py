# app.py — minimal, low-memory uploader (YouTube + WordPress) with simple 504 verification.

from flask import Flask, request, jsonify
import os, sys, json, uuid, threading, time, mimetypes
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

# ---- YouTube deps ----
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from zoneinfo import ZoneInfo  # Py3.9+

# Flush logs immediately on Render
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

app = Flask(__name__)

# --------- tiny utils ---------
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
WP_STATUS_FILE = "wp_status.json"

# --------- YouTube helpers ---------
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

TZ_BY_CHANNEL = {
    "UK": ZoneInfo("Europe/London"),
    "US": ZoneInfo("America/New_York"),   # tweak if needed
    "Asia": ZoneInfo("Asia/Kolkata"),     # tweak if needed
}

def select_channel_by_location(location: str) -> str:
    loc = (location or "").lower()
    if "united kingdom" in loc: return "UK"
    if "north america" in loc:  return "US"
    if "asia" in loc:           return "Asia"
    return "UK"

def yt_service(channel_key: str):
    mapping = {
        "UK":   ("YT_UK_CLIENT_ID", "YT_UK_CLIENT_SECRET", "YT_UK_REFRESH_TOKEN"),
        "US":   ("YT_US_CLIENT_ID", "YT_US_CLIENT_SECRET", "YT_US_REFRESH_TOKEN"),
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
        raise
    return build("youtube", "v3", credentials=creds)

def streamed_download(url: str, dest_path: str, log_prefix: str, chunk=1_048_576):
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0")) or None
        next_pct, read = 0, 0
        with open(dest_path, "wb") as f:
            for c in r.iter_content(chunk):
                if not c: continue
                f.write(c)
                if total:
                    read += len(c)
                    pct = int(read*100/total)
                    if pct >= next_pct:
                        app.logger.info(f"{log_prefix} download {pct}%")
                        next_pct += 10
    app.logger.info(f"{log_prefix} download complete -> {dest_path}")

# --------- YouTube worker ---------
def async_upload_to_youtube(job_id, video_url, title, description, privacy, thumbnail_url, bunny_delete_url, raw_tags, channel_key, publish_at=None):
    try:
        parsed = urlparse(video_url)
        filename = os.path.basename(parsed.path) or f"video_{job_id}.mp4"
        tmp = f"yt_{job_id}.mp4"

        app.logger.info(f"[{job_id}] YT start file=\"{filename}\" channel={channel_key}")
        streamed_download(video_url, tmp, f"[{job_id}]")

        yt = yt_service(channel_key)
        tags = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]
        # Decide scheduling
        status_obj = {'privacyStatus': privacy, 'madeForKids': False}
        
        if publish_at:
            try:
                # Cases:
                # - RFC3339 with Z/offset -> parse as-is (aware)
                # - "YYYY-MM-DD HH:mm" (no offset) -> treat as local time for channel
                if 'T' in publish_at:
                    dt = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
                else:
                    local_tz = TZ_BY_CHANNEL.get(channel_key, ZoneInfo("UTC"))
                    naive = datetime.strptime(publish_at, "%Y-%m-%d %H:%M")
                    dt = naive.replace(tzinfo=local_tz)
        
                now_utc = datetime.now(timezone.utc)
        
                if dt > now_utc.astimezone(dt.tzinfo):
                    status_obj['privacyStatus'] = 'private'
                    status_obj['publishAt'] = dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                    app.logger.info(f"[{job_id}] Scheduled for future publish at {status_obj['publishAt']}")
                else:
                    app.logger.info(f"[{job_id}] publish_at is in the past — publishing immediately.")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Could not parse publish_at '{publish_at}': {e}")

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
                    last = pct

        vid = status["id"]
        url = f"https://youtu.be/{vid}"
        app.logger.info(f"[{job_id}] YT done -> {url}")

        if thumbnail_url:
            try:
                thf = f"yt_thumb_{job_id}.jpg"
                streamed_download(thumbnail_url, thf, f"[{job_id}] thumb")
                yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thf, mimetype="image/jpeg")).execute()
                try: os.remove(thf)
                except: pass
                app.logger.info(f"[{job_id}] YT thumbnail set")
            except Exception as e:
                app.logger.warning(f"[{job_id}] YT thumbnail error: {e}")

        try: os.remove(tmp)
        except: pass

        # Bunny delete is optional; non-fatal
        if bunny_delete_url:
            try:
                dr = requests.delete(bunny_delete_url, headers={"AccessKey": os.environ.get("BUNNY_API_KEY")})
                app.logger.info(f"[{job_id}] Bunny delete -> {dr.status_code}")
            except Exception as e:
                app.logger.warning(f"[{job_id}] Bunny delete failed: {e}")

        st = read_json(YT_STATUS_FILE, dict)
        st[job_id] = {"state":"completed","youtube_url":url,"finished_at":iso_now(),"channel":channel_key,"source_filename":filename}
        write_json(YT_STATUS_FILE, st)

    except Exception as e:
        app.logger.error(f"[{job_id}] YT error: {e}")
        st = read_json(YT_STATUS_FILE, dict)
        st[job_id] = {"state":"error","error":str(e),"finished_at":iso_now()}
        write_json(YT_STATUS_FILE, st)

# --------- WordPress helpers ---------
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

# --------- WordPress worker (streaming, low RAM) ---------
def async_upload_to_wordpress(job_id, video_url, filename, title, alt_text, post_id):
    try:
        up_name = filename or f"video_{job_id}.mp4"
        tmp = f"wp_{job_id}"
        app.logger.info(f"[WP {job_id}] start file=\"{up_name}\"")

        # 1) download from Bunny (streamed)
        streamed_download(video_url, tmp, f"[WP {job_id}]")

        # 2) stream multipart to WP with a unique verify token in description
        token = f"job:{job_id}"
        api = wp_api_base()
        media_ep = f"{api}/media"
        guess, _ = mimetypes.guess_type(up_name)
        mime = guess or "video/mp4"

        def _progress(m: MultipartEncoderMonitor, last=[-10]):
            if not m.len: return
            pct = int(100 * m.bytes_read / m.len)
            if pct - last[0] >= 10:
                app.logger.info(f"[WP {job_id}] upload {pct}%")
                last[0] = pct

        with open(tmp, "rb") as fh:
            enc = MultipartEncoder(fields={
                "file": (up_name, fh, mime),
                "title": title or up_name,
                "description": f"Uploaded by automation ({token})",
                # alt_text / post can be set after creation to keep payload tiny
            })
            mon = MultipartEncoderMonitor(enc, _progress)
            headers = {"Content-Type": mon.content_type,
                       "Content-Disposition": f'attachment; filename="{up_name}"'}
            app.logger.info(f"[WP {job_id}] POST {media_ep}")
            t0 = time.time()
            resp = requests.post(media_ep, data=mon, headers=headers, auth=wp_auth(), timeout=900)
            elapsed = time.time() - t0

        app.logger.info(f"[WP {job_id}] response {resp.status_code} in {elapsed:.1f}s")

        # 3) handle response
        if resp.status_code == 201:
            data = resp.json()
            attach_id, src = data.get("id"), data.get("source_url")
            app.logger.info(f"[WP {job_id}] done id={attach_id} url={src}")

            # optional metadata patch (tiny request)
            if any([alt_text, post_id]):
                try:
                    patch = {}
                    if alt_text: patch["alt_text"] = alt_text
                    if post_id:  patch["post"] = int(post_id)
                    requests.post(f"{api}/media/{attach_id}", json=patch, auth=wp_auth(), timeout=120)
                except Exception as e:
                    app.logger.warning(f"[WP {job_id}] meta patch failed: {e}")

            st = read_json(WP_STATUS_FILE, dict)
            st[job_id] = {"state":"completed","attachment_id":attach_id,"source_url":src,
                          "finished_at":iso_now(),"filename":up_name}
            write_json(WP_STATUS_FILE, st)

        elif resp.status_code == 504:
            # proxy timeout – WP may still be processing.
            app.logger.warning(f"[WP {job_id}] 504 proxy timeout; defer verify to /wp-verify")
            st = read_json(WP_STATUS_FILE, dict)
            st[job_id] = {
                "state":"processing",
                "note":"proxy_timeout_504",
                "verify_token": token,
                "filename": up_name,
                "filename_base": filename_base(up_name),
                "title": title or up_name,
                "started_at": iso_now(),
            }
            write_json(WP_STATUS_FILE, st)

        else:
            try:
                preview = json.dumps(resp.json())[:1200]
            except Exception:
                preview = (resp.text or "")[:1200]
            app.logger.error(f"[WP {job_id}] upload failed {resp.status_code}: {preview}")
            st = read_json(WP_STATUS_FILE, dict)
            st[job_id] = {"state":"error","error":f"{resp.status_code}: {preview}","finished_at":iso_now()}
            write_json(WP_STATUS_FILE, st)

    except Exception as e:
        app.logger.error(f"[WP {job_id}] error: {e}")
        st = read_json(WP_STATUS_FILE, dict)
        st[job_id] = {"state":"error","error":str(e),"finished_at":iso_now()}
        write_json(WP_STATUS_FILE, st)
    finally:
        try: os.remove(tmp)
        except Exception: pass

# --------- Minimal verify (Zap calls this once after its Delay) ---------
def _wp_find_by_token_or_name(token: str, name_base: str):
    """Single lookup: prefer the unique token, fall back to recent scan by filename base."""
    api = wp_api_base()
    auth = wp_auth()

    # 1) token search (fast & unique)
    try:
        r = requests.get(f"{api}/media", params={"search": token, "per_page": 25, "orderby":"id","order":"desc"}, auth=auth, timeout=60)
        if r.ok:
            items = r.json()
            for it in items:
                desc = (it.get("description",{}) or {}).get("rendered","").lower()
                tit  = (it.get("title",{}) or {}).get("rendered","").lower()
                if token in desc or token in tit:
                    return it.get("id"), it.get("source_url")
    except Exception:
        pass

    # 2) recent items by id desc, match filename base in URL (no collisions with token)
    try:
        r = requests.get(f"{api}/media", params={"per_page": 50, "orderby":"id","order":"desc"}, auth=auth, timeout=60)
        if r.ok:
            for it in r.json():
                url = (it.get("source_url") or "").lower()
                if name_base and name_base in url.lower():
                    return it.get("id"), it.get("source_url")
    except Exception:
        pass

    return None, None

# --------- Routes ---------
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

@app.route("/upload-to-wordpress", methods=["POST"])
def upload_wp():
    d = request.json or {}
    video_url = d.get("video_url")
    if not video_url:
        return jsonify({"error":"Missing video_url"}), 400

    job_id = str(uuid.uuid4())
    st = read_json(WP_STATUS_FILE, dict)
    st[job_id] = {"state":"processing","title": d.get("title"), "started_at": iso_now()}
    write_json(WP_STATUS_FILE, st)

    threading.Thread(
        target=async_upload_to_wordpress,
        args=(job_id, video_url, d.get("filename"), d.get("title"), d.get("alt_text"), d.get("post_id")),
        daemon=True
    ).start()

    return jsonify({"status":"processing","job_id":job_id}), 202

@app.route("/wp-status", methods=["GET"])
def wp_status():
    job_id = request.args.get("job_id")
    if not job_id: return jsonify({"error":"Missing job_id parameter"}), 400
    data = read_json(WP_STATUS_FILE, dict).get(job_id)
    return (jsonify(data),200) if data else (jsonify({"error":"Not found"}),404)

@app.route("/wp-verify", methods=["GET"])
def wp_verify():
    job_id = request.args.get("job_id")
    if not job_id: return jsonify({"error":"Missing job_id parameter"}), 400

    st_all = read_json(WP_STATUS_FILE, dict)
    entry = st_all.get(job_id)
    if not entry:
        return jsonify({"error":"Not found"}), 404
    if entry.get("state") == "completed":
        return jsonify(entry), 200

    token = entry.get("verify_token") or f"job:{job_id}"
    base  = entry.get("filename_base") or filename_base(entry.get("filename",""))
    attach_id, url = _wp_find_by_token_or_name(token, base)

    if attach_id and url:
        entry.update({"state":"completed","attachment_id":attach_id,"source_url":url,"finished_at":iso_now(),"verified_via":"token_or_name"})
        st_all[job_id] = entry
        write_json(WP_STATUS_FILE, st_all)
        return jsonify(entry), 200

    # still processing
    entry.update({"state":"processing","note":"verify_pending","last_check":iso_now()})
    st_all[job_id] = entry
    write_json(WP_STATUS_FILE, st_all)
    return jsonify(entry), 200

@app.route("/", methods=["GET", "HEAD"])
def health():
    return "YouTube Uploader is live!", 200
