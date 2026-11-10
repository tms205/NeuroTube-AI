import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from edge_tts.exceptions import NoAudioReceived
from aiohttp import ClientConnectionError
from video import narrator


class NarrationRetryTests(unittest.TestCase):
    def setUp(self):
        voice_list = patch.object(narrator.edge_tts, "list_voices", new_callable=AsyncMock, return_value=[])
        self.list_voices = voice_list.start()
        self.addCleanup(voice_list.stop)

    def test_service_diagnostics_fall_back_to_same_language_and_record_effective_voice(self):
        self.list_voices.return_value = [
            {"ShortName": "vi-VN-HoaiMyNeural", "Locale": "vi-VN", "Gender": "Female"},
            {"ShortName": "vi-VN-NamMinhNeural", "Locale": "vi-VN", "Gender": "Male"},
            {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
        ]

        async def failed():
            raise NoAudioReceived("No audio was received")
            yield

        async def succeeded():
            yield {"type": "audio", "data": b"recovered"}
            yield {"type": "WordBoundary", "text": "Xin", "offset": 0, "duration": 1000000}

        def communicate(text, voice, **kwargs):
            return SimpleNamespace(stream=failed if voice == "vi-VN-HoaiMyNeural" else succeeded)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.config, "VOICE_ID", "vi-VN-HoaiMyNeural"
        ), patch.object(narrator.config, "VOICE_SMOOTH_PAUSES", False), patch.object(
            narrator.edge_tts, "Communicate", side_effect=communicate
        ) as create, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock), patch.object(
            narrator, "_get_audio_duration_ms", return_value=1000
        ):
            audio, srt = Path(directory, "narration.mp3"), Path(directory, "narration.srt")
            asyncio.run(narrator._synthesise("Xin", str(audio), str(srt), [(1, "Xin")], "script-hash"))
            self.assertEqual(audio.read_bytes(), b"recovered")
            metadata = json.loads(Path(directory, "narration_timing.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["voice"], "vi-VN-NamMinhNeural")
            self.assertEqual(metadata["requested_voice"], "vi-VN-HoaiMyNeural")
            self.assertEqual(narrator.config.VOICE_ID, "vi-VN-HoaiMyNeural")
            self.assertEqual([call.args[1] for call in create.call_args_list],
                             ["vi-VN-HoaiMyNeural"] * 3 + ["vi-VN-NamMinhNeural"])
            self.list_voices.assert_awaited_once()

    def test_network_failure_is_retried_without_changing_voice(self):
        async def failed():
            raise ClientConnectionError("Connection closed")
            yield

        async def succeeded():
            yield {"type": "audio", "data": b"complete"}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate",
            side_effect=[SimpleNamespace(stream=failed), SimpleNamespace(stream=succeeded)],
        ), patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock), patch.object(
            narrator.config, "VOICE_SMOOTH_PAUSES", False
        ), patch.object(narrator, "_get_audio_duration_ms", return_value=1000):
            audio, srt = Path(directory, "n.mp3"), Path(directory, "n.srt")
            asyncio.run(narrator._synthesise("Xin", str(audio), str(srt)))
            self.assertEqual(audio.read_bytes(), b"complete")
            self.list_voices.assert_not_awaited()

    def test_recovery_never_uses_a_voice_from_another_language(self):
        self.list_voices.return_value = [{"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"}]
        with patch.object(narrator.config, "VOICE_ID", "vi-VN-HoaiMyNeural"), patch.object(
            narrator, "_synthesise_once", new_callable=AsyncMock, side_effect=NoAudioReceived("Empty")
        ) as synth, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "Edge TTS.*3"):
                asyncio.run(narrator._synthesise("Xin", "unused.mp3", "unused.srt"))
            self.assertEqual(synth.await_count, 3)
            self.list_voices.assert_awaited_once()

    def test_unreachable_diagnostic_service_does_not_retry_forever(self):
        self.list_voices.side_effect = asyncio.TimeoutError
        with patch.object(narrator, "_synthesise_once", new_callable=AsyncMock,
                          side_effect=NoAudioReceived("Empty")) as synth, patch.object(
            narrator.asyncio, "sleep", new_callable=AsyncMock
        ):
            with self.assertRaisesRegex(RuntimeError, "Edge TTS.*3"):
                asyncio.run(narrator._synthesise("Xin", "unused.mp3", "unused.srt"))
            self.assertEqual(synth.await_count, 3)
            self.list_voices.assert_awaited_once()

    def test_all_fallbacks_failing_has_a_bounded_attempt_count(self):
        self.list_voices.return_value = [
            {"ShortName": name, "Locale": "vi-VN", "Gender": "Female"}
            for name in ("vi-VN-TestNeural", "vi-VN-BackupNeural", "vi-VN-ThirdNeural")
        ]
        with patch.object(narrator.config, "VOICE_ID", "vi-VN-HoaiMyNeural"), patch.object(
            narrator, "_synthesise_once", new_callable=AsyncMock, side_effect=NoAudioReceived("Empty")
        ) as synth, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "Edge TTS.*7"):
                asyncio.run(narrator._synthesise("Xin", "unused.mp3", "unused.srt"))
            self.assertEqual(synth.await_count, 7)
            self.assertEqual(len({call.kwargs["voice"] for call in synth.await_args_list}), 3)

    def test_retry_discards_audio_and_timing_from_failed_attempt(self):
        async def failed():
            yield {"type": "audio", "data": b"partial"}
            yield {"type": "WordBoundary", "text": "Wrong", "offset": 0, "duration": 1000000}
            raise NoAudioReceived("No audio was received")

        async def succeeded():
            yield {"type": "audio", "data": b"complete"}
            yield {"type": "WordBoundary", "text": "Xin", "offset": 1000000, "duration": 2000000}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate",
            side_effect=[SimpleNamespace(stream=failed), SimpleNamespace(stream=succeeded)],
        ) as communicate, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock), patch.object(
            narrator, "_get_audio_duration_ms", return_value=1000,
        ), patch.object(narrator.config, "VOICE_SMOOTH_PAUSES", False):
            audio = Path(directory, "narration.mp3")
            srt = Path(directory, "narration.srt")
            asyncio.run(narrator._synthesise("Xin", str(audio), str(srt)))
            self.assertEqual(audio.read_bytes(), b"complete")
            self.assertNotIn("Wrong", srt.read_text(encoding="utf-8"))
            self.assertIn("00:00:00,100", srt.read_text(encoding="utf-8"))
            self.assertEqual(communicate.call_count, 2)
            self.assertTrue(all(call.args[1] == narrator.config.VOICE_ID for call in communicate.call_args_list))

    def test_exhausted_retries_preserve_existing_outputs_and_explain_failure(self):
        async def failed():
            raise NoAudioReceived("No audio was received")
            yield

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate", return_value=SimpleNamespace(stream=failed),
        ) as communicate, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock):
            paths = [Path(directory, name) for name in ("narration.mp3", "narration.srt", "narration_timing.json")]
            for path in paths:
                path.write_bytes(b"previous-success")
            with self.assertRaisesRegex(RuntimeError, "Edge TTS.*3.*" + narrator.config.VOICE_ID):
                asyncio.run(narrator._synthesise("Xin", str(paths[0]), str(paths[1])))
            self.assertEqual(communicate.call_count, 3)
            for path in paths:
                self.assertEqual(path.read_bytes(), b"previous-success")

    def test_empty_audio_events_are_retried(self):
        async def empty():
            yield {"type": "audio", "data": b""}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate", return_value=SimpleNamespace(stream=empty),
        ) as communicate, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "Edge TTS.*3"):
                asyncio.run(narrator._synthesise("Xin", str(Path(directory, "n.mp3")), str(Path(directory, "n.srt"))))
            self.assertEqual(communicate.call_count, 3)

    def test_invalid_settings_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            narrator.edge_tts, "Communicate", side_effect=ValueError("Invalid rate"),
        ) as communicate, patch.object(narrator.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(ValueError, "Invalid rate"):
                asyncio.run(narrator._synthesise("Xin", str(Path(directory, "n.mp3")), str(Path(directory, "n.srt"))))
            self.assertEqual(communicate.call_count, 1)
            sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
