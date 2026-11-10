import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import gui
from agents import gemini_client


class GeminiModelTests(unittest.TestCase):
    def test_generate_request_uses_the_model_sent_by_the_ui(self):
        with patch.dict(gui._state, {"running": False}), patch.object(
            config, "GEMINI_MODEL", "gemini-3.6-flash"
        ), patch.object(gui.threading, "Thread"):
            response = gui.app.test_client().post("/api/run", json={
                "steps": ["research"], "gemini_model": "gemini-3.8-flash",
            })
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(config.GEMINI_MODEL, "gemini-3.8-flash")
            self.assertEqual(response.get_json()["gemini_model"], "gemini-3.8-flash")
            self.assertTrue(gui._state["running"])

    def test_invalid_generate_model_does_not_start_a_pipeline(self):
        with patch.dict(gui._state, {"running": False}), patch.object(gui.threading, "Thread") as thread:
            response = gui.app.test_client().post("/api/run", json={"gemini_model": "invalid model"})
            self.assertEqual(response.status_code, 400)
            thread.assert_not_called()

    def test_settings_cannot_change_the_model_of_a_running_pipeline(self):
        with patch.dict(gui._state, {"running": True}), patch.object(
            config, "GEMINI_MODEL", "gemini-3.8-flash"
        ), patch("builtins.open") as config_file:
            response = gui.app.test_client().post("/api/save-settings", json={"gemini_model": "gemini-3.6-flash"})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(config.GEMINI_MODEL, "gemini-3.8-flash")
            config_file.assert_not_called()

    def test_future_model_is_not_silently_replaced(self):
        chain = gemini_client.build_chain("models/gemini-future-flash")
        self.assertEqual(chain[0], "gemini-future-flash")
        self.assertEqual(len(chain), len(set(chain)))

    def test_selected_model_outside_old_chain_is_used_first(self):
        self.assertEqual(
            gemini_client.build_chain("gemini-4-argon")[0], "gemini-4-argon"
        )

    def test_generate_uses_selected_model(self):
        used_models = []

        def generate_content(**kwargs):
            used_models.append(kwargs["model"])
            return SimpleNamespace(text=" Script ready ")

        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        with patch.object(gemini_client, "_get_client", return_value=client):
            self.assertEqual(gemini_client.generate("Write a script", "gemini-4-argon"), "Script ready")
        self.assertEqual(used_models, ["gemini-4-argon"])

    def test_listing_excludes_models_for_other_tasks(self):
        models = [
            SimpleNamespace(name="models/" + name, supported_actions=actions)
            for name, actions in [
                ("gemini-4-argon", ["generateContent"]),
                ("gemini-3.8-flash", ["generateContent"]),
                ("gemini-3.8-flash-tts", ["generateContent"]),
                ("gemini-3.8-live", ["bidiGenerateContent"]),
                ("gemini-3.1-flash-image", ["generateContent"]),
                ("gemini-nano-banana-2.1", ["generateContent"]),
                ("gemini-robotics-er-2-preview", ["generateContent"]),
                ("gemini-embedding-001", ["embedContent"]),
                ("gemini-omni-1.1-flash", ["generateContent"]),
            ]
        ]
        client = SimpleNamespace(models=SimpleNamespace(list=lambda **kwargs: iter(models)))
        with patch.object(gemini_client, "_get_client", return_value=client):
            result = gemini_client.get_available_models(refresh=True)
        self.assertEqual(set(result["models"]), {"gemini-4-argon", "gemini-3.8-flash"})
        self.assertEqual(result["source"], "api")

    def test_listing_failure_has_fallback_without_exposing_key(self):
        def unavailable(**kwargs):
            raise RuntimeError("network error containing a private API key")

        client = SimpleNamespace(models=SimpleNamespace(list=unavailable))
        with patch.object(gemini_client, "_get_client", return_value=client):
            result = gemini_client.get_available_models(refresh=True)
        self.assertTrue(result["models"])
        self.assertEqual(result["source"], "fallback")
        self.assertNotIn("private API key", result.get("warning", ""))

    def test_models_endpoint(self):
        expected = {"models": ["gemini-4-argon"], "source": "api"}
        with patch.object(gemini_client, "get_available_models", return_value=expected, create=True):
            response = gui.app.test_client().get("/api/models?refresh=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["models"], expected["models"])

    def test_invalid_model_is_rejected_before_writing_config(self):
        original_model = config.GEMINI_MODEL
        with patch("builtins.open") as config_file:
            response = gui.app.test_client().post(
                "/api/save-settings", json={"gemini_model": 'gemini-4-argon"; invalid'}
            )
        config.GEMINI_MODEL = original_model
        self.assertEqual(response.status_code, 400)
        config_file.assert_not_called()

    def test_new_key_replaces_cached_client(self):
        with patch.object(gemini_client, "_client", None), patch.object(
            gemini_client.genai, "Client"
        ) as create_client, patch.object(config, "GEMINI_API_KEY", "first-test-key"):
            gemini_client._get_client()
            config.GEMINI_API_KEY = "second-test-key"
            gemini_client._get_client()
        self.assertEqual(create_client.call_count, 2)

    def test_save_model_preserves_utf8_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "config.py")
            config_path.write_text(
                '# Kênh tiếng Việt\nGEMINI_MODEL = "gemini-3.6-flash"\n', encoding="utf-8"
            )
            with patch.object(gui, "ROOT", directory), patch.object(config, "GEMINI_MODEL"):
                response = gui.app.test_client().post(
                    "/api/save-settings", json={"gemini_model": "gemini-4-argon"}
                )
            self.assertTrue(response.get_json()["ok"])
            saved = config_path.read_text(encoding="utf-8")
            self.assertIn('GEMINI_MODEL = "gemini-4-argon"', saved)
            self.assertIn("Kênh tiếng Việt", saved)


if __name__ == "__main__":
    unittest.main()
