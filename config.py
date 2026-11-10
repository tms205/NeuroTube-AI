import os
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# ─────────────────────────────────────────────────────────────────────────────
#  YouTube AI Agent Studio — Configuration
#  Copy this file as-is. Fill in your API keys below (or use env vars).
#  All settings are documented. Change only what you need.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────
#  API Keys  (all free-tier)
# ─────────────────────────────────────────
GEMINI_API_KEY     = "dán bản API của bạn ở đây"  # required for Gemini AI text models
PEXELS_API_KEY     = "dán bản API của bạn ở đây"  # optional for free stock images
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "YOUR_ELEVENLABS_KEY")  # optional

# ─────────────────────────────────────────
#  Your Channel Identity
#  The more specific you are, the better the AI's topic suggestions.
# ─────────────────────────────────────────
CHANNEL_DESCRIPTION = """
A mind-bending science and technology channel in the style of Vsauce, Veritasium, and Kurzgesagt.
We focus heavily on deep, existential "What If" scenarios, the extreme future of Artificial Intelligence,
theoretical physics, and mind-expanding thought experiments.
Every video should explore the absolute limits of science, space, or tech, making the viewer question reality.
Target audience: highly curious thinkers who want their minds blown by deep scientific and philosophical dives.
"""
CHANNEL_NAME = "My AI Channel"   # shown on-screen and in upload metadata

# ─────────────────────────────────────────
#  Voice (Edge TTS — free, no API key)
#  Voices confirmed to emit word-level timing (needed for caption sync):
#    en-GB-RyanNeural     — British, deep, cinematic
#    en-US-AndrewNeural   — US, warm, authoritative  ← default
#    en-US-BrianNeural    — US, calm, documentary
#    en-US-GuyNeural      — US, clear, neutral
# ─────────────────────────────────────────
VOICE_ID    = "vi-VN-HoaiMyNeural"
VOICE_RATE  = "+0%"
VOICE_PITCH = "+0Hz"
VOICE_SMOOTH_PAUSES = True   # cap long silent gaps and normalize speech loudness
VOICE_MAX_PAUSE = 0.45       # seconds; short breaths remain untouched (0.2–0.8)

# ─────────────────────────────────────────
#  AI Model
#  Starting model for the fallback chain — the system auto-switches
#  through all models below if quota or errors are hit.
#  Change this in the GUI under Settings → AI Model, or here directly.
#  Settings lists text models from the Gemini API, including newer releases.
#  Availability, quotas and billing depend on your Google API project.
# ─────────────────────────────────────────
GEMINI_MODEL = "gemini-3.6-flash"

# ─────────────────────────────────────────
#  Video Dimensions
# ─────────────────────────────────────────
VIDEO_WIDTH  = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS    = 24

# YouTube Shorts dimensions (9:16 vertical)
SHORTS_WIDTH  = 1080
SHORTS_HEIGHT = 1920
SHORTS_FPS    = 30

# ─── Visual Style ───────────────────────
# Ken Burns effect: how much to zoom/pan each image
KB_ZOOM_START = 1.00    # starting scale (1.0 = no zoom)
KB_ZOOM_END   = 1.10    # ending scale   (1.1 = 10% zoom in)

# Image crossfade duration (seconds)
# 0.0 = instant cut  |  0.5 = snappy  |  0.7 = smooth  |  1.2 = dreamy
CROSSFADE_DURATION = 0.7

# Normal video B-roll cycling
BROLL_INTERVAL  = 10.0   # seconds each image stays on screen
BROLL_XFADE_DUR =  1.2   # crossfade duration (must be < BROLL_INTERVAL)

# Render quality preset (ffmpeg libx264)
# "ultrafast" = fastest/largest  |  "veryfast" = recommended  |  "medium" = best quality
RENDER_PRESET = "ultrafast"

# Overlay opacity: lower = more image visible but text harder to read (0–1)
OVERLAY_OPACITY = 0.62

# Colour palette — customise to match your brand
COLORS = {
    "background": (10,  10,  20),
    "overlay":    (0,   0,   0),
    "primary":    (99,  102, 241),   # indigo
    "accent":     (167, 139, 250),   # purple
    "highlight":  (251, 191,  36),   # amber
    "white":      (255, 255, 255),
    "light":      (199, 210, 254),   # indigo-200
    "success":    (52,  211, 153),
    "red":        (239,  68,  68),
}

# Fonts — add your own .ttf paths for best results; system falls back gracefully
FONT_PATHS = {
    "bold": [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/tahomabd.ttf",
    ],
    "regular": [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    ],
    "light":   [
        "C:/Windows/Fonts/segoeuil.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
}

# ─────────────────────────────────────────
#  Review Server
# ─────────────────────────────────────────
REVIEW_PORT = 5050

# ─────────────────────────────────────────
#  YouTube Upload
# ─────────────────────────────────────────
YOUTUBE_CLIENT_SECRET = os.path.join(
    PROJECT_DIR,
    "client_secret.json"
)

YOUTUBE_TOKEN_FILE = os.path.join(
    PROJECT_DIR,
    "youtube_token.pickle"
)  # OAuth credentials file (see SETUP.md)
YOUTUBE_SCOPES        = ["https://www.googleapis.com/auth/youtube.upload"]
VIDEO_CATEGORY_ID     = "27"       # 27 = Education
VIDEO_PRIVACY         = "public"   # "public" | "unlisted" | "private"

# ─────────────────────────────────────────
#  Background Music
#  Drop MP3/WAV files into the  music library/  folder.
#  Tracks are shuffled and looped automatically to match video length.
# ─────────────────────────────────────────
MUSIC_ENABLED     = True
MUSIC_VOLUME      = 0.12          # 0.0–1.0  (0.12 = subtle underscore)
MUSIC_LIBRARY_DIR = os.path.join(os.path.dirname(__file__), "music library")

# ─────────────────────────────────────────
#  Paths  (do not change unless you know what you're doing)
# ─────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
