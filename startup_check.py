"""Check and maintain this project's environment before the studio starts.

Only dependency versions declared in requirements.txt may be installed.
Reports contain status messages, never credentials or raw service errors.
"""
import argparse
import asyncio
import datetime
import functools
import importlib
import importlib.metadata
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def http_json(url, headers=None):
    """Return HTTP status/data without leaking credential-bearing exceptions."""
    request = urllib.request.Request(url, headers={"User-Agent": "YouTubeAIStudio/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, {}, {}
    except (OSError, ValueError):
        return None, {}, {}


def missing_key(value):
    return not isinstance(value, str) or not value.strip() or any(
        marker in value.upper() for marker in ("YOUR_", "REPLACE_", "CHANGEME", "...")
    )


class CredentialReader(pickle.Unpickler):
    """Read the app's saved Google credentials, rejecting other pickle classes."""
    def find_class(self, module, name):
        allowed = {
            ("google.oauth2.credentials", "Credentials"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryManager"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryData"),
            ("google.auth._regional_access_boundary_utils", "_RegionalAccessBoundaryRefreshManager"),
            ("datetime", "datetime"), ("datetime", "timedelta"), ("datetime", "timezone"),
        }
        if (module, name) not in allowed:
            raise pickle.UnpicklingError("Unsupported credential class")
        return getattr(importlib.import_module(module), name)


class Checker:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.findings = []

    def add(self, level, component, message, action=""):
        item = dict(level=level, component=component, message=message, action=action)
        self.findings.append(item)
        print(f"[{level.upper()}] {component}: {message}", flush=True)
        if action:
            print(f"    → {action}", flush=True)

    def _run_pip(self, args, timeout=180):
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pip", "--disable-pip-version-check",
             "--no-input", *args], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )

    def check_dependencies(self, auto_update=True):
        self.add("info", "Thư viện", "Đang kiểm tra bản mới và giải quyết tương thích…")
        install = ["install", "--upgrade", "--upgrade-strategy", "eager", "--timeout", "10",
                   "--retries", "1", "-r", str(self.root / "requirements.txt")]
        planned_names = set()
        updates_applied = False
        try:
            with tempfile.TemporaryDirectory(prefix="studio-update-") as directory:
                report_path = Path(directory, "plan.json")
                result = self._run_pip([*install, "--dry-run", "--report", str(report_path)])
                if result.returncode:
                    self.add("warning", "Cập nhật", "Chưa xác minh được các bản cập nhật.",
                             "Kiểm tra mạng và requirements.txt. Giữ môi trường hiện tại nếu vẫn hoạt động.")
                else:
                    plan = json.loads(report_path.read_text(encoding="utf-8"))
                    changes = [item["metadata"] for item in plan.get("install", [])]
                    planned_names = {re.sub(r"[-_.]+", "-", item["name"].lower()) for item in changes}
                    for item in changes:
                        self.add("info", "Bản mới", f"{item['name']} → {item['version']}")
                    if changes and auto_update:
                        self.save_dependency_snapshot()
                        self.add("info", "Cập nhật", "Đang cài các phiên bản tương thích, vui lòng đợi…")
                        result = self._run_pip(install, timeout=300)
                        if result.returncode:
                            self.add("warning", "Cập nhật", "Cài đặt chưa hoàn tất; sẽ kiểm tra lại môi trường.",
                                     "Kiểm tra mạng/dung lượng rồi mở run.bat để thử lại.")
                        else:
                            updates_applied = True
                            for item in changes:
                                self.add("ok", "Đã cập nhật", f"{item['name']} {item['version']}")
                    elif changes:
                        self.add("warning", "Cập nhật", "Có bản mới tương thích; chế độ này chỉ kiểm tra.",
                                 "Mở run.bat bình thường để tự cập nhật.")
                    else:
                        self.add("ok", "Thư viện", "Đã dùng các phiên bản mới nhất trong giới hạn tương thích.")
            result = self._run_pip(["list", "--outdated", "--format=json", "--timeout", "5", "--retries", "0"], timeout=90)
            if result.returncode == 0:
                for item in json.loads(result.stdout):
                    name = re.sub(r"[-_.]+", "-", item["name"].lower())
                    if name in planned_names and not updates_applied:
                        continue
                    self.add("info", "Phiên bản giữ lại",
                             f"{item['name']} {item['version']}: có {item['latest_version']} mới hơn.",
                             "Giữ theo ràng buộc tương thích của dự án/phụ thuộc; bản mới này chưa được tự cài.")
            else:
                self.add("warning", "Phiên bản khác", "Chưa kiểm tra được các phiên bản ngoài giới hạn tương thích.")
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            self.add("warning", "Cập nhật", "Không hoàn tất kiểm tra/cập nhật thư viện.",
                     "Kiểm tra mạng rồi thử lại. Môi trường sẽ được kiểm tra trước khi mở ứng dụng.")

    def save_dependency_snapshot(self):
        output = self.root / "output"
        output.mkdir(exist_ok=True)
        versions = sorted(
            f"{dist.metadata['Name']}=={dist.version}"
            for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
        )
        (output / "dependencies_before_update.txt").write_text("\n".join(versions) + "\n", encoding="utf-8")

    def check_runtime(self):
        """Use a fresh process so imports reflect any just-installed updates."""
        code = '''
import importlib,json,tempfile
from pathlib import Path
failures=[]
for module in ("google.genai","requests","edge_tts","numpy","PIL","flask",
               "googleapiclient.discovery","google_auth_oauthlib.flow","google_auth_httplib2",
               "video.creator","video.narrator"):
    try: importlib.import_module(module)
    except Exception as error: failures.append(module + ": " + type(error).__name__)
if not failures:
    try:
        import numpy as np
        from moviepy.editor import VideoClip
        with tempfile.TemporaryDirectory(prefix="studio-render-check-") as directory:
            clip=VideoClip(lambda t: np.zeros((90,160,3),dtype=np.uint8),duration=0.2)
            try: clip.write_videofile(str(Path(directory,"check.mp4")),fps=5,codec="libx264",audio=False,logger=None)
            finally: clip.close()
    except Exception as error: failures.append("FFmpeg/render: " + type(error).__name__)
print(json.dumps(failures))
'''
        try:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=self.root,
                                    capture_output=True, text=True, encoding="utf-8", timeout=45)
            failures = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else ["Python imports"]
            package_check = self._run_pip(["check"], timeout=30)
            if package_check.returncode:
                failures.append("Xung đột phụ thuộc Python")
            if failures:
                self.add("error", "Môi trường", "; ".join(failures),
                         "Chạy run.bat -m pip install -r requirements.txt rồi kiểm tra lại.")
                return False
            self.add("ok", "Môi trường", "Thư viện tải được, phụ thuộc hợp lệ và FFmpeg render thử thành công.")
            return True
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            self.add("error", "Môi trường", "Không xác minh được thư viện/FFmpeg.",
                     "Chạy run.bat -m pip check để kiểm tra môi trường.")
            return False

    def check_gemini(self, key, selected_model):
        if missing_key(key):
            self.add("warning", "Gemini", "Chưa có GEMINI_API_KEY hợp lệ.",
                     "Mở Settings, nhập key từ https://aistudio.google.com/apikey và lưu.")
            return
        if not isinstance(selected_model, str) or not re.fullmatch(r"(?:models/)?gemini-[\w.-]+", selected_model):
            self.add("warning", "Gemini model", "ID model không hợp lệ.", "Chọn lại model trong Settings → AI Model.")
            return
        status, data, _ = http_json(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            {"x-goog-api-key": key},
        )
        if status == 200:
            names = {item.get("name", "").removeprefix("models/") for item in data.get("models", [])
                     if "generateContent" in item.get("supportedGenerationMethods", [])}
            if str(selected_model).removeprefix("models/") not in names:
                self.add("warning", "Gemini model", f"{selected_model} chưa có trong danh sách model của key này.",
                         "Mở Settings → AI Model → Tải lại model, chọn model khả dụng và lưu.")
            else:
                self.add("ok", "Gemini", f"Key được chấp nhận; model {selected_model} có trong danh sách.")
            self.add("info", "Gemini quota", "Kiểm tra này không tạo nội dung; hạn mức/billing chỉ xác minh khi gọi tạo nội dung.")
        else:
            self.account_error("Gemini", status, "Kiểm tra GEMINI_API_KEY và quyền dự án tại https://aistudio.google.com/apikey.")

    def account_error(self, name, status, action):
        if status is None or status >= 500:
            self.add("warning", name, "Mạng/dịch vụ chưa sẵn sàng; chưa xác minh được tài khoản.", "Thử lại khi có kết nối ổn định.")
        elif status == 429:
            self.add("warning", name, "Dịch vụ báo giới hạn yêu cầu/quota (429).", "Chờ rồi thử lại hoặc kiểm tra hạn mức trong tài khoản.")
        else:
            self.add("warning", name, f"Dịch vụ từ chối yêu cầu (HTTP {status}); cần kiểm tra key/quyền tài khoản.", action)

    def check_pexels(self, key):
        if missing_key(key):
            self.add("warning", "Pexels", "Chưa có PEXELS_API_KEY hợp lệ.",
                     "Mở Settings, nhập key từ https://www.pexels.com/api/ và lưu.")
            return
        status, _, headers = http_json("https://api.pexels.com/v1/search?query=nature&per_page=1", {"Authorization": key})
        if status == 200:
            self.add("ok", "Pexels", "Key được chấp nhận và API tìm ảnh hoạt động.")
            if any(name.lower() == "x-ratelimit-remaining" and value == "0" for name, value in headers.items()):
                self.add("warning", "Pexels quota", "Đã dùng hết hạn mức hiện tại.", "Chờ hạn mức được làm mới trong tài khoản Pexels.")
        else:
            self.account_error("Pexels", status, "Kiểm tra/cấp lại key tại https://www.pexels.com/api/ rồi lưu trong Settings.")

    def check_youtube(self, secret_path, token_path):
        secret_path, token_path = Path(secret_path), Path(token_path)
        try:
            secret = json.loads(secret_path.read_text(encoding="utf-8"))
            installed = secret.get("installed", {})
            if missing_key(installed.get("client_id")) or missing_key(installed.get("client_secret")):
                raise ValueError("Missing OAuth client")
            self.add("ok", "YouTube OAuth", "client_secret.json có thông tin ứng dụng desktop.")
        except (OSError, ValueError, AttributeError):
            self.add("warning", "YouTube OAuth", "Thiếu hoặc sai client_secret.json; cần file này để đăng nhập mới.",
                     "Tải OAuth Desktop client JSON từ Google Cloud. YouTube là tùy chọn, vẫn có thể tạo video.")
        if not token_path.exists():
            self.add("warning", "YouTube", "Chưa lưu đăng nhập YouTube.", "Đăng nhập/cấp quyền khi Upload lần đầu; vẫn có thể tạo video.")
            return
        try:
            from google.auth.exceptions import RefreshError, TransportError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            with token_path.open("rb") as source:
                credentials = CredentialReader(source).load()
            if not isinstance(credentials, Credentials):
                raise ValueError("Unsupported token")
            if credentials.refresh_token:
                credentials.refresh(functools.partial(Request(), timeout=10))
                with tempfile.NamedTemporaryFile(dir=token_path.parent, delete=False) as target:
                    temporary = Path(target.name)
                    pickle.dump(credentials, target)
                try:
                    temporary.replace(token_path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.add("ok", "YouTube", "Đã tự làm mới và lưu phiên đăng nhập.")
            elif credentials.valid:
                self.add("ok", "YouTube", "Token còn hạn trên máy; máy chủ sẽ xác minh quyền khi Upload.")
            else:
                self.add("warning", "YouTube", "Phiên đăng nhập đã hết hạn và không có refresh token.",
                         "Cần đăng nhập/cấp lại quyền YouTube khi Upload.")
        except RefreshError as error:
            if error.retryable:
                self.add("warning", "YouTube", "Dịch vụ xác thực tạm thời chưa sẵn sàng; chưa xác minh được phiên đăng nhập.",
                         "Thử lại sau; thông tin đăng nhập hiện tại được giữ lại.")
            else:
                self.add("warning", "YouTube", "Không làm mới được phiên đăng nhập; quyền có thể đã bị thu hồi.",
                         "Kiểm tra OAuth client. Đổi tên file token để giữ bản cũ, rồi Upload để đăng nhập/cấp lại quyền.")
        except TransportError:
            self.add("warning", "YouTube", "Chưa xác minh được phiên đăng nhập do lỗi kết nối.", "Kiểm tra mạng và thử lại.")
        except (OSError, ValueError, AttributeError, ImportError, pickle.UnpicklingError, EOFError):
            self.add("warning", "YouTube", "Không đọc/lưu được phiên đăng nhập hiện tại.",
                     "Kiểm tra file token và đăng nhập lại khi Upload; file gốc được giữ nếu đọc thất bại.")

    def check_tts(self, voice, rate, pitch):
        async def probe():
            import edge_tts
            audio_bytes = 0
            async for event in edge_tts.Communicate("Kiểm tra giọng đọc.", voice, rate=rate, pitch=pitch,
                                                  connect_timeout=5, receive_timeout=10).stream():
                if event["type"] == "audio":
                    audio_bytes += len(event["data"])
            return audio_bytes
        try:
            if not asyncio.run(asyncio.wait_for(probe(), timeout=15)):
                raise ValueError("No audio")
            self.add("ok", "Edge TTS", f"{voice} tạo được âm thanh thử; không cần API key.")
        except Exception as error:
            status = getattr(error, "status", None)
            self.add("warning", "Edge TTS", f"Chưa tạo được âm thanh thử{f' (HTTP {status})' if status else ''}.",
                     "Kiểm tra mạng/dịch vụ và giọng đã chọn. Mở run.bat để kiểm tra cập nhật rồi thử Narration lại.")

    def save_report(self):
        output = self.root / "output"
        output.mkdir(exist_ok=True)
        report = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "findings": self.findings}
        (output / "startup_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def studio_running():
    status, data, _ = http_json("http://127.0.0.1:7070/api/status")
    return status == 200 and isinstance(data, dict) and "stages" in data and "running" in data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kiểm tra và cập nhật trước khi mở Studio.")
    parser.add_argument("--no-update", action="store_true", help="Chỉ kiểm tra thư viện, không cài phiên bản mới.")
    args = parser.parse_args(argv)
    checker = Checker()
    if sys.version_info[:2] != (3, 13) or Path(sys.prefix).resolve() != (ROOT / "venv").resolve():
        checker.add("error", "Python", "Phải dùng Python 3.13 trong venv của dự án.", "Mở bằng run.bat.")
        checker.save_report()
        return 1
    if not args.no_update and studio_running():
        checker.add("warning", "Studio", "Ứng dụng đang chạy; chưa thay đổi thư viện đang được sử dụng.",
                    "Đóng cửa sổ chạy Studio cũ bằng Ctrl+C rồi mở run.bat để cập nhật.")
        checker.save_report()
        return 2
    checker.add("ok", "Python", f"{sys.version.split()[0]} — {sys.executable}")
    checker.check_dependencies(auto_update=not args.no_update)
    if not checker.check_runtime():
        checker.save_report()
        return 1
    try:
        import config
        checker.check_gemini(config.GEMINI_API_KEY, config.GEMINI_MODEL)
        checker.check_pexels(config.PEXELS_API_KEY)
        checker.check_youtube(config.YOUTUBE_CLIENT_SECRET, config.YOUTUBE_TOKEN_FILE)
        checker.check_tts(config.VOICE_ID, config.VOICE_RATE, config.VOICE_PITCH)
    except Exception as error:
        checker.add("warning", "Cấu hình", f"Chưa hoàn tất kiểm tra cấu hình ({type(error).__name__}).",
                    "Kiểm tra Settings/config.py; không có key nào được ghi vào báo cáo.")
    checker.save_report()
    warnings = sum(item["level"] == "warning" for item in checker.findings)
    print(f"\nHoàn tất kiểm tra: {warnings} thông báo cần xem. Chi tiết: output/startup_report.json\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
