import asyncio
import io
import json
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from moviepy.editor import VideoClip
from video import creator, narrator, stock
import config
import gui


class NarrationQualityTests(unittest.TestCase):
    def test_spoken_text_cleans_markup_without_breaking_numbers_or_words(self):
        text = unicodedata.normalize("NFD", "**Bộ não** dùng phiên bản 3.13, đúng không?")
        result = narrator._prepare_text(text)
        self.assertEqual(result, "Bộ não dùng phiên bản 3.13, đúng không?")

    def test_only_long_pauses_are_shortened(self):
        cuts = narrator._pause_cuts([(1.0, 1.2), (3.0, 4.0)], 6.0, 0.45)
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0][1] - cuts[0][0], 0.55)
        self.assertAlmostEqual(narrator._retime(4.5, cuts), 3.95)
        self.assertAlmostEqual(narrator._retime(2.5, cuts), 2.5)

    def test_leading_and_trailing_silence_keep_a_short_soft_margin(self):
        cuts = narrator._pause_cuts([(0.0, 1.0), (4.0, 5.0)], 5.0, 0.45)
        self.assertAlmostEqual(cuts[0][1], 0.88)
        self.assertAlmostEqual(cuts[1][0], 4.12)

    def test_pause_processing_never_cuts_through_a_timed_word(self):
        events = [{"word": "nhỏ", "start_ms": 1200, "end_ms": 1700}]
        self.assertEqual(narrator._pause_cuts([(1.0, 2.0)], 3.0, 0.45, events), [])

    def test_caption_groups_do_not_bridge_a_speech_pause(self):
        words = [
            {"word": "Xin", "start_ms": 0, "end_ms": 100},
            {"word": "chào.", "start_ms": 120, "end_ms": 300},
            {"word": "Bắt", "start_ms": 1000, "end_ms": 1200},
            {"word": "đầu", "start_ms": 1220, "end_ms": 1400},
        ]
        blocks = narrator._group_word_events(words)
        self.assertEqual([block[2] for block in blocks], ["Xin chào.", "Bắt đầu"])

    def test_sections_follow_actual_word_times(self):
        sections = [(1, "Xin chào."), (2, "Bắt đầu.")]
        events = [
            {"word": "Xin", "start_ms": 100, "end_ms": 300},
            {"word": "chào", "start_ms": 350, "end_ms": 650},
            {"word": "Bắt", "start_ms": 2100, "end_ms": 2400},
            {"word": "đầu", "start_ms": 2450, "end_ms": 2800},
        ]
        spans = narrator._section_spans(sections, events, 3.0)
        self.assertAlmostEqual(spans[0]["end"], 2.1)
        self.assertAlmostEqual(spans[1]["start"], 2.1)
        self.assertAlmostEqual(spans[-1]["end"], 3.0)

    def test_stream_explicitly_requests_word_boundaries(self):
        async def stream():
            yield {"type": "audio", "data": b"test-audio"}
            yield {"type": "WordBoundary", "text": "Xin", "offset": 1000000, "duration": 2000000}
            yield {"type": "WordBoundary", "text": "chào", "offset": 4000000, "duration": 2000000}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate", return_value=SimpleNamespace(stream=stream)
        ) as communicate, patch.object(narrator, "_get_audio_duration_ms", return_value=1000), patch.object(
            narrator.config, "VOICE_SMOOTH_PAUSES", False, create=True
        ):
            audio = Path(directory, "narration.mp3")
            subtitle = Path(directory, "narration.srt")
            asyncio.run(narrator._synthesise("Xin chào", str(audio), str(subtitle)))
            self.assertEqual(communicate.call_args.kwargs.get("boundary"), "WordBoundary")
            self.assertIn("00:00:00,100", subtitle.read_text(encoding="utf-8"))


class VideoTimingTests(unittest.TestCase):
    def test_crossfades_do_not_shorten_narration(self):
        first = VideoClip(lambda t: np.zeros((4, 4, 3), dtype=np.uint8), duration=1.5)
        second = VideoClip(lambda t: np.full((4, 4, 3), 100, dtype=np.uint8), duration=2.5)
        result = creator._join_section_clips([first, second], fade_dur=0.4)
        self.assertAlmostEqual(result.duration, 4.0)
        self.assertTrue(np.all(result.get_frame(3.9) == 100))
        result.close()
        first.close()
        second.close()

    def test_legacy_timing_always_fills_the_actual_audio(self):
        script = {"sections": [{"id": 1, "narration": "Một hai"}, {"id": 2, "narration": "Ba bốn năm sáu"}]}
        with tempfile.TemporaryDirectory() as directory:
            spans = creator._load_section_timing(script, directory, 12.0)
        self.assertEqual(spans[0]["start"], 0)
        self.assertAlmostEqual(spans[0]["end"], 4)
        self.assertAlmostEqual(spans[-1]["end"], 12)

    def test_changed_script_cannot_reuse_old_narration_timeline(self):
        script = {"sections": [{"id": 1, "narration": "Kịch bản mới"}]}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "narration_timing.json").write_text(json.dumps({"script_hash": "old-script"}))
            with self.assertRaisesRegex(ValueError, "Narration"):
                creator._load_section_timing(script, directory, 5.0)

    def test_legacy_subtitles_from_another_script_are_rejected(self):
        script = {"sections": [{"id": 1, "narration": "Ánh sáng xanh đi qua khí quyển tạo nên màu sắc bầu trời."}]}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "narration.srt").write_text("1\n00:00:00,100 --> 00:00:02,000\nCon mèo đang nằm ngủ trên ghế trong căn nhà.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Narration"):
                creator._load_section_timing(script, directory, 5.0)

    def test_shorts_images_are_scheduled_with_the_spoken_sections(self):
        spans = [{"id": 1, "start": 0, "end": 2}, {"id": 2, "start": 2, "end": 9}]
        schedule = creator._image_schedule(spans, {1: [0, 1], 2: [2, 3]})
        self.assertTrue(all(item["index"] in (0, 1) for item in schedule if item["start"] < 2))
        self.assertTrue(all(item["index"] in (2, 3) for item in schedule if item["start"] >= 2))
        self.assertEqual(schedule[-1]["end"], 9)

    def test_single_image_has_one_continuous_camera_move(self):
        schedule = creator._image_schedule([{"id": 1, "start": 0, "end": 9}], {1: [0]})
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["end"], 9)


class StockQualityTests(unittest.TestCase):
    def test_saved_images_can_be_reused_with_current_file_names(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory, "images")
            folder.mkdir()
            for name in ("section_01_img01.jpg", "section_01_img02.jpg", "section_02.jpg"):
                folder.joinpath(name).write_bytes(b"test-image")
            image_map = stock.load_saved_images({"sections": [{"id": 1}, {"id": 2}]}, directory)
        self.assertEqual(len(image_map[1]), 2)
        self.assertEqual(len(image_map[2]), 1)

    def test_vietnamese_titles_are_not_added_to_english_stock_searches(self):
        script = {"title": "Bộ não của chúng ta", "sections": [{"id": 1, "image_query": "human brain scan"}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(stock, "_search_candidates", return_value=[]) as fetch:
            stock.download_images(script, directory)
        self.assertEqual(fetch.call_args.args[0]["query"], "human brain scan")


class VoiceSettingsTests(unittest.TestCase):
    def test_invalid_pause_limit_is_rejected_before_settings_change(self):
        for value in (-1, 5, "bad", float("nan")):
            with patch("builtins.open") as file:
                response = gui.app.test_client().post("/api/save-settings", json={"voice_max_pause": value})
            self.assertEqual(response.status_code, 400)
            file.assert_not_called()

    def test_natural_voice_options_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.py")
            path.write_text("VOICE_SMOOTH_PAUSES = True\nVOICE_MAX_PAUSE = 0.45\n", encoding="utf-8")
            with patch.object(gui, "ROOT", directory), patch.object(config, "VOICE_SMOOTH_PAUSES", True), \
                 patch.object(config, "VOICE_MAX_PAUSE", 0.45):
                response = gui.app.test_client().post("/api/save-settings", json={"voice_smooth_pauses": False, "voice_max_pause": 0.6})
            self.assertTrue(response.get_json()["ok"])
            self.assertIn("VOICE_SMOOTH_PAUSES = False", path.read_text(encoding="utf-8"))
            self.assertIn("VOICE_MAX_PAUSE = 0.6", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
