# Render YouTube Uploader

## Overview
A minimal Flask app for uploading videos directly to YouTube via the YouTube Data API.

### Endpoints
- `POST /upload-to-youtube`

### Environment Variables
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

### Deploy on Render
- Connect GitHub repository.
- Use Gunicorn start command:
```
gunicorn app:app --timeout 900
```

### Example POST Body
```json
{
  "video_url": "https://link-to-your-video.mp4",
  "title": "My Video Title",
  "description": "Video Description",
  "privacy": "unlisted"
}
```