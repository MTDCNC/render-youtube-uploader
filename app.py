from flask import Flask, request, jsonify
from youtube_upload import upload_to_youtube

app = Flask(__name__)

@app.route("/upload-to-youtube", methods=["POST"])
def upload_video():
    data = request.json
    video_url = data.get("video_url")
    title = data.get("title")
    description = data.get("description")
    privacy = data.get("privacy", "unlisted")
    bunny_delete_url = data.get("bunny_delete_url")
    thumbnail_url = data.get("thumbnail_url")

    # ✅ Must have video_url, title, description
    if not video_url or not title or not description:
        return jsonify({"error": "Missing required fields (video_url, title, description)"}), 400

    youtube_url = upload_to_youtube(
        video_url, title, description, privacy, bunny_delete_url, thumbnail_url
    )

    return jsonify({"youtube_url": youtube_url}), 200

@app.route("/", methods=["GET"])
def home():
    return "YouTube Uploader is live!", 200
