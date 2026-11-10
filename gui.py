"""
YouTube AI Agent — Desktop GUI
Run:  python gui.py
Open: http://localhost:7070

Replaces pipeline.py + review/app.py entirely.
One page controls everything: generate, review, edit, upload.
"""
import os, sys, json, queue, threading, time, io, pickle, re, math, textwrap
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# Module-level imports from agents (sys.path is set above)
from agents.researcher import load_banned_topics, save_banned_topics, validate_topic

from flask import Flask, Response, jsonify, request, send_file, abort

import config

# ── Lazy-import pipeline modules so GUI loads even if deps missing ──────────
def _import(mod):
    try:
        return __import__(mod, fromlist=["_"])
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════════════

_log_queue  = queue.Queue()
_stop_event = threading.Event()   # set() to request pipeline stop

_state = {
    "running":        False,
    "stop_requested": False,
    "video_type":     "normal",
    "stages": {
        "research":  "pending",   # pending | running | done | error
        "script":    "pending",
        "images":    "pending",
        "narration": "pending",
        "video":     "pending",
    },
    "video_path":   None,
    "thumb_path":   None,
    "audio_path":   None,
    "image_map":    {},
    "script":       None,
    "research":     None,
    "yt_url":       None,
    "error":        None,
    "upload_pct":   0,
    "uploading":    False,
}


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class _Capture:
    """Redirect stdout into the log queue so pipeline prints appear in GUI."""
    def __init__(self, orig): self._orig = orig
    def write(self, t):
        if t.strip(): _log_queue.put(("log", t.rstrip()))
        self._orig.write(t)
    def flush(self): self._orig.flush()

def _log(msg, level="info"):
    _log_queue.put(("log", f"[{level.upper()}] {msg}"))

def _stage(name, status):
    _state["stages"][name] = status
    _log_queue.put(("stage", f"{name}:{status}"))


# ══════════════════════════════════════════════════════════════════════════════
#  THUMBNAIL GENERATOR  (built-in — no separate file needed)
# ══════════════════════════════════════════════════════════════════════════════

def _make_thumbnail(script, image_map, output_dir):
    from PIL import Image, ImageDraw, ImageFont

    TW, TH = 1280, 720
    title = script.get("title", "")
    C = config.COLORS

    def _font(variant, size):
        for p in config.FONT_PATHS.get(variant, config.FONT_PATHS["regular"]):
            try: return ImageFont.truetype(p, size)
            except: pass
        return ImageFont.load_default()

    def _wrap(draw, text, font, max_w):
        words, lines, cur = text.split(), [], []
        for w in words:
            test = " ".join(cur + [w])
            if draw.textbbox((0,0), test, font=font)[2] > max_w and cur:
                lines.append(" ".join(cur)); cur = [w]
            else: cur.append(w)
        if cur: lines.append(" ".join(cur))
        return lines or [text]

    def _shadow(draw, xy, text, font, fill, r=5):
        x, y = xy
        for dx in range(-r, r+1, 2):
            for dy in range(-r, r+1, 2):
                if abs(dx)+abs(dy) >= r:
                    draw.text((x+dx, y+dy), text, font=font, fill=(0,0,0))
        draw.text((x,y), text, font=font, fill=fill)

    # Background
    # FIX: For Shorts, image_map values are lists of paths. Unwrap to a single path.
    def _first_path(v):
        for path in v if isinstance(v, list) else [v]:
            if not path:
                continue
            if Path(path).suffix.lower() in (".mp4", ".mov", ".webm"):
                path = str(Path(path).with_suffix(".poster.jpg"))
            if os.path.isfile(path):
                return path
        return None

    bg_path = _first_path(image_map.get(1)) or next(
        (_first_path(v) for v in image_map.values() if v), None
    )
    if bg_path and os.path.exists(bg_path):
        bg = Image.open(bg_path).convert("RGB")
        bw, bh = bg.size
        scale = max(TW/bw, TH/bh)
        bg = bg.resize((int(bw*scale), int(bh*scale)), Image.LANCZOS)
        nw, nh = bg.size
        bg = bg.crop(((nw-TW)//2, (nh-TH)//2, (nw-TW)//2+TW, (nh-TH)//2+TH))
    else:
        bg = Image.new("RGB", (TW, TH), C["background"])

    # Dark overlay
    ov = Image.new("RGBA", (TW, TH), (0,0,0,0))
    od = ImageDraw.Draw(ov)
    for x in range(TW):
        od.line([(x,0),(x,TH)], fill=(0,0,10, int(190*(1-x/TW*0.45))))
    od.rectangle([0, TH-80, TW, TH], fill=(0,0,0,185))
    bg = Image.alpha_composite(bg.convert("RGBA"), ov).convert("RGB")
    draw = ImageDraw.Draw(bg)

    # Accent bar
    draw.rectangle([60, 100, 700, 108], fill=C["primary"])

    # Title
    font_big = _font("bold", 92)
    font_sm  = _font("bold", 70)
    lines = _wrap(draw, title.upper(), font_big, TW-140)
    if len(lines) > 2: font_big = font_sm; lines = _wrap(draw, title.upper(), font_big, TW-140)
    lines = lines[:2]

    for i, line in enumerate(lines):
        words = line.split(" ", 1)
        fw = words[0]; rest = words[1] if len(words) > 1 else ""
        fw_w = draw.textbbox((0,0), fw+" ", font=font_big)[2]
        _shadow(draw, (60, 130+i*108), fw, font_big, fill=C["highlight"], r=6)
        if rest: _shadow(draw, (60+fw_w, 130+i*108), rest, font_big, fill=C["white"], r=6)

    # Channel name
    _shadow(draw, (60, TH-60), config.CHANNEL_NAME.upper(), _font("bold", 36), fill=C["light"], r=3)

    # NEW VIDEO badge
    fb = _font("bold", 28)
    bt = "NEW VIDEO"
    bw2 = draw.textbbox((0,0), bt, font=fb)[2]
    bx = TW - bw2 - 80
    draw.rounded_rectangle([bx-16, 50, bx+bw2+16, 102], radius=8, fill=C["primary"])
    draw.text((bx, 58), bt, font=fb, fill=C["white"])

    out = os.path.join(output_dir, "thumbnail.jpg")
    bg.save(out, "JPEG", quality=95)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  DESCRIPTION / CHAPTERS BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _ts(secs):
    h, m, s = secs//3600, (secs%3600)//60, secs%60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _read_media_report(script: dict) -> dict:
    path = Path(config.OUTPUT_DIR, "media_manifest.json")
    if path.exists():
        try:
            from video.stock import _media_fingerprint
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("script_hash") == _media_fingerprint(script):
                return report
        except (OSError, ValueError, TypeError):
            pass
    return {}


def _build_description(script, research, video_type: str = "normal"):
    """Use the final script and measured chapters, not an unedited research draft."""
    is_shorts = video_type == "shorts" or script.get("video_type") == "shorts"
    lines = []
    if research.get("hook_question"):
        lines += [research["hook_question"], ""]
    if script.get("description"):
        lines += [script["description"], ""]
    if not is_shorts:
        timing_path = Path(config.OUTPUT_DIR, "narration_timing.json")
        try:
            from video.narrator import _script_fingerprint
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            spans = timing.get("sections", [])
            if (timing.get("script_hash") == _script_fingerprint(script) and len(spans) >= 3
                    and len(spans) == len(script.get("sections", [])) and spans[0]["start"] == 0
                    and all(span["end"] - span["start"] >= 10 for span in spans)):
                lines += ["NỘI DUNG VIDEO"]
                for span, section in zip(spans, script["sections"]):
                    seconds = int(span["start"])
                    stamp = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"
                    lines.append(f"{stamp} {section.get('title', '')}")
                lines.append("")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    highlights = list(dict.fromkeys(point for section in script.get("sections", [])
                                    for point in section.get("bullet_points", []) if point))
    if highlights:
        lines += ["Trong video này:"] + [f"• {point}" for point in highlights[:6]] + [""]
    lines += ["Chia sẻ góc nhìn của bạn ở phần bình luận. Theo dõi kênh để xem thêm nội dung cùng chủ đề.", ""]
    media = _read_media_report(script)
    if media.get("assets"):
        creators = list(dict.fromkeys(asset["creator"] for asset in media["assets"] if asset.get("creator")))
        lines += ["Tư liệu minh họa: Pexels — https://www.pexels.com"]
        if creators:
            lines.append("Tác giả: " + ", ".join(creators))
        lines.append("")
    tags = script.get("tags", [])
    if tags:
        lines.append(" ".join(f"#{tag.replace(' ', '')}" for tag in tags[:8]))
    if is_shorts:
        lines.append("#Shorts #YouTubeShorts")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE RUNNER  (runs in background thread)
# ══════════════════════════════════════════════════════════════════════════════

def _run_pipeline(steps: list, topic_override: str = "", video_type: str = "normal", focus_angle: str = ""):
    orig_stdout = sys.stdout
    sys.stdout = _Capture(orig_stdout)
    _state["running"] = True
    _state["error"]   = None
    _state["stop_requested"] = False
    _stop_event.clear()

    def _check_stop(after_stage: str):
        """Raise if user clicked Stop."""
        if _stop_event.is_set():
            raise InterruptedError(f"Stopped by user after {after_stage}")

    try:
        Path(config.OUTPUT_DIR).mkdir(exist_ok=True)
        Path(config.IMAGES_DIR).mkdir(exist_ok=True)

        from agents.researcher   import research_topic
        from agents.scriptwriter import write_script
        from video.narrator      import generate_narration
        from video.stock         import download_images, load_saved_images
        from video.creator       import create_video
        from video.music         import mix_music_with_narration

        _log(f"AI model đã chọn: {config.GEMINI_MODEL}")

        # ── Research ──────────────────────────────────────────
        if "research" in steps:
            _stage("research", "running")
            research = research_topic(
                channel_description = config.CHANNEL_DESCRIPTION,
                topic_override      = topic_override,
                focus_angle         = focus_angle,
                video_type          = video_type,
            )
            _state["research"] = research
            with open(f"{config.OUTPUT_DIR}/research.json", "w", encoding="utf-8") as f:
                json.dump(research, f, ensure_ascii=False, indent=2)
            _stage("research", "done")
            _log(f"Topic: {research['video_title']}")
        else:
            _state["research"] = None
            try:
                with open(f"{config.OUTPUT_DIR}/research.json", encoding="utf-8") as f:
                    _state["research"] = json.load(f)
            except: pass

        _check_stop("research")
        research = _state["research"]
        if not research: raise ValueError("No research data — run Research step first.")
        validate_topic(research, topic_override)

        # ── Script ────────────────────────────────────────────
        if "script" in steps:
            _stage("script", "running")
            _log(f"Generating {'Shorts' if video_type == 'shorts' else 'normal'} script…")
            script = write_script(research, video_type=video_type)
            _state["script"] = script
            with open(f"{config.OUTPUT_DIR}/script.json", "w", encoding="utf-8") as f:
                json.dump(script, f, ensure_ascii=False, indent=2)
            _stage("script", "done")
            _log(f"Script: {len(script['sections'])} sections")
        else:
            _state["script"] = None
            try:
                with open(f"{config.OUTPUT_DIR}/script.json", encoding="utf-8") as f:
                    _state["script"] = json.load(f)
            except: pass

        _check_stop("script")
        script = _state["script"]
        if not script: raise ValueError("No script data — run Script step first.")
        if script.get("topic") or topic_override:
            validate_topic(script, research.get("requested_topic") or research["topic"])
        if script.get("video_type", video_type) != video_type:
            raise ValueError("Kịch bản cũ thuộc loại video khác. Chạy lại Script cho định dạng đã chọn.")

        # ── Images ────────────────────────────────────────────
        if "images" in steps:
            _stage("images", "running")
            image_map = download_images(script, config.OUTPUT_DIR)
            _state["image_map"] = image_map
            _stage("images", "done")
        else:
            image_map = load_saved_images(script, config.OUTPUT_DIR)
            _state["image_map"] = image_map

        _check_stop("images")

        # ── Narration ─────────────────────────────────────────
        if "narration" in steps:
            _stage("narration", "running")
            audio_path = generate_narration(script, config.OUTPUT_DIR)
            _state["audio_path"] = audio_path
            _stage("narration", "done")
        else:
            ap = os.path.join(config.OUTPUT_DIR, "narration.mp3")
            _state["audio_path"] = ap if os.path.exists(ap) else None

        if not _state["audio_path"] or not os.path.exists(_state["audio_path"]):
            raise FileNotFoundError("narration.mp3 missing — run Narration step.")

        _check_stop("narration")

        # ── Music  (optional, non-blocking) ───────────────────
        final_audio_path = _state["audio_path"]
        if config.MUSIC_ENABLED and "music" in steps:
            _log("Mixing background music from library…")
            mixed_path = os.path.join(config.OUTPUT_DIR, "narration_mixed.mp3")
            final_audio_path = mix_music_with_narration(
                narration_path = _state["audio_path"],
                output_path    = mixed_path,
                music_volume   = getattr(config, "MUSIC_VOLUME", 0.12),
                library_dir    = getattr(config, "MUSIC_LIBRARY_DIR", ""),
            )
            _log("Audio ready (narration + music)")

        _check_stop("music")

        # ── Thumbnail ─────────────────────────────────────────
        _log("Generating thumbnail…")
        try:
            image_map_int = {int(k): v for k, v in _state["image_map"].items()}
            thumb = _make_thumbnail(script, image_map_int, config.OUTPUT_DIR)
            _state["thumb_path"] = thumb
            _log(f"Thumbnail saved: {thumb}")
        except Exception as e:
            _log(f"Thumbnail failed (non-fatal): {e}", "warn")

        _check_stop("thumbnail")

        # ── Video ─────────────────────────────────────────────
        if "video" in steps:
            _stage("video", "running")
            image_map_int = {int(k): v for k, v in _state["image_map"].items()}
            video_path = create_video(
                script, final_audio_path, config.OUTPUT_DIR, image_map_int,
                video_type=video_type
            )
            _state["video_path"] = video_path
            _stage("video", "done")
        else:
            vp_short = os.path.join(config.OUTPUT_DIR, "final_short.mp4")
            vp_norm  = os.path.join(config.OUTPUT_DIR, "final_video.mp4")
            vp = vp_short if os.path.exists(vp_short) else vp_norm
            _state["video_path"] = vp if os.path.exists(vp) else None

        # ── Build description ─────────────────────────────────
        if _state["research"] and _state["script"]:
            _state["description"] = _build_description(
                _state["script"], _state["research"], video_type=video_type)

        _log("Pipeline complete!", "info")
        _log_queue.put(("done", ""))

    except InterruptedError as e:
        _log(f"⏹ {e}", "warn")
        for s in _state["stages"]:
            if _state["stages"][s] == "running":
                _state["stages"][s] = "pending"
        _log_queue.put(("stopped", ""))

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        _state["error"] = str(e)
        for s in _state["stages"]:
            if _state["stages"][s] == "running":
                _state["stages"][s] = "error"
        _log(f"ERROR: {e}", "error")
        _log_queue.put(("error", str(e)))
    finally:
        _state["running"] = False
        sys.stdout = orig_stdout


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__, template_folder="gui_templates")


@app.route("/")
def index():
    with open(os.path.join(ROOT, "gui_templates", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/api/startup-status")
def get_startup_status():
    try:
        with open(os.path.join(ROOT, "output", "startup_report.json"), encoding="utf-8") as source:
            return jsonify(json.load(source))
    except (OSError, ValueError):
        return jsonify({"findings": []})


# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route("/api/stream")
def stream():
    def gen():
        while True:
            try:
                kind, msg = _log_queue.get(timeout=30)
                data = json.dumps({"kind": kind, "msg": msg})
                yield f"data: {data}\n\n"
                if kind in ("done", "error", "stopped"):
                    break
            except queue.Empty:
                yield "data: {\"kind\":\"ping\"}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── Pipeline control ──────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def run_pipeline():
    if _state["running"]:
        return jsonify({"ok": False, "error": "Pipeline already running"})
    body       = request.get_json(force=True) or {}
    from agents.gemini_client import normalize_model
    try:
        model = normalize_model(body.get("gemini_model", config.GEMINI_MODEL))
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    steps       = body.get("steps", ["research","script","images","narration","music","video"])
    topic       = body.get("topic", "").strip()
    focus_angle = body.get("focus_angle", "").strip()
    video_type  = body.get("video_type", "normal")
    for s in steps:
        if s in _state["stages"]: _state["stages"][s] = "pending"
    _state["video_type"] = video_type
    config.GEMINI_MODEL = model
    _state["running"] = True
    _stop_event.clear()
    t = threading.Thread(target=_run_pipeline, args=(steps, topic, video_type, focus_angle), daemon=True)
    t.start()
    return jsonify({"ok": True, "gemini_model": model})


@app.route("/api/stop", methods=["POST"])
def stop_pipeline():
    """Request the pipeline to stop after the current stage finishes."""
    _stop_event.set()
    _state["stop_requested"] = True
    _log("⏹ Stop requested — will halt after current step completes…", "warn")
    return jsonify({"ok": True})


@app.route("/api/status")
def status():
    return jsonify({
        "running":        _state["running"],
        "stop_requested": _state.get("stop_requested", False),
        "stages":         _state["stages"],
        "has_video":      bool(_state.get("video_path") and os.path.exists(_state["video_path"])),
        "has_thumb":      bool(_state.get("thumb_path") and os.path.exists(_state["thumb_path"])),
        "has_script":     bool(_state.get("script")),
        "yt_url":         _state.get("yt_url"),
        "error":          _state.get("error"),
        "upload_pct":     _state.get("upload_pct", 0),
        "uploading":      _state.get("uploading", False),
        "video_type":     _state.get("video_type", "normal"),
    })


# ── Media serving ─────────────────────────────────────────────────────────────

@app.route("/video")
def serve_video():
    p = _state.get("video_path")

    if p and os.path.exists(p):
        response = send_file(
            p,
            mimetype="video/mp4",
            conditional=True,
            max_age=0
        )
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    abort(404)

@app.route("/thumbnail")
def serve_thumb():
    p = _state.get("thumb_path")
    if p and os.path.exists(p): return send_file(p, mimetype="image/jpeg")
    abort(404)

@app.route("/thumbnail/<int:ts>")  # cache-busting route
def serve_thumb_ts(ts):
    return serve_thumb()


# ── Script / description data ─────────────────────────────────────────────────

@app.route("/api/output")
def get_output():
    s = _state.get("script") or {}
    r = _state.get("research") or {}
    media = _read_media_report(s)
    return jsonify({
        "title":       s.get("title", ""),
        "description": _state.get("description", _build_description(s, r, _state.get("video_type","normal")) if s and r else ""),
        "tags":        s.get("tags", []),
        "sections":    s.get("sections", []),
        "topic":       r.get("topic", ""),
        "why_now":     r.get("why_now", ""),
        "hook":        r.get("hook_question", ""),
        "content_plan": {key: r.get(key) for key in ("central_answer", "scope", "outline", "factual_cautions", "verification_status")},
        "content_review": s.get("content_review", {}),
        "media_report": media,
    })

@app.route("/api/save-output", methods=["POST"])
def save_output():
    body = request.get_json(force=True) or {}
    if _state["script"]:
        if "title"       in body: _state["script"]["title"]       = body["title"]
        if "description" in body: _state["script"]["description"] = body["description"]
        if "tags"        in body: _state["script"]["tags"]        = body["tags"]
    if "description" in body:
        _state["description"] = body["description"]
    # Persist script to disk
    if _state["script"]:
        with open(f"{config.OUTPUT_DIR}/script.json", "w", encoding="utf-8") as f:
            json.dump(_state["script"], f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


# ── AI description regeneration ───────────────────────────────────────────────

@app.route("/api/regenerate-description", methods=["POST"])
def regen_description():
    if not _state.get("script") or not _state.get("research"):
        return jsonify({"ok": False, "error": "No script loaded"})
    try:
        from agents.gemini_client import generate
        script   = _state["script"]
        research = _state["research"]
        prompt = f"""Write a compelling YouTube video description for this video.

Title: {script.get('title','')}
Topic: {research.get('topic','')}
Hook: {research.get('hook_question','')}
Key points: {json.dumps(research.get('key_points',[]))}
Tags: {json.dumps(script.get('tags',[]))}

The description should:
- Open with the hook question or a gripping statement
- List what viewers will learn (3-5 bullet points)
- Include a strong CTA to subscribe and comment
- End with hashtags

Return ONLY the description text, no explanation."""
        desc = generate(prompt)
        _state["description"] = desc
        return jsonify({"ok": True, "description": desc})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Thumbnail regeneration ────────────────────────────────────────────────────

@app.route("/api/regenerate-thumbnail", methods=["POST"])
def regen_thumbnail():
    if not _state.get("script"):
        return jsonify({"ok": False, "error": "No script loaded"})
    body = request.get_json(force=True) or {}

    # Allow title override for thumbnail
    if "title" in body and _state["script"]:
        _state["script"]["title"] = body["title"]

    try:
        image_map_int = {int(k): v for k, v in _state.get("image_map", {}).items()}
        thumb = _make_thumbnail(_state["script"], image_map_int, config.OUTPUT_DIR)
        _state["thumb_path"] = thumb
        return jsonify({"ok": True, "ts": int(time.time())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── YouTube upload ────────────────────────────────────────────────────────────

@app.route("/api/yt-status")
def yt_status():
    token_ok = os.path.exists(config.YOUTUBE_TOKEN_FILE)
    secret_ok = os.path.exists(config.YOUTUBE_CLIENT_SECRET)

    return jsonify({
        "token": token_ok,
        "secret": secret_ok
    })

@app.route("/api/upload", methods=["POST"])
def upload():
    _state["error"] = None
    if _state.get("uploading"):
        return jsonify({
            "ok": False,
            "error": "Đang có một video được upload"
        })

    video_path = _state.get("video_path")

    if not video_path or not os.path.exists(video_path):
        return jsonify({
            "ok": False,
            "error": f"Không tìm thấy video: {video_path}"
        })

    if (
        not os.path.exists(config.YOUTUBE_TOKEN_FILE)
        and not os.path.exists(config.YOUTUBE_CLIENT_SECRET)
    ):
        return jsonify({
            "ok": False,
            "error": (
                "Không tìm thấy youtube_token.pickle và "
                "client_secret.json trong thư mục dự án"
            )
        })
    if _state.get("uploading"):
        return jsonify({"ok": False, "error": "Upload already in progress"})
    if not _state.get("video_path") or not os.path.exists(_state["video_path"]):
        return jsonify({"ok": False, "error": "No video to upload"})

    body    = request.get_json(force=True) or {}
    privacy = body.get("privacy", config.VIDEO_PRIVACY)
    publish_at = body.get("publish_at", "").strip()   # RFC 3339 datetime or ""

    def _do_upload():
        _state["uploading"]  = True
        _state["upload_pct"] = 0
        _state["yt_url"]     = None
        try:
            import pickle as pkl
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            from google_auth_oauthlib.flow import InstalledAppFlow

            TOKEN_FILE = config.YOUTUBE_TOKEN_FILE
            creds = None
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, "rb") as f: creds = pkl.load(f)
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(
                    config.YOUTUBE_CLIENT_SECRET, config.YOUTUBE_SCOPES)
                creds = flow.run_local_server(port=8080)
                with open(TOKEN_FILE, "wb") as f: pkl.dump(creds, f)

            yt = build("youtube", "v3", credentials=creds)
            script = _state["script"] or {}
            desc   = _state.get("description", script.get("description",""))

            body_data = {
                "snippet": {
                    "title":       script.get("title",""),
                    "description": desc,
                    "tags":        script.get("tags",[]),
                    "categoryId":  config.VIDEO_CATEGORY_ID,
                },
                "status": {
                    # If scheduling: upload as private, YouTube publishes at publishAt
                    "privacyStatus":           "private" if publish_at else privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }
            if publish_at:
                body_data["status"]["publishAt"] = publish_at

            media = MediaFileUpload(
                _state["video_path"], mimetype="video/mp4",
                resumable=True, chunksize=5*1024*1024,
            )
            req = yt.videos().insert(part=",".join(body_data.keys()),
                                     body=body_data, media_body=media)
            response = None
            while response is None:
                status_chunk, response = req.next_chunk()
                if status_chunk:
                    _state["upload_pct"] = int(status_chunk.progress() * 100)

            video_id = response["id"]

            # Upload thumbnail if available
            if _state.get("thumb_path") and os.path.exists(_state["thumb_path"]):
                try:
                    yt.thumbnails().set(
                        videoId=video_id,
                        media_body=MediaFileUpload(
                            _state["thumb_path"], mimetype="image/jpeg")
                    ).execute()
                except: pass

            _state["yt_url"]     = f"https://www.youtube.com/watch?v={video_id}"
            _state["upload_pct"] = 100
            _log(f"Uploaded! {_state['yt_url']}")

        except Exception as e:
            _log(f"Upload error: {e}", "error")
            _state["error"] = str(e)
        finally:
            _state["uploading"] = False

    threading.Thread(target=_do_upload, daemon=True).start()
    return jsonify({"ok": True})


# ── Settings read / write ─────────────────────────────────────────────────────

@app.route("/api/models")
def get_models():
    from agents.gemini_client import get_available_models
    result = get_available_models(refresh=request.args.get("refresh") == "1")
    return jsonify({**result, "selected": config.GEMINI_MODEL})


@app.route("/api/settings")
def get_settings():
    return jsonify({
        "gemini_key":      config.GEMINI_API_KEY,
        "pexels_key":      config.PEXELS_API_KEY,
        "music_enabled":   getattr(config, "MUSIC_ENABLED", True),
        "music_volume":    getattr(config, "MUSIC_VOLUME", 0.12),
        "music_library":   getattr(config, "MUSIC_LIBRARY_DIR", ""),
        "channel_name":    config.CHANNEL_NAME,
        "channel_desc":    config.CHANNEL_DESCRIPTION,
        "voice_id":        config.VOICE_ID,
        "voice_rate":      config.VOICE_RATE,
        "voice_pitch":     config.VOICE_PITCH,
        "voice_smooth_pauses": getattr(config, "VOICE_SMOOTH_PAUSES", True),
        "voice_max_pause": getattr(config, "VOICE_MAX_PAUSE", 0.45),
        "gemini_model":    config.GEMINI_MODEL,
        "overlay":         config.OVERLAY_OPACITY,
        "kb_start":        config.KB_ZOOM_START,
        "kb_end":          config.KB_ZOOM_END,
        "privacy":         config.VIDEO_PRIVACY,
        "broll_interval":  getattr(config, "BROLL_INTERVAL",  10.0),
        "broll_xfade":     getattr(config, "BROLL_XFADE_DUR",  1.2),
        "render_preset":   getattr(config, "RENDER_PRESET", "ultrafast"),
        "banned_topics":   load_banned_topics(),
    })

@app.route("/api/save-settings", methods=["POST"])
def save_settings():
    body = request.get_json(force=True) or {}
    if "voice_max_pause" in body:
        try:
            pause = float(body["voice_max_pause"])
            if not math.isfinite(pause) or not 0.2 <= pause <= 0.8:
                raise ValueError("Invalid pause")
            body["voice_max_pause"] = pause
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Khoảng nghỉ phải từ 0.2 đến 0.8 giây."}), 400
    if "voice_smooth_pauses" in body and not isinstance(body["voice_smooth_pauses"], bool):
        return jsonify({"ok": False, "error": "Tùy chọn làm mượt giọng phải là bật/tắt."}), 400
    if "gemini_model" in body:
        from agents.gemini_client import normalize_model
        try:
            body["gemini_model"] = normalize_model(body["gemini_model"])
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        if _state["running"] and body["gemini_model"] != config.GEMINI_MODEL:
            return jsonify({"ok": False, "error": "Đang tạo video; hãy đợi hoàn tất trước khi đổi model."}), 409
    # Update live config object
    m = {
        "gemini_key":    "GEMINI_API_KEY",
        "pexels_key":    "PEXELS_API_KEY",
        "music_enabled": "MUSIC_ENABLED",
        "music_volume":  "MUSIC_VOLUME",
        "channel_name":  "CHANNEL_NAME",
        "channel_desc":  "CHANNEL_DESCRIPTION",
        "voice_id":      "VOICE_ID",
        "voice_rate":    "VOICE_RATE",
        "voice_pitch":   "VOICE_PITCH",
        "voice_smooth_pauses": "VOICE_SMOOTH_PAUSES",
        "voice_max_pause": "VOICE_MAX_PAUSE",
        "gemini_model":  "GEMINI_MODEL",
        "overlay":       "OVERLAY_OPACITY",
        "kb_start":      "KB_ZOOM_START",
        "kb_end":        "KB_ZOOM_END",
        "privacy":       "VIDEO_PRIVACY",
        "broll_interval":"BROLL_INTERVAL",
        "broll_xfade":   "BROLL_XFADE_DUR",
        "render_preset": "RENDER_PRESET",
    }
    for k, attr in m.items():
        if k in body:
            val = body[k]
            if attr in ("OVERLAY_OPACITY","KB_ZOOM_START","KB_ZOOM_END","MUSIC_VOLUME",
                          "BROLL_INTERVAL","BROLL_XFADE_DUR","VOICE_MAX_PAUSE"):
                val = float(val)
            if attr in ("MUSIC_ENABLED", "VOICE_SMOOTH_PAUSES"):
                val = bool(val)
            setattr(config, attr, val)

    # Patch config.py file on disk
    cfg_path = os.path.join(ROOT, "config.py")
    try:
        with open(cfg_path, encoding="utf-8") as f: src = f.read()

        patches = {
            "GEMINI_API_KEY":    body.get("gemini_key"),
            "PEXELS_API_KEY":    body.get("pexels_key"),
            "MUSIC_ENABLED":     body.get("music_enabled"),
            "MUSIC_VOLUME":      body.get("music_volume"),
            "CHANNEL_NAME":      body.get("channel_name"),
            "VOICE_ID":          body.get("voice_id"),
            "VOICE_RATE":        body.get("voice_rate"),
            "VOICE_PITCH":       body.get("voice_pitch"),
            "VOICE_SMOOTH_PAUSES": body.get("voice_smooth_pauses"),
            "VOICE_MAX_PAUSE": body.get("voice_max_pause"),
            "GEMINI_MODEL":      body.get("gemini_model"),
            "OVERLAY_OPACITY":   body.get("overlay"),
            "KB_ZOOM_START":     body.get("kb_start"),
            "KB_ZOOM_END":       body.get("kb_end"),
            "VIDEO_PRIVACY":     body.get("privacy"),
            "BROLL_INTERVAL":    body.get("broll_interval"),
            "BROLL_XFADE_DUR":   body.get("broll_xfade"),
            "RENDER_PRESET":     body.get("render_preset"),
        }
        for attr, val in patches.items():
            if val is None: continue
            # Replace the first assignment of this variable
            if isinstance(val, str):
                pat = rf'({re.escape(attr)}\s*=\s*)[^\n]+'
                src = re.sub(pat, rf'\g<1>"{val}"', src, count=1)
            else:
                pat = rf'({re.escape(attr)}\s*=\s*)[^\n]+'
                src = re.sub(pat, rf'\g<1>{val}', src, count=1)

        # Channel description (multiline)
        if "channel_desc" in body:
            cd = body["channel_desc"].replace('"""', "'''")
            pat = r'(CHANNEL_DESCRIPTION\s*=\s*)"""[\s\S]*?"""'
            src = re.sub(pat, f'CHANNEL_DESCRIPTION = """\n{cd}\n"""', src, count=1)

        with open(cfg_path, "w", encoding="utf-8") as f: f.write(src)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Saved to memory but disk write failed: {e}"})

    return jsonify({"ok": True})


@app.route("/api/banned-topics")
def get_banned_topics():
    return jsonify({"topics": load_banned_topics()})


@app.route("/api/save-banned-topics", methods=["POST"])
def post_banned_topics():
    body   = request.get_json(force=True) or {}
    topics = body.get("topics", [])
    try:
        save_banned_topics(topics)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import webbrowser
    port = 7070
    print(f"\n🎬  YouTube AI Agent Studio")
    print(f"   Opening at  http://localhost:{port}\n")
    threading.Timer(1.4, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(port=port, debug=False, threaded=True, use_reloader=False)
