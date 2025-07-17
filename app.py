from flask import Flask, request, jsonify
from youtube_upload import upload_to_youtube

app = Flask(__name__)

@app.route("/upload-to-youtube", methods=["POST"])
def upload_video():
    data = request.json
    video_url = data.get("video_url")
    title = data.get("title", "Untitled Video")
    description = data.get("description", "")
    privacy = data.get("privacy", "unlisted")
    bunny_delete_url = data.get("bunny_delete_url")

    if not video_url:
        return jsonify({"error": "Missing video_url"}), 400

    youtube_url = upload_to_youtube(video_url, title, description, privacy, bunny_delete_url)

    return jsonify({"youtube_url": youtube_url}), 200

@app.route("/", methods=["GET"])
def home():
    return "YouTube Uploader is live!", 200
