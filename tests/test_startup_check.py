import datetime
import io
import json
import pickle
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gui
import startup_check


class StartupCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.joinpath("requirements.txt").write_text(
            "moviepy==1.0.3\nedge-tts>=7.3.1,<8\n", encoding="utf-8"
        )
        self.checker = startup_check.Checker(self.root)

    def plan_runner(self, args, timeout=180):
        if "--report" in args:
            report_path = Path(args[args.index("--report") + 1])
            report_path.write_text(json.dumps({"install": [
                {"metadata": {"name": "edge-tts", "version": "7.3.2"}}
            ]}))
        if "list" in args:
            return SimpleNamespace(returncode=0, stdout=json.dumps([
                {"name": "moviepy", "version": "1.0.3", "latest_version": "2.2.1"}
            ]))
        return SimpleNamespace(returncode=0, stdout="")

    def test_updates_resolved_dependencies_and_reports_pinned_versions(self):
        with patch.object(self.checker, "_run_pip", side_effect=self.plan_runner) as run:
            self.checker.check_dependencies(auto_update=True)
        install_calls = [call.args[0] for call in run.call_args_list if "install" in call.args[0]]
        self.assertTrue(any("--dry-run" in args for args in install_calls))
        self.assertTrue(any("--dry-run" not in args for args in install_calls))
        self.assertTrue(all("-r" in args for args in install_calls))
        self.assertIn("moviepy", str(self.checker.findings).lower())
        self.assertIn("2.2.1", str(self.checker.findings))

    def test_read_only_check_never_installs_updates(self):
        with patch.object(self.checker, "_run_pip", side_effect=self.plan_runner) as run:
            self.checker.check_dependencies(auto_update=False)
        self.assertFalse(any(
            "install" in call.args[0] and "--dry-run" not in call.args[0]
            for call in run.call_args_list
        ))
        self.assertIn("7.3.2", str(self.checker.findings))

    def test_failed_update_does_not_report_success_or_echo_secrets(self):
        def fail_install(args, timeout=180):
            if "install" in args and "--dry-run" not in args:
                return SimpleNamespace(returncode=1, stdout="private-key-in-pip-output")
            return self.plan_runner(args, timeout)

        with patch.object(self.checker, "_run_pip", side_effect=fail_install), redirect_stdout(io.StringIO()) as output:
            self.checker.check_dependencies(auto_update=True)
        self.assertNotIn("private-key", output.getvalue() + str(self.checker.findings))
        self.assertTrue(any(item["level"] == "warning" for item in self.checker.findings))

    def test_placeholder_key_does_not_send_request(self):
        with patch.object(startup_check, "http_json") as request:
            self.checker.check_gemini("YOUR_GEMINI_KEY", "gemini-3.8-flash")
        request.assert_not_called()
        self.assertIn("GEMINI_API_KEY", str(self.checker.findings))

    def test_http_probe_identifies_itself_to_avoid_false_pexels_rejections(self):
        response = io.BytesIO(b'{"photos": []}')
        response.status = 200
        response.headers = {}
        with patch.object(startup_check.urllib.request, "urlopen", return_value=response) as request:
            status, _, _ = startup_check.http_json("https://api.pexels.com/v1/search", {"Authorization": "test-key"})
        self.assertEqual(status, 200)
        self.assertTrue(request.call_args.args[0].get_header("User-agent"))

    def test_running_studio_prevents_package_updates(self):
        with patch.object(startup_check, "ROOT", self.root), patch.object(startup_check.sys, "prefix", str(self.root / "venv")), \
             patch.object(startup_check, "Checker", return_value=self.checker), \
             patch.object(startup_check, "studio_running", return_value=True), \
             patch.object(self.checker, "check_dependencies") as update:
            code = startup_check.main([])
        self.assertEqual(code, 2)
        update.assert_not_called()

    def test_youtube_service_outage_is_not_reported_as_revoked_access(self):
        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials
        token_path = self.make_credentials()
        with patch.object(Credentials, "refresh", side_effect=RefreshError("server_error", retryable=True)):
            self.checker.check_youtube(self.root / "missing-secret.json", token_path)
        self.assertNotIn("thu hồi", str(self.checker.findings).lower())

    def test_rejected_key_has_action_without_exposing_credential(self):
        with patch.object(startup_check, "http_json", return_value=(403, {}, {})):
            self.checker.check_gemini("private-test-key", "gemini-3.8-flash")
        report = str(self.checker.findings)
        self.assertNotIn("private-test-key", report)
        self.assertTrue(self.checker.findings[-1]["action"])

    def test_offline_is_not_reported_as_an_expired_key(self):
        with patch.object(startup_check, "http_json", return_value=(None, {}, {})):
            self.checker.check_gemini("private-test-key", "gemini-3.8-flash")
        self.assertIn("chưa xác minh", str(self.checker.findings).lower())
        self.assertNotIn("hết hạn", str(self.checker.findings).lower())

    def test_missing_selected_model_is_reported(self):
        with patch.object(startup_check, "http_json", return_value=(200, {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}
        ]}, {})):
            self.checker.check_gemini("private-test-key", "gemini-retired-flash")
        self.assertIn("gemini-retired-flash", str(self.checker.findings))
        self.assertTrue(any(item["level"] == "warning" for item in self.checker.findings))

    def make_credentials(self):
        from google.oauth2.credentials import Credentials
        credentials = Credentials(
            token="old-test-access", refresh_token="test-refresh",
            client_id="test-client", client_secret="test-client-secret",
            token_uri="https://oauth2.googleapis.com/token",
        )
        credentials.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=1)
        path = self.root / "youtube_token.pickle"
        path.write_bytes(pickle.dumps(credentials))
        return path

    def test_expired_youtube_access_token_is_refreshed_and_saved(self):
        from google.oauth2.credentials import Credentials
        token_path = self.make_credentials()

        def refresh(credentials, request):
            credentials.token = "new-test-access"
            credentials.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)

        with patch.object(Credentials, "refresh", refresh):
            self.checker.check_youtube(self.root / "missing-secret.json", token_path)
        with token_path.open("rb") as source:
            saved = startup_check.CredentialReader(source).load()
        self.assertEqual(saved.token, "new-test-access")
        self.assertNotIn("test-refresh", str(self.checker.findings))

    def test_revoked_youtube_token_requests_login_and_preserves_file(self):
        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials
        token_path = self.make_credentials()
        original = token_path.read_bytes()
        with patch.object(Credentials, "refresh", side_effect=RefreshError("invalid_grant")):
            self.checker.check_youtube(self.root / "missing-secret.json", token_path)
        self.assertEqual(token_path.read_bytes(), original)
        self.assertIn("đăng nhập", str(self.checker.findings).lower())

    def test_credential_loader_rejects_unexpected_pickle_classes(self):
        source = io.BytesIO(pickle.dumps(SimpleNamespace(token="untrusted")))
        with self.assertRaises(pickle.UnpicklingError):
            startup_check.CredentialReader(source).load()

    def test_startup_report_is_available_in_gui(self):
        self.root.joinpath("output").mkdir()
        report = {"checked_at": "2026-10-17T12:00:00+00:00", "findings": [
            {"level": "warning", "component": "Gemini", "message": "Thiếu key", "action": "Mở Settings"}
        ]}
        self.root.joinpath("output", "startup_report.json").write_text(json.dumps(report))
        with patch.object(gui, "ROOT", str(self.root)):
            response = gui.app.test_client().get("/api/startup-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), report)


if __name__ == "__main__":
    unittest.main()
