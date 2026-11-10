import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import researcher, scriptwriter
from video import stock, creator
import gui


TOPIC = "Vì sao bầu trời có màu xanh?"


def brief():
    return {
        "topic": TOPIC, "video_title": "Màu xanh trên đầu chúng ta",
        "key_points": ["Ánh sáng bị tán xạ"], "tags": ["ánh sáng"],
        "central_answer": "Khí quyển tán xạ ánh sáng xanh mạnh hơn ánh sáng đỏ.",
        "outline": [{"question": TOPIC, "answer": "Do sự tán xạ ánh sáng.",
                     "visual_subject": "blue sky", "transition": "Giải thích cơ chế."}],
    }


def script():
    sections = []
    for i, (kind, narration, subject) in enumerate([
        ("hook", "Bạn có bao giờ tự hỏi vì sao bầu trời lại có màu xanh?", "blue sky"),
        ("content", "Ánh sáng mặt trời chứa nhiều màu, nhưng khí quyển tán xạ ánh sáng xanh mạnh hơn.", "sunlight atmosphere"),
        ("conclusion", "Đó là lý do chúng ta nhìn thấy màu xanh khi ngước lên trời.", "clear blue sky"),
    ], 1):
        sections.append({"id": i, "section_type": kind, "title": "Bầu trời xanh",
                         "narration": narration, "visual_plan": [
                             {"subject": subject, "query": subject, "video_query": subject,
                              "narration_excerpt": narration, "purpose": "Minh họa điều đang giải thích."}],
                         "image_query": subject, "bullet_points": [], "caption_text": ""})
    return {"topic": TOPIC, "title": "Bầu trời xanh", "description": "Ánh sáng và bầu trời.",
            "tags": ["bầu trời"], "sections": sections}


class ContentQualityTests(unittest.TestCase):
    def test_exact_topic_cannot_be_silently_replaced(self):
        wrong = brief()
        wrong["topic"] = "Bí mật hố đen"
        with patch.object(researcher, "generate", return_value=json.dumps(wrong)):
            with self.assertRaisesRegex(ValueError, "chủ đề"):
                researcher.research_topic("Kênh kiến thức", TOPIC)

    def test_research_repairs_missing_plan_and_preserves_requested_topic(self):
        invalid = brief()
        invalid.pop("central_answer")
        with patch.object(researcher, "generate", side_effect=[json.dumps(invalid), json.dumps(brief())]):
            result = researcher.research_topic("Kênh kiến thức", TOPIC)
        self.assertEqual(result["requested_topic"], TOPIC)
        self.assertEqual(result["central_answer"], brief()["central_answer"])
        self.assertEqual(result["verification_status"], "not_web_verified")

    def test_script_is_revised_when_editor_finds_topic_drift(self):
        draft = script()
        draft["sections"][1]["narration"] = "Hố đen có nhiều bí ẩn trong vũ trụ."
        draft["sections"][1]["visual_plan"][0]["narration_excerpt"] = draft["sections"][1]["narration"]
        outputs = [draft, {"approved": False, "issues": ["Đoạn 2 lạc sang hố đen."]},
                   script(), {"approved": True, "issues": []}]
        with patch.object(scriptwriter, "generate", side_effect=[json.dumps(value) for value in outputs]):
            result = scriptwriter.write_script(brief(), "shorts")
        self.assertEqual(result["sections"][1]["narration"], script()["sections"][1]["narration"])
        self.assertTrue(result["content_review"]["approved"])
        self.assertEqual(result["topic"], TOPIC)

    def test_script_stops_if_editor_still_finds_unresolved_problems(self):
        rejected = {"approved": False, "issues": ["Chưa trả lời câu hỏi mở đầu."]}
        outputs = [script(), rejected, script(), rejected]
        with patch.object(scriptwriter, "generate", side_effect=[json.dumps(value) for value in outputs]):
            with self.assertRaisesRegex(ValueError, "Chưa trả lời"):
                scriptwriter.write_script(brief(), "shorts")

    def test_duplicate_section_ids_are_repaired_before_editing(self):
        invalid = script()
        invalid["sections"][1]["id"] = 1
        outputs = [invalid, script(), {"approved": True, "issues": []}]
        with patch.object(scriptwriter, "generate", side_effect=[json.dumps(value) for value in outputs]):
            result = scriptwriter.write_script(brief(), "shorts")
        self.assertEqual([s["id"] for s in result["sections"]], [1, 2, 3])

    def test_matching_request_label_cannot_hide_a_different_topic(self):
        data = {"requested_topic": TOPIC, "topic": "Hố đen"}
        with self.assertRaisesRegex(ValueError, "chủ đề"):
            researcher.validate_topic(data, TOPIC)

    def test_review_screen_exposes_the_content_brief(self):
        with patch.dict(gui._state, {"research": brief(), "script": script(), "description": ""}):
            result = gui.app.test_client().get("/api/output").get_json()
        self.assertEqual(result["content_plan"]["central_answer"], brief()["central_answer"])
        self.assertEqual(result["content_plan"]["outline"], brief()["outline"])

    def test_skipping_research_does_not_reuse_a_different_topic(self):
        import config
        with tempfile.TemporaryDirectory() as directory:
            previous = brief()
            previous["topic"] = "Hố đen"
            Path(directory, "research.json").write_text(json.dumps(previous), encoding="utf-8")
            with patch.dict(gui._state, {"error": None}), patch.object(config, "OUTPUT_DIR", directory), \
                 patch.object(config, "IMAGES_DIR", directory):
                gui._run_pipeline([], topic_override=TOPIC)
                self.assertIn("chủ đề", gui._state["error"])

    def test_description_uses_the_final_script_without_canned_hype_or_unreviewed_research_claims(self):
        research = brief()
        research["key_points"] = ["An unreviewed claim from an old research draft"]
        result = gui._build_description(script(), research, video_type="shorts")
        self.assertIn(script()["description"], result)
        self.assertNotIn("unreviewed claim", result)
        self.assertNotIn("mind-bending", result)
        self.assertNotIn("every week", result)


class VisualQualityTests(unittest.TestCase):
    def test_missing_subject_never_searches_for_generic_space_filler(self):
        response = type("Response", (), {"status_code": 200, "ok": True, "raise_for_status": lambda s: None,
                                        "json": lambda s: {"photos": []}})()
        with tempfile.TemporaryDirectory() as directory, patch.object(stock.requests, "get", return_value=response) as get:
            result = stock._fetch_images("red panda bamboo", 1, directory)
        self.assertEqual(result, [])
        self.assertEqual({call.kwargs["params"]["query"] for call in get.call_args_list}, {"red panda bamboo"})

    def test_saved_media_rejects_another_scripts_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "media_manifest.json").write_text(json.dumps({"script_hash": "old", "assets": []}))
            with self.assertRaisesRegex(ValueError, "Images|Tư liệu"):
                stock.load_saved_images(script(), directory)

    def test_video_file_selection_keeps_mp4_and_sensible_resolution(self):
        files = [
            {"file_type": "video/mp4", "width": 3840, "height": 2160, "link": "4k"},
            {"file_type": "video/mp4", "width": 1280, "height": 720, "link": "hd"},
            {"file_type": "video/webm", "width": 1920, "height": 1080, "link": "webm"},
        ]
        self.assertEqual(stock._choose_video_file(files, "landscape")["link"], "hd")

    def test_visual_review_can_reject_all_candidates(self):
        candidates = [{"candidate_id": "c1", "shot": 0, "kind": "image", "query": "blue sky",
                       "preview_bytes": b"preview", "subject": "blue sky"}]
        verdict = {"selections": [], "missing_shots": [0]}
        with patch.object(stock, "generate", return_value=json.dumps(verdict)):
            result = stock._review_candidates(TOPIC, script()["sections"][0], candidates)
        self.assertEqual(result, [])

    def test_visual_review_cannot_choose_an_invented_id_or_another_shots_candidate(self):
        candidate = {"candidate_id": "known", "identity": "image:1", "shot": 0, "kind": "image",
                     "query": "sky", "preview_bytes": b"preview", "subject": "sky"}
        verdict = {"selections": [
            {"candidate_id": "made-up", "shot": 0, "match": "direct", "reason": "Không tồn tại"},
            {"candidate_id": "known", "shot": 1, "match": "direct", "reason": "Sai cảnh"}]}
        with patch.object(stock, "generate", return_value=json.dumps(verdict)):
            self.assertEqual(stock._review_candidates(TOPIC, script()["sections"][0], [candidate]), [])

    def test_download_saves_video_and_a_placeholder_without_relabelling_missing_shots(self):
        section = script()["sections"][0]
        section["visual_plan"] *= 2
        candidate = {"candidate_id": "selected", "identity": "video:1", "shot": 1, "kind": "video",
                     "query": "blue sky", "subject": "sky", "preview_bytes": b"preview",
                     "download_url": "https://videos.pexels.com/test.mp4", "source_url": "https://www.pexels.com/video/1/",
                     "creator": "Test creator", "match": "direct", "reason": "Có bầu trời xanh"}
        with tempfile.TemporaryDirectory() as directory, patch.object(stock, "_search_candidates", return_value=[]), \
             patch.object(stock, "_review_candidates", return_value=[candidate]), patch.object(stock, "_download_bytes", return_value=b"video"):
            data = {"topic": TOPIC, "sections": [section]}
            result = stock.download_images(data, directory)
            loaded = stock.load_saved_images(data, directory)
            self.assertIsNone(result[1][0])
            self.assertTrue(result[1][1].endswith(".mp4"))
            self.assertEqual(loaded, result)
            self.assertTrue(Path(result[1][1]).with_suffix(".poster.jpg").is_file())

    def test_renderer_reads_video_frames_and_does_not_use_its_audio(self):
        import numpy as np
        from moviepy.editor import VideoClip
        clip = VideoClip(lambda t: np.full((12, 16, 3), int(t * 80), dtype=np.uint8), duration=2)
        with patch.object(creator, "W", 16), patch.object(creator, "H", 12):
            frame = creator._media_frame(clip, 1.0, 2.0)
        self.assertAlmostEqual(float(frame.mean()), 80, delta=2)
        clip.close()

    def test_shots_follow_story_order_without_restarting_previous_footage(self):
        result = creator._image_schedule([{"id": 1, "start": 0, "end": 20}], {1: [0, 1]})
        self.assertEqual([slot["index"] for slot in result], [0, 1])

    def test_missing_shot_keeps_its_place_in_saved_storyboard(self):
        data = script()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "photo.jpg").write_bytes(b"image")
            manifest = {"script_hash": stock._media_fingerprint(data), "coverage": [
                {"section_id": 1, "planned": 2}], "assets": [
                {"section_id": 1, "shot": 1, "path": "photo.jpg"}]}
            Path(directory, "media_manifest.json").write_text(json.dumps(manifest))
            result = stock.load_saved_images(data, directory)
        self.assertIsNone(result[1][0])
        self.assertTrue(result[1][1].endswith("photo.jpg"))

    def test_visual_change_follows_its_spoken_excerpt(self):
        section = {"id": 1, "narration": "Một hai ba bốn", "visual_plan": [
            {"narration_excerpt": "Một hai"}, {"narration_excerpt": "ba bốn"}]}
        events = [{"word": word, "start_ms": start, "end_ms": start + 300}
                  for word, start in zip(["Một", "hai", "ba", "bốn"], [100, 500, 3100, 3600])]
        span = {"id": 1, "start": 0, "end": 4, "words": events}
        result = creator._image_schedule([span], {1: [0, 1]}, sections=[section])
        self.assertAlmostEqual(result[1]["start"], 3.1)

    def test_legacy_scripts_still_respect_the_broll_interval_setting(self):
        import numpy as np
        sources = [np.zeros((12, 16, 3), dtype=np.uint8), np.full((12, 16, 3), 200, dtype=np.uint8)]
        with patch.object(creator, "W", 16), patch.object(creator, "H", 12), \
             patch.object(creator, "BROLL_INTERVAL", 4), patch.object(creator, "BROLL_XFADE_DUR", 0), \
             patch.object(creator, "_apply_overlay", side_effect=lambda frame, *args: frame):
            self.assertGreater(float(creator._section_background(sources, 5, 20).mean()), 150)

    def test_short_footage_keeps_moving_when_the_spoken_shot_is_longer(self):
        import numpy as np
        from moviepy.editor import VideoClip
        clip = VideoClip(lambda t: np.full((12, 16, 3), int(t * 80), dtype=np.uint8), duration=2)
        with patch.object(creator, "W", 16), patch.object(creator, "H", 12):
            before = creator._media_frame(clip, 2.4, 5)
            after = creator._media_frame(clip, 2.7, 5)
            left = creator._media_frame(clip, 1.99, 5)
            right = creator._media_frame(clip, 2.01, 5)
        self.assertGreater(abs(float(before.mean()) - float(after.mean())), 10)
        self.assertLess(abs(float(left.mean()) - float(right.mean())), 10)
        clip.close()


if __name__ == "__main__":
    unittest.main()
