"""
Narrator — Microsoft Edge TTS (100% free)
Generates broadcast-quality voiceover + word-level subtitle (SRT) file.
The SRT is used by the Shorts creator to burn in synced captions.

Uses edge-tts 7.3.1+ with explicit timing metadata.
Uses MoviePy's AudioFileClip for duration — guaranteed to match the video render.
"""
import os
import re
import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
import edge_tts
from aiohttp import ClientError
from edge_tts.exceptions import NoAudioReceived, WebSocketError
import config

_RECOVERABLE_TTS_ERRORS = (NoAudioReceived, WebSocketError, ClientError, asyncio.TimeoutError)


# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def _prepare_text(text: str) -> str:
    """Keep spoken Vietnamese clean without changing numbers or sentence meaning."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\*\*(.*?)\*\*|__(.*?)__|`([^`]+)`", lambda match: next(group for group in match.groups() if group is not None), text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\.{3,}|…+", ", ", text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r"[,;:]\s*([,.!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _script_fingerprint(script: dict) -> str:
    content = [(section.get("id"), section.get("narration", "").strip()) for section in script.get("sections", [])]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def _pause_cuts(silences: list, duration: float, max_pause: float, word_events: list = None) -> list:
    """Shorten only long silences; retain breathing space around each edit."""
    cuts = []
    for start, end in silences:
        start, end = max(0.0, start), min(duration, end)
        if end - start < max(0.7, max_pause + 0.2):
            continue
        keep = 0.12 if start < 0.03 or end >= duration - 0.03 else max_pause
        if start < 0.03:
            cut_start, cut_end = start, end - keep
        elif end >= duration - 0.03:
            cut_start, cut_end = start + keep, end
        else:
            cut_start, cut_end = start + keep / 2, end - keep / 2
        overlaps_speech = any(cut_start < event["end_ms"] / 1000 + 0.04
                              and cut_end > event["start_ms"] / 1000 - 0.04 for event in (word_events or []))
        if cut_end > cut_start and not overlaps_speech:
            cuts.append((cut_start, cut_end))
    return cuts


def _retime(seconds: float, cuts: list) -> float:
    return max(0.0, seconds - sum(max(0.0, min(seconds, end) - start) for start, end in cuts))


def _polish_audio(source: str, output: str, word_events: list = None) -> list:
    """Cap excessive pauses and normalize loudness, preserving a timing map."""
    if not getattr(config, "VOICE_SMOOTH_PAUSES", True):
        shutil.copy2(source, output)
        return []
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        duration = _get_audio_duration_ms(source) / 1000
        maximum = min(0.8, max(0.2, float(getattr(config, "VOICE_MAX_PAUSE", 0.45))))
        detection = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", source, "-af", "silencedetect=noise=-42dB:d=0.7", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if detection.returncode:
            raise RuntimeError("Silence analysis failed")
        starts = [float(value) for value in re.findall(r"silence_start:\s*([\d.]+)", detection.stderr)]
        ends = [float(value) for value in re.findall(r"silence_end:\s*([\d.]+)", detection.stderr)]
        if len(starts) > len(ends):
            ends.append(duration)
        cuts = _pause_cuts(list(zip(starts, ends)), duration, maximum, word_events)
        segments, cursor = [], 0.0
        for start, end in cuts:
            if start > cursor:
                segments.append((cursor, start))
            cursor = end
        if cursor < duration:
            segments.append((cursor, duration))
        if not segments:
            raise ValueError("Empty speech")
        graph = []
        outputs = "".join(f"[in{i}]" for i in range(len(segments)))
        graph.append(f"[0:a]asplit={len(segments)}{outputs}")
        for index, (start, end) in enumerate(segments):
            graph.append(f"[in{index}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[part{index}]")
        inputs = "".join(f"[part{i}]" for i in range(len(segments)))
        graph.append(f"{inputs}concat=n={len(segments)}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11[out]")
        filter_path = Path(output).with_suffix(".filters.txt")
        filter_path.write_text(";\n".join(graph), encoding="utf-8")
        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", source,
             "-filter_complex_script", str(filter_path), "-map", "[out]", "-ar", "24000", "-b:a", "192k", output],
            capture_output=True, timeout=120,
        )
        if result.returncode or not Path(output).exists():
            raise RuntimeError("Audio processing failed")
        removed = sum(end - start for start, end in cuts)
        print(f"   → Natural pacing: shortened {len(cuts)} long pauses ({removed:.1f}s); normalized speech loudness")
        return cuts
    except Exception as error:
        print(f"   ⚠ Audio polish unavailable ({type(error).__name__}); keeping the original speech")
        shutil.copy2(source, output)
        return []


def _section_spans(sections: list, word_events: list, duration: float) -> list:
    counts = [len(re.findall(r"\w+", text)) for _, text in sections]
    event_counts = [len(re.findall(r"\w+", event["word"])) for event in word_events]
    boundaries = [0.0]
    if word_events and sum(event_counts) == sum(counts):
        consumed, cursor, target = 0, 0, 0
        for count in counts[:-1]:
            target += count
            while cursor < len(word_events) and consumed < target:
                consumed += event_counts[cursor]
                cursor += 1
            boundaries.append(min(duration, word_events[cursor]["start_ms"] / 1000) if cursor < len(word_events) else duration)
    else:
        total = sum(max(count, 1) for count in counts)
        cumulative = 0
        for count in counts[:-1]:
            cumulative += max(count, 1)
            boundaries.append(duration * cumulative / max(total, 1))
    boundaries.append(duration)
    return [{"id": sid, "start": boundaries[index], "end": boundaries[index + 1]}
            for index, (sid, _) in enumerate(sections)]


def _format_srt_time(ms: float) -> str:
    """Convert milliseconds → SRT timestamp  HH:MM:SS,mmm"""
    ms = max(0, int(ms))
    h  = ms // 3_600_000;  ms %= 3_600_000
    m  = ms // 60_000;     ms %= 60_000
    s  = ms // 1_000;      ms %= 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _get_audio_duration_ms(audio_path: str) -> float:
    """
    Return the exact audio duration in milliseconds using MoviePy's
    AudioFileClip — the same engine the video renderer uses, so the number
    is guaranteed to match what creator.py reports.

    Falls back to a rough file-size estimate only if MoviePy itself fails.
    """
    try:
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(audio_path)
        dur  = clip.duration          # seconds, same value creator.py gets
        clip.close()
        return dur * 1000
    except Exception as e:
        print(f"   ⚠ AudioFileClip duration failed ({e}); using file-size estimate")

    # Rough fallback: 128 kbps assumption
    try:
        return (os.path.getsize(audio_path) / (128 * 1024 / 8)) * 1000
    except Exception:
        return 60_000


# ══════════════════════════════════════════════════════════════
#  SRT WRITERS
# ══════════════════════════════════════════════════════════════

def _write_srt_blocks(blocks: list[tuple[float, float, str]], srt_path: str) -> int:
    """
    Write a list of (start_ms, end_ms, text) tuples as a valid SRT file.
    Returns the number of cues written.
    """
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (start_ms, end_ms, text) in enumerate(blocks, 1):
            f.write(f"{idx}\n")
            f.write(f"{_format_srt_time(start_ms)} --> {_format_srt_time(end_ms)}\n")
            f.write(f"{text}\n")
            f.write("\n")
    return len(blocks)


def _group_word_events(word_events: list, words_per_cue: int = 6) -> list:
    """Group word-boundary events into (start_ms, end_ms, text) caption blocks."""
    blocks = []
    chunk = []
    for event in word_events:
        if chunk and (len(chunk) >= words_per_cue or event["start_ms"] - chunk[-1]["end_ms"] > 300
                      or re.search(r"[.!?]$", chunk[-1]["word"])):
            blocks.append((chunk[0]["start_ms"], chunk[-1]["end_ms"], " ".join(word["word"] for word in chunk)))
            chunk = []
        chunk.append(event)
    if chunk:
        blocks.append((chunk[0]["start_ms"], chunk[-1]["end_ms"], " ".join(word["word"] for word in chunk)))
    return blocks


def _build_sentence_srt(text: str, audio_path: str, srt_path: str) -> int:
    """
    Fallback SRT builder using CHARACTER-based proportional timing.

    Why character-based instead of word-based:
      TTS engines speak at roughly constant character rate, not word rate.
      "if" (~100ms) and "internationalization" (~700ms) should not get the
      same time budget. Character count × ms_per_char + inter-word pause
      is a much better model of actual speech duration per word.

    Each word is allocated:
      duration_ms = len(word) * ms_per_char + INTER_WORD_MS

    Where ms_per_char is derived from total_ms / total_weighted_chars so
    that all word durations sum exactly to total_ms.

    Captions are grouped into chunks of up to 6 words. No cue ever ends
    past total_ms — the last cue is pinned to total_ms exactly.
    """
    total_ms  = _get_audio_duration_ms(audio_path)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', text) if s.strip()]
    all_words = [w for s in sentences for w in s.split()]

    if not all_words:
        open(srt_path, "w").close()
        return 0

    # Each gap between words is modelled as 1.5 "equivalent characters"
    INTER_WORD_CHAR_EQ = 1.5
    total_weighted = sum(len(w) for w in all_words) + len(all_words) * INTER_WORD_CHAR_EQ
    ms_per_char    = total_ms / total_weighted

    # Pre-compute cumulative start time for every word
    word_starts = []
    cursor      = 0.0
    for w in all_words:
        word_starts.append(cursor)
        cursor += len(w) * ms_per_char + INTER_WORD_CHAR_EQ * ms_per_char

    blocks     = []
    word_index = 0   # absolute index into all_words / word_starts

    for sentence in sentences:
        s_words = sentence.split()
        # Group sentence words into chunks of up to 6
        for ci in range(0, len(s_words), 6):
            chunk      = s_words[ci : ci + 6]
            chunk_abs  = word_index + ci
            c_start    = word_starts[chunk_abs]
            # End = start of the word AFTER the last word in chunk
            last_abs   = chunk_abs + len(chunk) - 1
            last_word  = all_words[last_abs]
            c_end      = min(
                word_starts[last_abs] + len(last_word) * ms_per_char,
                total_ms,
            )
            if c_start >= total_ms:
                break
            blocks.append((c_start, c_end, " ".join(chunk)))
        word_index += len(s_words)

    # Pin last cue to exact audio end (floating-point safety)
    if blocks:
        blocks[-1] = (blocks[-1][0], total_ms, blocks[-1][2])

    cues       = _write_srt_blocks(blocks, srt_path)
    actual_end = blocks[-1][1] / 1000 if blocks else 0.0
    print(f"   → Sentence-level SRT: {cues} cues, 0.0s → {actual_end:.1f}s "
          f"(audio: {total_ms/1000:.1f}s) — exact match ✓")
    return cues


# ══════════════════════════════════════════════════════════════
#  TTS SYNTHESIS
# ══════════════════════════════════════════════════════════════

async def _synthesise(text: str, audio_path: str, srt_path: str,
                      sections: list = None, script_hash: str = None) -> int:
    """Retry the requested voice, then diagnose and recover with a local voice."""
    requested_voice = config.VOICE_ID
    total_attempts = 3
    try:
        return await _synthesise_with_retries(text, audio_path, srt_path, sections, script_hash,
                                             requested_voice, requested_voice, 3)
    except _RECOVERABLE_TTS_ERRORS as error:
        last_error = error

    for voice in await _fallback_voices(requested_voice):
        print(f"   ⚠ Tự phục hồi: chuyển từ {requested_voice} sang giọng dự phòng {voice}")
        total_attempts += 2
        try:
            return await _synthesise_with_retries(text, audio_path, srt_path, sections, script_hash,
                                                 voice, requested_voice, 2)
        except _RECOVERABLE_TTS_ERRORS as error:
            last_error = error

    raise RuntimeError(
        f"Edge TTS failed after {total_attempts} attempts for voice {requested_voice}. "
        "Đã thử lại và tự kiểm tra dịch vụ/giọng dự phòng nhưng chưa tạo được âm thanh. "
        "File cũ được giữ lại. Dịch vụ hoặc mạng có thể đang lỗi; hãy thử lại bước Narration sau."
    ) from last_error


async def _fallback_voices(requested_voice: str) -> list[str]:
    """Check service reachability and select at most two voices of the same locale."""
    print("   → Tự kiểm tra: kết nối Microsoft và danh sách giọng khả dụng…")
    try:
        voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=15)
    except (ClientError, asyncio.TimeoutError) as error:
        print(f"   ⚠ Không kết nối được dịch vụ kiểm tra giọng ({type(error).__name__}).")
        return []
    selected = next((voice for voice in voices if voice["ShortName"] == requested_voice), None)
    locale = selected["Locale"] if selected else requested_voice.rsplit("-", 1)[0]
    if selected:
        print(f"   → Dịch vụ có phản hồi; {requested_voice} vẫn khả dụng nhưng tổng hợp đang lỗi.")
    else:
        print(f"   ⚠ Giọng {requested_voice} không có trong danh sách hiện tại.")
    candidates = [voice for voice in voices
                  if voice["Locale"] == locale and voice["ShortName"] != requested_voice]
    candidates.sort(key=lambda voice: (voice.get("Gender") != (selected or {}).get("Gender"), voice["ShortName"]))
    return [voice["ShortName"] for voice in candidates[:2]]


async def _synthesise_with_retries(text, audio_path, srt_path, sections, script_hash,
                                  voice, requested_voice, attempts):
    for attempt in range(1, attempts + 1):
        try:
            result = await _synthesise_once(text, audio_path, srt_path, sections, script_hash,
                                             voice=voice, requested_voice=requested_voice)
            print(f"   → Giọng thực tế: {voice}")
            return result
        except _RECOVERABLE_TTS_ERRORS as error:
            if attempt == attempts:
                raise
            print(f"   ⚠ Edge TTS {voice}: {type(error).__name__} "
                  f"(lần {attempt}/{attempts}); tự thử lại sau {attempt}s…")
            await asyncio.sleep(attempt)


async def _synthesise_once(text: str, audio_path: str, srt_path: str,
                           sections: list = None, script_hash: str = None, *,
                           voice: str = None, requested_voice: str = None) -> int:
    """
    Stream speech, process long pauses and retime subtitles/section boundaries.
    Returns the number of SRT cues written.
    """
    voice = voice or config.VOICE_ID
    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=config.VOICE_RATE,
        pitch=config.VOICE_PITCH,
        boundary="WordBoundary",
    )

    audio_chunks = []
    word_events = []
    sentence_events = []

    async for event in communicate.stream():
        if event["type"] == "audio" and event["data"]:
            audio_chunks.append(event["data"])

        elif event["type"] == "WordBoundary":
            offset_ms   = event["offset"]   / 10_000   # 100-ns ticks → ms
            duration_ms = event["duration"] / 10_000
            word_events.append({
                "word":     event["text"],
                "start_ms": offset_ms,
                "end_ms":   offset_ms + duration_ms,
            })

        elif event["type"] == "SentenceBoundary":
            sentence_events.append({"word": event["text"], "start_ms": event["offset"] / 10_000,
                                    "end_ms": (event["offset"] + event["duration"]) / 10_000})

    if not audio_chunks:
        raise NoAudioReceived("Edge TTS returned an empty audio stream")
    # Prepare all outputs before replacing the previous successful narration.
    with tempfile.TemporaryDirectory(prefix="speech-", dir=Path(audio_path).parent) as directory:
        raw_path = Path(directory, "original.mp3")
        final_path = Path(directory, "narration.mp3")
        subtitle_path = Path(directory, "narration.srt")
        raw_path.write_bytes(b"".join(audio_chunks))
        cuts = _polish_audio(str(raw_path), str(final_path), word_events)
        duration = _get_audio_duration_ms(str(final_path)) / 1000
        for event in word_events + sentence_events:
            event["start_ms"] = min(duration, _retime(event["start_ms"] / 1000, cuts)) * 1000
            event["end_ms"] = min(duration, _retime(event["end_ms"] / 1000, cuts)) * 1000
        if word_events:
            cue_count = _write_srt_blocks(_group_word_events(word_events), str(subtitle_path))
            print(f"   → Word timing: {len(word_events)} events, {cue_count} caption cues")
        elif sentence_events:
            cue_count = _write_srt_blocks([(event["start_ms"], event["end_ms"], event["word"])
                                           for event in sentence_events], str(subtitle_path))
            print("   → Using measured sentence timing from the speech service")
        else:
            print("   ⚠ No speech timing returned; subtitle alignment is approximate")
            cue_count = _build_sentence_srt(text, str(final_path), str(subtitle_path))
        if sections is not None:
            metadata = {
                "script_hash": script_hash, "duration": duration,
                "audio_hash": hashlib.sha256(final_path.read_bytes()).hexdigest(),
                "voice": voice, "requested_voice": requested_voice or voice,
                "rate": config.VOICE_RATE, "pitch": config.VOICE_PITCH,
                "sections": _section_spans(sections, word_events, duration),
                "words": word_events,
                "timing_source": "words" if word_events else "estimated",
            }
            timing_path = Path(directory, "narration_timing.json")
            timing_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(final_path, audio_path)
        os.replace(subtitle_path, srt_path)
        if sections is not None:
            os.replace(timing_path, Path(audio_path).with_name("narration_timing.json"))
        return cue_count


# ══════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════

def generate_narration(script: dict, output_dir: str) -> str:
    """
    Concatenates all section narrations → synthesises MP3 → writes SRT.
    Returns path to the saved audio file.
    """
    parts = []
    for section in script["sections"]:
        narration = section.get("narration", "").strip()
        if not narration:
            continue
        if narration.startswith(("Start with", "Deep explanation", "Open with")):
            continue
        parts.append((section.get("id"), _prepare_text(narration)))

    full_text = " ".join(text if re.search(r"[.!?]$", text) else text + "." for _, text in parts)
    if not full_text.strip():
        raise ValueError("No narration text; generate a complete script first.")
    os.makedirs(output_dir, exist_ok=True)
    audio_path = os.path.join(output_dir, "narration.mp3")
    srt_path   = os.path.join(output_dir, "narration.srt")

    print(f"   → Synthesising {len(full_text.split())} words via Edge TTS…")
    asyncio.run(_synthesise(full_text, audio_path, srt_path, sections=parts,
                            script_hash=_script_fingerprint(script)))

    # ── Post-run report ──────────────────────────────────────────────────────
    size_kb     = os.path.getsize(audio_path) // 1024
    audio_dur_s = _get_audio_duration_ms(audio_path) / 1000

    if not os.path.exists(srt_path):
        print(f"   ⚠ ERROR: SRT file missing at {srt_path}")
        return audio_path

    content        = open(srt_path, encoding="utf-8").read()
    verified_count = content.count(" --> ")

    # Find the last timestamp in the SRT to measure caption coverage
    timestamps = re.findall(r"--> (\d+:\d+:\d+,\d+)", content)
    if timestamps:
        def _ts_to_ms(ts: str) -> int:
            h, m, rest = ts.split(":")
            s, ms = rest.split(",")
            return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms)
        cap_end_s = max(_ts_to_ms(ts) for ts in timestamps) / 1000
    else:
        cap_end_s = 0.0

    coverage = (cap_end_s / audio_dur_s * 100) if audio_dur_s > 0 else 0

    print(f"   → Audio : {size_kb} KB  |  {audio_dur_s:.1f}s")
    print(f"   → SRT   : {verified_count} cues  |  covers 0s → {cap_end_s:.1f}s  ({coverage:.0f}%)")

    if coverage < 85:
        print(f"   ⚠ Caption coverage {coverage:.0f}% — captions will disappear before audio ends.")
        print( "      Retry Narration to refresh speech timing before rendering.")

    return audio_path
