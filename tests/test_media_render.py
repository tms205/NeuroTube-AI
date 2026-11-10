"""Small real exports to verify mixed media, missing shots and audio isolation."""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from moviepy.editor import VideoClip, VideoFileClip
from moviepy.audio.AudioClip import AudioArrayClip

import config
from video import creator


class MixedMediaRenderTests(unittest.TestCase):
    def test_both_formats_export_photos_video_and_missing_shots_with_only_narration_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "narration.wav"
            pcm = (np.sin(np.arange(24000 * 4) * 2 * np.pi * 440 / 24000) * 4000).astype("<i2")
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(pcm.tobytes())
            image = root / "photo.jpg"
            Image.new("RGB", (320, 180), (20, 50, 180)).save(image)
            footage = root / "footage.mp4"
            unwanted_audio = np.sin(np.arange(24000) * 2 * np.pi * 880 / 24000).reshape(-1, 1) * 0.8
            clip = VideoClip(lambda t: np.full((180, 320, 3), (int(50 + t * 150), 100, 30), dtype=np.uint8), duration=1)
            clip = clip.set_audio(AudioArrayClip(np.repeat(unwanted_audio, 2, axis=1), fps=24000))
            try:
                clip.write_videofile(str(footage), fps=10, codec="libx264", audio_codec="aac", verbose=False, logger=None)
            finally:
                clip.close()
            for kind in ("normal", "shorts"):
                with self.subTest(kind=kind):
                    sections = [{"id": i, "title": "Tư liệu", "section_type": "content", "narration": "Một hai",
                                 "bullet_points": [], "caption_text": ""}
                                for i in range(1, 4 if kind == "shorts" else 5)]
                    script = {"sections": sections, "video_type": kind}
                    image_map = {1: [str(image), str(footage)], 2: [None], 3: [str(footage)]}
                    if kind == "normal":
                        image_map[4] = [str(image)]
                    with patch.object(config, "VIDEO_WIDTH", 320), patch.object(config, "VIDEO_HEIGHT", 180), \
                         patch.object(config, "VIDEO_FPS", 10), patch.object(config, "SHORTS_WIDTH", 180), \
                         patch.object(config, "SHORTS_HEIGHT", 320), patch.object(config, "SHORTS_FPS", 10), \
                         patch.object(creator, "CAP_FONT_SIZE", 14):
                        result = creator.create_video(script, str(audio), directory, image_map, video_type=kind)
                    with VideoFileClip(result) as rendered:
                        self.assertAlmostEqual(rendered.duration, 4, delta=0.12)
                        # Explicit sample times avoid MoviePy 1.x's generator stacking on NumPy 2.
                        samples = rendered.audio.to_soundarray(tt=np.arange(2.8, 3.8, 1 / 24000)).mean(axis=1)
                        spectrum = np.abs(np.fft.rfft(samples))
                        peak = np.fft.rfftfreq(len(samples), 1 / 24000)[spectrum.argmax()]
                        self.assertAlmostEqual(peak, 440, delta=2)
                        expected = (320, 180) if kind == "shorts" else (180, 320)
                        self.assertEqual(rendered.get_frame(3.8).shape[:2], expected)
