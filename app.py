# ✅ FINAL app.py with status-check

from flask import Flask, request, jsonify
from youtube_upload import upload_to_youtube
import json
import os

app = Flask(__name__)

STATUS_FILE = "status.json"


def save_status(title, youtube_url):
    status = {}
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            status = json.load(f)
    status[title] = youtube_url
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f)


def get_status(title):
    if not os.path.exists(STATUS_FILE):
        return None
    with open(STATUS_FILE, "r") as f:
        status = json.load(f)
    return status.get(title)


@app.route("/upload-to-youtube", methods=["POST"])
def upload_video():
    data = request.json
    video_url = data.get("video_url")
    title = data.get("title")
    description = data.get("description")
    privacy = data.get("privacy", "unlisted")
    bunny_delete_url = data.get("bunny_delete_url")
    thumbnail_url = data.get("thumbnail_url")

    if not video_url or not title or not description:
        return jsonify({"error": "Missing required fields (video_url, title, description)"}), 400

    try:
        youtube_url = upload_to_youtube(
            video_url, title, description, privacy,
            bunny_delete_url, thumbnail_url
        )
        save_status(title, youtube_url)
        return jsonify({"youtube_url": youtube_url}), 200

    except Exception as e:
        print(f"❌ Fatal Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/status-check", methods=["GET"])
def status_check():
    title = request.args.get("title")
    if not title:
        return jsonify({"error": "Missing title parameter"}), 400
    youtube_url = get_status(title)
    if youtube_url:
        return jsonify({"youtube_url": youtube_url}), 200
    else:
        return jsonify({"error": "Status not found"}), 404


@app.route("/", methods=["GET"])
def home():
    return "YouTube Uploader is live!", 200
