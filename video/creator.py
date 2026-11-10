"""
Video Creator — Cinematic Edition (Normal) + Shorts Edition
Normal  : 1920×1080, topic-matched photos and silent B-roll, Ken Burns,
          animated title cards, bullet points, lower-third captions.
Shorts  : 1080×1920, photos and silent B-roll ordered with the narration,
          NO section-type labels, synced SRT captions burnt in,
          high-energy zoom pulses, gradient text overlay.

Runs fully offline: MoviePy 1.0.3 + Pillow + NumPy.
"""
import os, re, math
import hashlib
import json
from bisect import bisect_right
from difflib import SequenceMatcher
import unicodedata
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from moviepy.editor import VideoClip, VideoFileClip, AudioFileClip, concatenate_videoclips

import config
from video.narrator import _script_fingerprint, _section_spans, _prepare_text

# ── globals (overwritten per call) ────────────────────────────
W   = config.VIDEO_WIDTH
H   = config.VIDEO_HEIGHT
FPS = config.VIDEO_FPS
C   = config.COLORS

# FIX: Font cache — load each (variant, size) pair once, not every frame.
_font_cache: dict = {}


# ══════════════════════════════════════════════════════════════
#  FONT LOADING
# ══════════════════════════════════════════════════════════════

def _load_font(variant: str, size: int) -> ImageFont.FreeTypeFont:
    key = (variant, size)
    if key in _font_cache:
        return _font_cache[key]
    paths = config.FONT_PATHS.get(variant, config.FONT_PATHS["regular"])
    font  = None
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ══════════════════════════════════════════════════════════════
#  SRT CAPTION PARSER
# ══════════════════════════════════════════════════════════════

def _parse_srt(srt_path: str) -> list:
    """
    Returns list of {"start": float_sec, "end": float_sec, "text": str}.
    Prints a clear warning if the file is missing or empty.
    """
    if not srt_path or not os.path.exists(srt_path):
        print(f"   ⚠ SRT file not found: {srt_path}")
        print("      Captions will not appear. Run the Narration step first.")
        return []

    raw = open(srt_path, encoding="utf-8").read().strip()
    if not raw:
        print(f"   ⚠ SRT file exists but is empty: {srt_path}")
        print("      This usually means edge-tts returned no word-boundary events.")
        return []

    def _ts(s):
        h, m, rest = s.strip().split(":")
        sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

    captions = []
    for block in re.split(r"\n{2,}", raw):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            times = lines[1].split(" --> ")
            captions.append({
                "start": _ts(times[0]),
                "end":   _ts(times[1]),
                "text":  " ".join(lines[2:]),
            })
        except Exception:
            continue

    if not captions:
        print(f"   ⚠ SRT file has content but no valid cues could be parsed.")
        print(f"      First 300 chars: {raw[:300]!r}")

    return captions


# ══════════════════════════════════════════════════════════════
#  IMAGE UTILITIES
# ══════════════════════════════════════════════════════════════

def _load_bg_image(path: str) -> np.ndarray | None:
    """Load + cover-resize to current W×H."""
    if not path or not os.path.exists(path):
        return None
    img  = Image.open(path).convert("RGB")
    iw, ih = img.size
    scale  = max(W / iw, H / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - W) // 2
    top    = (nh - H) // 2
    return np.array(img.crop((left, top, left + W, top + H)), dtype=np.uint8)


def _load_media(path: str):
    if not path or not os.path.isfile(path):
        return None
    try:
        if os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".webm"):
            return VideoFileClip(path, audio=False)
        return _load_bg_image(path)
    except (OSError, ValueError):
        print("   ⚠ Không đọc được một tệp tư liệu; bỏ qua tệp này.")
        return None


def _media_frame(source, t: float, duration: float, index: int = 0, shorts: bool = False) -> np.ndarray:
    if source is None:
        return _gradient_bg(t)
    if isinstance(source, np.ndarray):
        if shorts:
            return _zoom_pulse(source, t, index, duration=duration)
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        return _ken_burns(source, t, duration, pan_dir=directions[index % len(directions)])
    # Repeat short silent B-roll with a dissolve, avoiding both a frozen tail and a hard loop cut.
    length = source.duration
    fade = min(0.35, length * 0.2)
    local = max(0.0, t)
    if local >= length:
        local = fade + (local - length) % max(length - fade, 0.001)
    frame = source.get_frame(min(local, max(0.0, length - 1e-6)))
    if duration > length and fade > 0 and local >= length - fade:
        alpha = (local - length + fade) / fade
        following = source.get_frame(max(0.0, local - length + fade))
        frame = (frame.astype(np.float32) * (1 - alpha) + following.astype(np.float32) * alpha).astype(np.uint8)
    image = Image.fromarray(frame.astype(np.uint8)).convert("RGB")
    scale = max(W / image.width, H / image.height)
    image = image.resize((max(W, math.ceil(image.width * scale)), max(H, math.ceil(image.height * scale))), Image.LANCZOS)
    left, top = (image.width - W) // 2, (image.height - H) // 2
    return np.array(image.crop((left, top, left + W, top + H)))


def _section_background(sources: list, t: float, duration: float, timeline: list = None) -> np.ndarray:
    if not sources:
        return _apply_overlay(_gradient_bg(t), 0.3)
    slot_duration = duration if len(sources) == 1 else max(0.1, BROLL_INTERVAL)
    slot = int(t / max(slot_duration, 0.001))
    index = slot % len(sources)
    local_t = t - slot * slot_duration
    if timeline:
        index = min(len(timeline) - 1, max(0, bisect_right([slot["start"] for slot in timeline], t) - 1))
        local_t = t - timeline[index]["start"]
        slot_duration = timeline[index]["end"] - timeline[index]["start"]
    frame = _apply_overlay(_media_frame(sources[index], local_t, slot_duration, index))
    fade = min(BROLL_XFADE_DUR, slot_duration * 0.3)
    following_index = index + 1 if timeline else (index + 1) % len(sources)
    if following_index < len(sources) and following_index != index and fade > 0 and local_t >= slot_duration - fade:
        alpha = min(1.0, (local_t - slot_duration + fade) / fade)
        alpha = alpha * alpha * (3 - 2 * alpha)
        following = _apply_overlay(_media_frame(sources[following_index], 0.0, slot_duration, following_index))
        frame = (frame.astype(np.float32) * (1 - alpha) + following.astype(np.float32) * alpha).astype(np.uint8)
    return frame


def _ken_burns(base: np.ndarray, t: float, duration: float,
               zoom_start=None, zoom_end=None, pan_dir=(1, 0)) -> np.ndarray:
    zs = zoom_start or config.KB_ZOOM_START
    ze = zoom_end   or config.KB_ZOOM_END
    p  = t / max(duration, 0.001)
    scale   = zs + (ze - zs) * p
    crop_w  = int(W / scale)
    crop_h  = int(H / scale)
    max_x   = W - crop_w
    max_y   = H - crop_h
    ox = int(max_x * 0.5 + max_x * 0.5 * pan_dir[0] * p)
    oy = int(max_y * 0.5 + max_y * 0.5 * pan_dir[1] * p)
    ox = max(0, min(ox, max_x))
    oy = max(0, min(oy, max_y))
    cropped = base[oy: oy + crop_h, ox: ox + crop_w]
    return np.array(Image.fromarray(cropped).resize((W, H), Image.LANCZOS), dtype=np.uint8)


def _apply_overlay(arr: np.ndarray, opacity: float = None) -> np.ndarray:
    op = opacity if opacity is not None else config.OVERLAY_OPACITY
    return (arr * (1 - op)).astype(np.uint8)


def _gradient_bg(t: float) -> np.ndarray:
    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        r = int(C["background"][0] + (C["primary"][0] - C["background"][0]) * (y / H) * 0.4)
        g = int(C["background"][1] + (C["primary"][1] - C["background"][1]) * (y / H) * 0.3)
        b = int(C["background"][2] + (C["primary"][2] - C["background"][2]) * (y / H) * 0.5)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    for i in range(3):
        phase = t * 0.3 + i * 2.1
        cx = W // 2 + int(300 * math.cos(phase * 0.5 + i))
        cy = H // 2 + int(200 * math.sin(phase * 0.4 + i))
        r_px = int(280 + 60 * math.sin(phase))
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        od.ellipse([cx - r_px, cy - r_px, cx + r_px, cy + r_px],
                   fill=(*C["primary"], 20))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return np.array(img, dtype=np.uint8)


# ══════════════════════════════════════════════════════════════
#  TEXT UTILITIES
# ══════════════════════════════════════════════════════════════

def _shadow_text(draw, pos, text, font, fill, shadow_color=(0, 0, 0), shadow_offset=3):
    x, y = pos
    for ox in range(-shadow_offset, shadow_offset + 1):
        for oy in range(-shadow_offset, shadow_offset + 1):
            if abs(ox) + abs(oy) >= shadow_offset:
                draw.text((x + ox, y + oy), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill)


def _wrap_text(draw, text, font, max_width):
    words, lines, current = text.split(), [], []
    for word in words:
        test = " ".join(current + [word])
        if draw.textbbox((0, 0), test, font=font)[2] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _alpha(color, a):
    a = max(0.0, min(1.0, a))
    return tuple(int(c * a) for c in color[:3])


# ══════════════════════════════════════════════════════════════
#  NORMAL VIDEO — PERSISTENT UI
# ══════════════════════════════════════════════════════════════

def _draw_progress_bar(draw, progress, section_num, total):
    bar_h = 4
    draw.rectangle([0, H - bar_h, W, H], fill=(30, 30, 60))
    draw.rectangle([0, H - bar_h, int(W * progress), H], fill=C["primary"])
    dot_y = H - 22
    for i in range(total):
        cx = W - 30 - (total - i - 1) * 20
        col = C["primary"] if i < section_num else (60, 60, 90)
        draw.ellipse([cx - 5, dot_y - 5, cx + 5, dot_y + 5], fill=col)


def _draw_lower_third(frame, caption, alpha, section_title=""):
    if alpha <= 0:
        return frame
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    bar_h, bar_y = 80, H - 120
    od.rectangle([0, bar_y, W, bar_y + bar_h], fill=(10, 10, 30, int(200 * alpha)))
    od.rectangle([0, bar_y, 6, bar_y + bar_h], fill=(*C["primary"], int(255 * alpha)))
    if section_title:
        od.text((30, bar_y + 8), section_title.upper(),
                font=_load_font("regular", 22), fill=(*C["accent"], int(200 * alpha)))
    if caption:
        od.text((30, bar_y + 36), caption[:80],
                font=_load_font("bold", 34), fill=(*C["white"], int(255 * alpha)))
    return Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")


# ══════════════════════════════════════════════════════════════
#  NORMAL VIDEO — SRT CAPTION OVERLAY
# ══════════════════════════════════════════════════════════════

NORMAL_CAP_FONT_SIZE = 36       # smaller than Shorts (56px) — 1920×1080 context
NORMAL_CAP_BG        = (0, 0, 0, 160)   # semi-transparent dark pill
NORMAL_CAP_FG        = (255, 255, 255)
NORMAL_CAP_MARGIN_B  = 90       # px from bottom (above progress bar)


def _draw_normal_caption(frame: Image.Image, text: str) -> Image.Image:
    """
    Burn a subtitle cue into a normal (16:9) video frame.
    Centred horizontally, positioned above the progress bar.
    Uses the same pill-background approach as Shorts captions.
    """
    if not text or not text.strip():
        return frame

    base_rgba    = frame.convert("RGBA")
    measure_draw = ImageDraw.Draw(base_rgba)
    font         = _load_font("bold", NORMAL_CAP_FONT_SIZE)
    max_w        = W - 200   # leave margins on both sides

    # Wrap text — normal video uses Title Case, not ALL CAPS
    lines  = _wrap_text(measure_draw, text, font, max_w)
    line_h = NORMAL_CAP_FONT_SIZE + 8
    pad    = 16

    # Position: above progress bar and lower-third area
    block_h = len(lines) * line_h
    block_y = H - NORMAL_CAP_MARGIN_B - block_h

    # Build pill background
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od      = ImageDraw.Draw(overlay)
    for i, line in enumerate(lines):
        bbox = measure_draw.textbbox((0, 0), line, font=font)
        lw   = bbox[2] - bbox[0]
        lx   = (W - lw) // 2
        ly   = block_y + i * line_h
        od.rounded_rectangle(
            [lx - pad, ly - 4, lx + lw + pad, ly + line_h - 2],
            radius=8, fill=NORMAL_CAP_BG,
        )

    composited = Image.alpha_composite(base_rgba, overlay)
    draw2      = ImageDraw.Draw(composited)

    for i, line in enumerate(lines):
        bbox = draw2.textbbox((0, 0), line, font=font)
        lw   = bbox[2] - bbox[0]
        lx   = (W - lw) // 2
        ly   = block_y + i * line_h
        _shadow_text(draw2, (lx, ly), line, font,
                     fill=NORMAL_CAP_FG, shadow_color=(0, 0, 0), shadow_offset=3)

    return composited.convert("RGB")


# ══════════════════════════════════════════════════════════════
#  NORMAL VIDEO — SECTION RENDERERS
# ══════════════════════════════════════════════════════════════

def _render_hook(draw, section, t, duration):
    narr  = section.get("narration", "")
    title = section.get("title", "")
    if t > 0.3:
        a = min(1.0, (t - 0.3) / 0.4)
        _shadow_text(draw, (60, 50), "▶ FRACTURED TIMELINES",
                     _load_font("regular", 24), fill=_alpha(C["accent"], a), shadow_offset=2)
    sentences = [s.strip() for s in narr.replace("...", "…").split(".") if s.strip()]
    first_two = ". ".join(sentences[:2]) + ("." if sentences else "")
    if t > 0.5:
        a = min(1.0, _ease_out((t - 0.5) / 0.8))
        font_big = _load_font("bold", 64)
        lines = _wrap_text(draw, first_two, font_big, W - 200)
        y0 = H // 2 - len(lines) * 78 // 2
        for i, line in enumerate(lines[:5]):
            la    = min(1.0, _ease_out(max(0.0, (t - 0.5 - i * 0.18) / 0.5)))
            slide = int((1 - la) * 60)
            _shadow_text(draw, (100 + slide, y0 + i * 78), line,
                         font_big, fill=_alpha(C["white"], la), shadow_offset=4)


def _render_content(draw, section, t, duration, section_num):
    title   = section.get("title", "")
    bullets = section.get("bullet_points", [])
    narr    = section.get("narration", "")
    if t > 0.2:
        a = min(1.0, _ease_out((t - 0.2) / 0.5))
        font_title = _load_font("bold", 52)
        bbox = draw.textbbox((0, 0), title, font=font_title)
        tw   = bbox[2] - bbox[0]
        px, py, pad = 70, 55, 18
        draw.rounded_rectangle([px - pad, py - 10, px + tw + pad, py + 65],
                                radius=10, fill=(10, 10, 30, 200))
        draw.rectangle([px - pad, py - 10, px - pad + 5, py + 65],
                       fill=_alpha(C["primary"], a))
        _shadow_text(draw, (px, py), title, font_title,
                     fill=_alpha(C["white"], a), shadow_offset=3)
    if bullets:
        font_b = _load_font("regular", 40)
        for i, bullet in enumerate(bullets[:6]):
            appear = 0.8 + i * 0.55
            if t <= appear:
                continue
            ba    = min(1.0, _ease_out((t - appear) / 0.4))
            slide = int((1 - ba) * 80)
            bx    = 110 - slide
            by    = 200 + i * 88
            draw.ellipse([bx - 2, by + 4, bx + 36, by + 40],
                         fill=_alpha(C["primary"], ba))
            draw.text((bx + 8, by + 6), str(i + 1),
                      font=_load_font("bold", 26), fill=_alpha(C["white"], ba))
            _shadow_text(draw, (bx + 50, by),
                         bullet[:70] + ("…" if len(bullet) > 70 else ""),
                         font_b, fill=_alpha(C["light"], ba), shadow_offset=3)
    else:
        if t > 0.7:
            a = min(1.0, _ease_out((t - 0.7) / 0.6))
            font_n = _load_font("regular", 38)
            lines  = _wrap_text(draw, narr, font_n, W - 220)
            for i, line in enumerate(lines[:10]):
                la = min(1.0, _ease_out(max(0.0, (t - 0.7 - i * 0.1) / 0.4)))
                _shadow_text(draw, (110, 200 + i * 56), line, font_n,
                             fill=_alpha(C["light"], la), shadow_offset=2)


def _render_conclusion(draw, section, t, duration):
    bullets = section.get("bullet_points", [])
    title   = section.get("title", "Key Takeaways")
    if t > 0.3:
        a = min(1.0, _ease_out((t - 0.3) / 0.5))
        font_h = _load_font("bold", 70)
        bbox = draw.textbbox((0, 0), title, font=font_h)
        tw   = bbox[2] - bbox[0]
        x    = (W - tw) // 2
        _shadow_text(draw, (x, 80), title, font_h,
                     fill=_alpha(C["highlight"], a), shadow_offset=4)
        uw = int(min(1.0, (t - 0.3) / 0.6) * tw)
        if uw > 0:
            draw.rectangle([x, 162, x + uw, 168], fill=_alpha(C["highlight"], a))
    font_b = _load_font("regular", 44)
    for i, pt in enumerate(bullets[:4]):
        appear = 0.9 + i * 0.6
        if t <= appear:
            continue
        ba = min(1.0, _ease_out((t - appear) / 0.5))
        by = 210 + i * 110
        _shadow_text(draw, (100, by), "✓", _load_font("bold", 40),
                     fill=_alpha(C["success"], ba), shadow_offset=2)
        _shadow_text(draw, (160, by), pt[:65] + ("…" if len(pt) > 65 else ""),
                     font_b, fill=_alpha(C["white"], ba), shadow_offset=3)


def _render_cta(draw, section, t):
    if t > 0.3:
        a = min(1.0, _ease_out((t - 0.3) / 0.5))
        font_s = _load_font("bold", 100)
        txt  = "ĐĂNG KÝ"
        bbox = draw.textbbox((0, 0), txt, font=font_s)
        tw   = bbox[2] - bbox[0]
        _shadow_text(draw, ((W - tw) // 2, H // 2 - 150), txt, font_s,
                     fill=_alpha(C["white"], a), shadow_offset=6)
    if t > 1.0:
        a2 = min(1.0, _ease_out((t - 1.0) / 0.5))
        font_sub = _load_font("regular", 44)
        sub  = "Cùng khám phá những điều thú vị"
        bbox = draw.textbbox((0, 0), sub, font=font_sub)
        tw   = bbox[2] - bbox[0]
        _shadow_text(draw, ((W - tw) // 2, H // 2), sub, font_sub,
                     fill=_alpha(C["light"], a2), shadow_offset=3)
    if t > 1.8:
        a3 = min(1.0, _ease_out((t - 1.8) / 0.4))
        font_b = _load_font("bold", 36)
        bell   = "Bạn muốn khám phá điều gì tiếp theo?"
        bbox   = draw.textbbox((0, 0), bell, font=font_b)
        tw     = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, H // 2 + 90), bell, font=font_b,
                  fill=_alpha(C["highlight"], a3))


# ══════════════════════════════════════════════════════════════
#  NORMAL VIDEO — CLIP BUILDER
# ══════════════════════════════════════════════════════════════

# B-roll timing — read from config so GUI changes take effect without restart.
# Fallback literals keep creator.py working even if config is missing the attrs.
BROLL_INTERVAL  = float(getattr(config, "BROLL_INTERVAL",  10.0))
BROLL_XFADE_DUR = float(getattr(config, "BROLL_XFADE_DUR",  1.2))


def _make_clip(section, duration, bg_images, section_num, total_sections,
               pan_dir=(1, 0), captions: list = None, section_start_t: float = 0.0,
               media_timeline: list = None):
    """
    Build a single section clip with:
      • Narration-aligned shots; legacy scripts retain BROLL_INTERVAL timing
      • Alternating Ken Burns direction per image for visual rhythm
      • On-screen title / bullets / narration overlay
      • SRT subtitle cues burnt in at bottom-centre (36px, sentence-level sync)
      • Progress bar

    bg_images : list of np.ndarray (pre-loaded).  If empty, uses gradient.
    captions  : full SRT cue list [{start, end, text}] for the whole video.
    section_start_t : global timestamp (seconds) when this section begins —
                      used to look up the correct SRT cue for local time t.
    """
    s_type  = section.get("section_type", "content")
    caption = section.get("caption_text", "")
    title   = section.get("title", "")
    def make_frame(t):
        # Follow the planned shot order once; do not cycle back to unrelated earlier narration.
        frame_np = _section_background(bg_images, t, duration, media_timeline)

        frame = Image.fromarray(frame_np)
        draw  = ImageDraw.Draw(frame)

        # ── On-screen title / bullets / narration ─────────────────────────
        if s_type == "hook":
            _render_hook(draw, section, t, duration)
        elif s_type == "cta":
            _render_cta(draw, section, t)
        elif s_type == "conclusion":
            _render_conclusion(draw, section, t, duration)
        else:
            _render_content(draw, section, t, duration, section_num)

        # ── Lower-third caption bar (section caption_text field) ───────────
        cap_alpha = 0.0
        if t > 1.5:
            cap_alpha = min(1.0, (t - 1.5) / 0.6)
        frame = _draw_lower_third(frame, caption, cap_alpha, title)

        # ── SRT subtitle cue (bottom-centre, 36px) ────────────────────────
        if captions:
            global_t  = section_start_t + t
            srt_text  = _get_caption_at(captions, global_t)
            if srt_text:
                frame = _draw_normal_caption(frame, srt_text)

        # ── Progress bar ──────────────────────────────────────────────────
        draw2 = ImageDraw.Draw(frame)
        _draw_progress_bar(draw2, t / duration, section_num, total_sections)
        return np.array(frame)

    return VideoClip(make_frame, duration=duration)


# ══════════════════════════════════════════════════════════════
#  TRANSITION
# ══════════════════════════════════════════════════════════════

def _crossfade(clip_a, clip_b, fade_dur=0.8):
    total = clip_a.duration + clip_b.duration - fade_dur

    def make_frame(t):
        if t < clip_a.duration - fade_dur:
            return clip_a.get_frame(t)
        elif t > clip_a.duration:
            return clip_b.get_frame(t - clip_a.duration + fade_dur)
        else:
            alpha   = (t - (clip_a.duration - fade_dur)) / fade_dur
            frame_a = clip_a.get_frame(t)
            frame_b = clip_b.get_frame(t - (clip_a.duration - fade_dur))
            return (frame_a * (1 - alpha) + frame_b * alpha).astype(np.uint8)

    return VideoClip(make_frame, duration=total)


def _join_section_clips(clips: list, fade_dur: float = 0.4, captions: list = None):
    """Blend only the pictures; keep every second of the narration timeline."""
    starts, total = [], 0.0
    for clip in clips:
        starts.append(total)
        total += clip.duration

    def make_frame(t):
        index = max(0, min(len(clips) - 1, bisect_right(starts, t) - 1))
        local = max(0.0, min(t - starts[index], clips[index].duration - 1e-6))
        frame = clips[index].get_frame(local)
        fade = min(fade_dur, clips[index].duration / 2, clips[index - 1].duration / 2) if index else 0
        if index and fade > 0 and local < fade:
            previous = clips[index - 1].get_frame(clips[index - 1].duration - fade + local)
            alpha = local / fade
            alpha = alpha * alpha * (3 - 2 * alpha)
            frame = (previous.astype(np.float32) * (1 - alpha) + frame.astype(np.float32) * alpha).astype(np.uint8)
        if captions:
            frame = np.array(_draw_normal_caption(Image.fromarray(frame), _get_caption_at(captions, t)))
        return frame

    return VideoClip(make_frame, duration=total)


def _load_section_timing(script: dict, output_dir: str, audio_duration: float) -> list:
    path = os.path.join(output_dir, "narration_timing.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            metadata = json.load(source)
        if metadata.get("script_hash") != _script_fingerprint(script):
            raise ValueError("Kịch bản đã thay đổi. Chạy lại Narration trước khi dựng để giọng và hình khớp nhau.")
        original = os.path.join(output_dir, "narration.mp3")
        if os.path.exists(original):
            with open(original, "rb") as source:
                audio_hash = hashlib.sha256(source.read()).hexdigest()
            if audio_hash != metadata.get("audio_hash"):
                raise ValueError("Âm thanh đã thay đổi. Chạy lại Narration để cập nhật phụ đề và nhịp dựng.")
        spans = metadata.get("sections", [])
        if (len(spans) != len(script.get("sections", [])) or not spans
                or abs(float(metadata.get("duration", 0)) - audio_duration) > 0.15):
            raise ValueError("Thời gian âm thanh không khớp. Chạy lại Narration và Music trước khi dựng.")
        if any(not math.isfinite(span["start"]) or not math.isfinite(span["end"]) or span["end"] <= span["start"] for span in spans):
            raise ValueError("Mốc giọng đọc không hợp lệ. Chạy lại Narration.")
        spans[-1]["end"] = audio_duration
        for span in spans:
            span["words"] = [word for word in metadata.get("words", [])
                             if span["start"] <= word["start_ms"] / 1000 < span["end"]]
        return spans
    subtitles = _parse_srt(os.path.join(output_dir, "narration.srt"))
    if subtitles:
        spoken = " ".join(_prepare_text(section.get("narration", "")) for section in script["sections"])
        captioned = " ".join(cue["text"] for cue in subtitles)
        words = lambda text: re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold())
        if SequenceMatcher(None, words(spoken), words(captioned), autojunk=False).ratio() < 0.75:
            raise ValueError("Phụ đề/giọng đọc cũ không khớp kịch bản. Chạy lại Narration trước khi dựng.")
    print("   ⚠ No measured speech timeline; estimating section lengths from the actual audio duration")
    sections = [(section["id"], _prepare_text(section.get("narration", ""))) for section in script["sections"]]
    return _section_spans(sections, [], audio_duration)


def _image_schedule(spans: list, indices: dict, sections: list = None) -> list:
    schedule = []
    by_id = {section["id"]: section for section in (sections or [])}
    for span in spans:
        images = indices.get(span["id"], [])
        duration = span["end"] - span["start"]
        count = max(len(images), 1)
        starts = [span["start"] + duration * index / count for index in range(count)]
        section = by_id.get(span["id"], {})
        plan = section.get("visual_plan", [])
        tokens = re.findall(r"\w+", _prepare_text(section.get("narration", "")).casefold())
        timed_tokens, word_starts = [], []
        for word in span.get("words", []):
            parts = re.findall(r"\w+", word["word"].casefold())
            timed_tokens.extend(parts)
            word_starts.extend([word["start_ms"] / 1000] * len(parts))
        if len(plan) == count and tokens:
            previous = 0
            for index, shot in enumerate(plan):
                excerpt = re.findall(r"\w+", _prepare_text(shot.get("narration_excerpt", "")).casefold())
                position = next((offset for offset in range(previous, len(tokens) - len(excerpt) + 1)
                                 if excerpt and tokens[offset:offset + len(excerpt)] == excerpt), None)
                if position is None:
                    continue
                previous = position
                if index:
                    starts[index] = (word_starts[position] if timed_tokens == tokens
                                     else span["start"] + duration * position / len(tokens))
            if any(starts[index] <= starts[index - 1] for index in range(1, len(starts))):
                starts = [span["start"] + duration * index / count for index in range(count)]
        for index in range(count):
            schedule.append({"start": starts[index],
                             "end": starts[index + 1] if index + 1 < count else span["end"],
                             "index": images[index % len(images)] if images else None})
    return schedule


# ══════════════════════════════════════════════════════════════
#  SHORTS — FAST-PACED SLIDESHOW RENDERER
# ══════════════════════════════════════════════════════════════

# How long each image stays on screen (seconds).
# The crossfade overlaps the last CROSSFADE_DURATION seconds of one image
# with the first CROSSFADE_DURATION seconds of the next, so images blend
# rather than pop.  Actual visible time per image = SHORTS_IMG_DURATION.
SHORTS_IMG_DURATION = 2.5   # seconds of visible time per image (before fade)

# Caption styling
CAP_BG        = (0, 0, 0, 180)
CAP_FG        = (255, 255, 255)
CAP_FONT_SIZE = 56           # large for vertical mobile viewing

# Vignette darkness at edges
VIGNETTE_STRENGTH = 0.28

# FIX: Vignette is now built with NumPy (vectorised) instead of a Python
# double-for-loop over millions of pixels — ~100× faster.
_vignette_cache: dict = {}


def _build_vignette(w: int, h: int) -> np.ndarray:
    """Pre-build a vignette mask using NumPy broadcasting (fast)."""
    cx, cy = w / 2, h / 2
    xs = (np.arange(w, dtype=np.float32) - cx) / cx   # -1 … +1
    ys = (np.arange(h, dtype=np.float32) - cy) / cy   # -1 … +1
    xx, yy = np.meshgrid(xs, ys)
    d    = np.sqrt(xx ** 2 + yy ** 2)                 # distance from centre
    mask = 1.0 - np.clip(d, 0.0, 1.0) * VIGNETTE_STRENGTH
    return mask[:, :, np.newaxis].astype(np.float32)   # (H, W, 1) for broadcasting


def _get_vignette():
    key = (W, H)
    if key not in _vignette_cache:
        _vignette_cache[key] = _build_vignette(W, H)
    return _vignette_cache[key]


def _apply_vignette(arr: np.ndarray) -> np.ndarray:
    v = _get_vignette()
    return np.clip(arr.astype(np.float32) * v, 0, 255).astype(np.uint8)


def _zoom_pulse(base: np.ndarray, img_t: float, img_index: int = 0,
                duration: float = SHORTS_IMG_DURATION) -> np.ndarray:
    """
    Ken Burns effect per image: slow zoom-in + subtle alternating pan.
    img_t    = time within current image slot (0 → SHORTS_IMG_DURATION).
    img_index = used to alternate pan direction each image.
    """
    progress = min(img_t / max(duration, 0.001), 1.0)
    scale    = 1.0 + 0.06 * progress          # 0 → 6% zoom

    # Alternate pan direction: even images pan right, odd images pan left
    pan_x = 0.015 * progress * (1 if img_index % 2 == 0 else -1)

    crop_w = int(W / scale)
    crop_h = int(H / scale)
    # Centre crop with pan offset
    x0 = int((W - crop_w) / 2 + pan_x * W)
    y0 = (H - crop_h) // 2
    x0 = max(0, min(x0, W - crop_w))
    y0 = max(0, min(y0, H - crop_h))
    cropped = base[y0: y0 + crop_h, x0: x0 + crop_w]
    return np.array(Image.fromarray(cropped).resize((W, H), Image.LANCZOS), dtype=np.uint8)


def _draw_shorts_caption(frame_img: Image.Image, text: str) -> Image.Image:
    """
    Burnt-in caption bar: bold text centred in the lower third,
    on a semi-transparent dark pill background.

    FIX: All compositing is done in RGBA throughout; a single .convert("RGB")
    happens at the very end — removes the redundant double-conversion from
    the original code.
    """
    if not text or not text.strip():
        return frame_img

    # Work in RGBA from the start
    base_rgba = frame_img.convert("RGBA")

    # Measure text before drawing
    measure_draw = ImageDraw.Draw(base_rgba)
    font         = _load_font("bold", CAP_FONT_SIZE)
    max_w        = W - 80
    lines        = _wrap_text(measure_draw, text.upper(), font, max_w)

    line_h  = CAP_FONT_SIZE + 12
    block_y = int(H * 0.72)     # sit in lower quarter of 9:16 frame
    pad     = 20

    # Build pill-background overlay
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od      = ImageDraw.Draw(overlay)
    for i, line in enumerate(lines):
        bbox = measure_draw.textbbox((0, 0), line, font=font)
        lw   = bbox[2] - bbox[0]
        lx   = (W - lw) // 2
        ly   = block_y + i * line_h
        od.rounded_rectangle(
            [lx - pad, ly - 6, lx + lw + pad, ly + line_h - 4],
            radius=10, fill=CAP_BG,
        )

    # Composite pill onto frame (still RGBA)
    composited = Image.alpha_composite(base_rgba, overlay)

    # Draw text directly onto composited RGBA image
    draw2 = ImageDraw.Draw(composited)
    for i, line in enumerate(lines):
        bbox = draw2.textbbox((0, 0), line, font=font)
        lw   = bbox[2] - bbox[0]
        lx   = (W - lw) // 2
        ly   = block_y + i * line_h
        _shadow_text(draw2, (lx, ly), line, font,
                     fill=CAP_FG, shadow_color=(0, 0, 0), shadow_offset=4)

    # Single conversion to RGB at the very end
    return composited.convert("RGB")


def _get_caption_at(captions: list, t_global: float) -> str:
    """Return the caption text active at global time t_global (seconds)."""
    for cap in captions:
        if cap["start"] <= t_global < cap["end"]:
            return cap["text"]
    return ""


def _build_shorts_clip(all_images: list, total_duration: float,
                       captions: list, image_timeline: list = None) -> VideoClip:
    """
    Builds the full Shorts video as one VideoClip with smooth crossfades
    between images instead of hard cuts.

    How crossfading works inside a single VideoClip make_frame():
      Each image occupies a slot of SHORTS_IMG_DURATION seconds.
      During the last CROSSFADE_DURATION seconds of a slot, we linearly
      blend the current image (zoomed) with the next image (zoomed from t=0)
      using alpha = (img_t - crossfade_start) / CROSSFADE_DURATION.
      This is equivalent to what moviepy's crossfadein/crossfadeout do,
      but computed inline so we stay in a single VideoClip (much faster
      to render than concatenating dozens of small clips with transitions).
    """
    n_imgs    = len(all_images) if all_images else 0
    xfade_dur = float(getattr(config, "CROSSFADE_DURATION", 0.7))
    # Clamp so fade never exceeds half the image slot (avoids triple-blend)
    xfade_dur = min(xfade_dur, SHORTS_IMG_DURATION * 0.45)
    slot_starts = [slot["start"] for slot in image_timeline] if image_timeline else []

    def make_frame(t):
        # ── Which image slot are we in? ──────────────────────
        slot_index = min(len(slot_starts) - 1, max(0, bisect_right(slot_starts, t) - 1)) if slot_starts else 0
        current = image_timeline[slot_index] if image_timeline else None
        if n_imgs > 0 and (current is None or current["index"] is not None):
            slot      = t / SHORTS_IMG_DURATION          # float slot index
            cur_idx   = int(slot) % n_imgs
            img_t     = t % SHORTS_IMG_DURATION          # time within slot

            # Ken Burns uses time since this image's slot START (global anchor).
            # This keeps zoom continuous across the slot boundary — the incoming
            # image's zoom progresses from wherever the xfade left off, so there
            # is no snap back to scale=1.0 on the first frame of the new slot.
            cur_slot_start = int(slot) * SHORTS_IMG_DURATION
            cur_kb_t  = t - cur_slot_start               # same as img_t, 0-based
            slot_duration = SHORTS_IMG_DURATION
            fade = xfade_dur
            if current is not None:
                cur_idx = current["index"]
                cur_slot_start = current["start"]
                img_t = cur_kb_t = t - cur_slot_start
                slot_duration = current["end"] - current["start"]
                fade = min(xfade_dur, slot_duration * 0.45)

            # Render current image with Ken Burns anchored to slot start
            cur_frame = _media_frame(all_images[cur_idx], cur_kb_t, slot_duration, cur_idx, shorts=True)
            cur_frame = _apply_overlay(cur_frame, opacity=0.18)
            cur_frame = _apply_vignette(cur_frame)

            # ── Crossfade zone: blend into next image ────────
            xfade_start = slot_duration - fade
            following = image_timeline[slot_index + 1] if image_timeline and slot_index + 1 < len(image_timeline) else None
            can_blend = not image_timeline or (following is not None and following["index"] is not None)
            if fade > 0 and img_t >= xfade_start and n_imgs > 1 and can_blend:
                # alpha: 0.0 at xfade_start → 1.0 at end of slot
                raw_alpha = min(1.0, (img_t - xfade_start) / fade)
                # Smoothstep for a more cinematic S-curve blend
                alpha = raw_alpha * raw_alpha * (3.0 - 2.0 * raw_alpha)

                next_idx      = (cur_idx + 1) % n_imgs
                next_slot_start = (int(slot) + 1) * SHORTS_IMG_DURATION
                if following is not None:
                    next_idx = following["index"]
                    next_slot_start = following["start"]
                # next image's Ken Burns time: how far past its slot start are we?
                # At xfade_start this is negative (pre-roll), clamp to 0.
                # This means the next image's zoom starts gently before its slot
                # begins, so by the time alpha=1 its zoom matches where it will
                # be at t=0 of its own slot — zero discontinuity.
                next_kb_t = max(0.0, t - next_slot_start)
                nxt_frame = _media_frame(all_images[next_idx], next_kb_t, slot_duration, next_idx, shorts=True)
                nxt_frame  = _apply_overlay(nxt_frame, opacity=0.18)
                nxt_frame  = _apply_vignette(nxt_frame)

                # Blend
                frame_np = (cur_frame.astype(np.float32) * (1.0 - alpha) +
                            nxt_frame.astype(np.float32) * alpha).astype(np.uint8)
            else:
                frame_np = cur_frame
        else:
            frame_np = _gradient_bg(t)

        frame = Image.fromarray(frame_np)

        # ── Captions ─────────────────────────────────────────
        cap_text = _get_caption_at(captions, t)
        frame    = _draw_shorts_caption(frame, cap_text)

        # ── Top brand strip ───────────────────────────────────
        draw       = ImageDraw.Draw(frame)
        font_brand = _load_font("bold", 30)
        brand      = config.CHANNEL_NAME.upper()
        bbox       = draw.textbbox((0, 0), brand, font=font_brand)
        bw         = bbox[2] - bbox[0]
        draw.text(((W - bw) // 2, 50), brand, font=font_brand,
                  fill=(255, 255, 255, 160))

        # NOTE: Flash cut indicator removed — crossfades replace hard cuts.

        return np.array(frame)

    return VideoClip(make_frame, duration=total_duration)



# ══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════

def create_video(script: dict, audio_path: str, output_dir: str,
                 image_map: dict = None, video_type: str = "normal") -> str:
    """
    Assembles the final video.
    image_map: {section_id: [photo/video path | None, …]}; None keeps a missing shot in place.
    video_type: "normal" | "shorts"
    """
    global W, H, FPS

    # Clear font cache when dimensions change (Shorts vs Normal have different
    # optimal sizes, but the same font object can be shared across renders).
    _font_cache.clear()
    _vignette_cache.clear()

    # Refresh B-roll constants from config each render (picks up GUI changes)
    global BROLL_INTERVAL, BROLL_XFADE_DUR
    BROLL_INTERVAL  = float(getattr(config, "BROLL_INTERVAL",  10.0))
    BROLL_XFADE_DUR = float(getattr(config, "BROLL_XFADE_DUR",  1.2))

    is_shorts = video_type == "shorts" or script.get("video_type") == "shorts"
    if is_shorts:
        W, H, FPS = config.SHORTS_WIDTH, config.SHORTS_HEIGHT, config.SHORTS_FPS
    else:
        W, H, FPS = config.VIDEO_WIDTH,  config.VIDEO_HEIGHT,  config.VIDEO_FPS

    audio = AudioFileClip(audio_path)
    try:
        spans = _load_section_timing(script, output_dir, audio.duration)
    except Exception:
        audio.close()
        raise
    clips = []
    media_sources = []

    # ── SHORTS PATH ────────────────────────────────────────────
    if is_shorts:
        print(f"\n   Building Shorts ({W}×{H}, {FPS} fps) …")

        # Flatten all section image lists into one ordered list
        all_images = []
        section_images = {}
        for section in script["sections"]:
            sid   = section["id"]
            section_images[sid] = []
            paths = (image_map or {}).get(sid, [])
            if isinstance(paths, str):
                paths = [paths]   # handle single-path fallback
            for p in paths:
                arr = _load_media(p)
                if arr is not None:
                    section_images[sid].append(len(all_images))
                    all_images.append(arr)
                    media_sources.append(arr)
                else:
                    section_images[sid].append(None)

        if not all_images:
            print("   ⚠ No images loaded — using animated gradient")

        # ── FIX: Load and validate SRT with clear diagnostics ──
        srt_path = os.path.join(output_dir, "narration.srt")
        captions = _parse_srt(srt_path)
        if captions:
            print(f"   → {len(captions)} caption cues loaded  ✓")
            # Sanity check: do timecodes fit within likely audio duration?
            last_end = max(c["end"] for c in captions)
            print(f"   → Caption range: 0.0s → {last_end:.1f}s")
        else:
            print("   ⚠ 0 caption cues — Shorts will render WITHOUT captions.")
            print("      To fix: delete narration.mp3 + narration.srt and re-run")
            print("      the Narration step, then re-run Video.")

        print(f"   → {len(all_images)} images → "
              f"narration-aligned photo/video shots")

        # Attach audio first to know true duration
        total_dur  = audio.duration
        print(f"   → Audio duration: {total_dur:.1f}s")

        final = _build_shorts_clip(all_images, total_dur, captions,
                                   _image_schedule(spans, section_images, script["sections"]))
        final = final.set_audio(audio)

        output_path = os.path.join(output_dir, "final_short.mp4")

    # ── NORMAL PATH ────────────────────────────────────────────
    else:
        sections       = script["sections"]
        total_sections = len(sections)

        # Load SRT captions for burnt-in subtitles
        srt_path = os.path.join(output_dir, "narration.srt")
        captions = _parse_srt(srt_path)
        if captions:
            print(f"   → {len(captions)} SRT cues loaded for normal video subtitles ✓")
        else:
            print("   → No SRT captions — subtitles will be disabled for this render.")

        print(f"\n   Building {total_sections} sections with narration-aligned B-roll:")
        clips           = []
        section_start_t = 0.0   # running global timestamp for SRT lookup

        for i, section in enumerate(sections):
            duration = spans[i]["end"] - spans[i]["start"]
            s_type   = section.get("section_type", "content")
            print(f"     [{section['id']:02d}] {section['title']:<42} {duration:5.1f}s  [{s_type}]")

            # Load all B-roll images for this section (list of np arrays)
            bg_images = []
            if image_map:
                raw = image_map.get(section["id"])
                if raw is None:
                    raw = []
                elif isinstance(raw, str):
                    raw = [raw]   # single path → list
                for p in raw:
                    arr = _load_media(p)
                    bg_images.append(arr)
                    if arr is not None:
                        media_sources.append(arr)
            if not bg_images:
                print(f"          ⚠ no images for section {section['id']} — gradient fallback")

            schedule = _image_schedule([spans[i]], {section["id"]: list(range(len(bg_images)))}, [section])
            for slot in schedule:
                slot["start"] -= spans[i]["start"]
                slot["end"] -= spans[i]["start"]
            clip = _make_clip(section, duration, bg_images, i + 1, total_sections,
                              section_start_t=section_start_t,
                              media_timeline=schedule if section.get("visual_plan") else None)
            clips.append(clip)
            section_start_t += duration   # advance global timestamp

        print("\n   → Crossfading sections…")
        final = _join_section_clips(clips, fade_dur=0.4, captions=captions)

        print("   → Attaching narration…")
        final = final.set_audio(audio)

        output_path = os.path.join(output_dir, "final_video.mp4")

    # ── EXPORT ─────────────────────────────────────────────────
    total_secs = int(final.duration)
    print(f"\n   → Rendering {total_secs // 60}m {total_secs % 60}s video…")
    print(f"      (~{max(1, total_secs // 10)} min — go get a coffee ☕)\n")

    try:
        final.write_videofile(
            output_path, fps=FPS, codec="libx264", audio_codec="aac", audio_bitrate="192k",
            threads=4, preset=getattr(config, "RENDER_PRESET", "ultrafast"), verbose=False, logger=None,
            ffmpeg_params=["-crf", "20", "-movflags", "+faststart"],
        )
    finally:
        final.close()
        audio.close()
        for clip in clips:
            clip.close()
        for source in media_sources:
            if not isinstance(source, np.ndarray):
                source.close()

    size_mb = os.path.getsize(output_path) // (1024 * 1024)
    print(f"\n   ✅ Video rendered: {output_path} ({size_mb} MB)")
    return output_path
