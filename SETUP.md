# 🛠️ Setup Guide

Complete step-by-step instructions to get YouTube AI Agent Studio running from scratch.

> **Time required:** ~10 minutes

---

## Prerequisites

- Python **3.10 or newer** — [python.org/downloads](https://www.python.org/downloads/)
- `ffmpeg` installed and on your PATH (see [Step 1b](#1b-install-ffmpeg))
- A Google account (for Gemini + YouTube)
- A Pexels account (free image API)

---

## Step 1 — Clone and install

### 1a. Clone the repository

```bash
git clone https://github.com/yourusername/youtube-ai-agent.git
cd youtube-ai-agent
```

### 1b. Install ffmpeg

ffmpeg is required by MoviePy for video rendering.

| OS | Command |
|---|---|
| **Windows** | Download from [ffmpeg.org](https://ffmpeg.org/download.html) → extract → add `bin/` folder to PATH |
| **Mac** | `brew install ffmpeg` |
| **Ubuntu/Debian** | `sudo apt install ffmpeg` |

Verify: `ffmpeg -version` should print version info.

### 1c. Install Python dependencies

On Windows, select Python 3.13 explicitly and install into the project environment:

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `venv` already uses Python 3.13 and the app works, keep that environment.
Do not recreate it just to launch the app. `requirements.txt` keeps MoviePy 1.0.3
and selects NumPy 2.1+ for new installs on Python 3.13.

On macOS/Linux, create the environment with `python3.13 -m venv venv`,
then install with `venv/bin/python -m pip install -r requirements.txt`.

---

## Step 2 — Get your API keys

### 2a. Google Gemini API Key (required — free)

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with your Google account
3. Click **Create API Key**
4. Copy the key

### 2b. Pexels API Key (required — free)

1. Go to [pexels.com/api](https://www.pexels.com/api/)
2. Create a free account and click **Your API Key**
3. Copy the key

---

## Step 3 — Configure your keys

### Option A — Environment variables (recommended)

```bash
# Mac / Linux
export GEMINI_API_KEY="your_key_here"
export PEXELS_API_KEY="your_key_here"

# Windows (Command Prompt)
set GEMINI_API_KEY=your_key_here
set PEXELS_API_KEY=your_key_here

# Windows (PowerShell)
$env:GEMINI_API_KEY="your_key_here"
$env:PEXELS_API_KEY="your_key_here"
```

To make these permanent, add them to your shell profile (`~/.bashrc`, `~/.zshrc`) or use a `.env` file:

```bash
cp .env.example .env
# Now edit .env and fill in your keys
```

### Option B — Edit config.py directly

Open `config.py` and replace the placeholder values:

```python
GEMINI_API_KEY = "AIza..."     # your Gemini key
PEXELS_API_KEY = "abc123..."   # your Pexels key
```

---

## Step 4 — Set up YouTube upload (optional)

Skip this step if you just want to generate videos locally without uploading.

### 4a. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **Select a project → New Project**
3. Name it (e.g. "YouTube AI Agent") and click **Create**

### 4b. Enable the YouTube Data API

1. In your new project, go to **APIs & Services → Library**
2. Search for **YouTube Data API v3**
3. Click **Enable**

### 4c. Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. If prompted, configure the consent screen first:
   - User Type: **External**
   - Fill in app name, support email, and developer email
   - Scopes: click **Add or remove scopes** → search "youtube" → select `youtube.upload`
   - Test users: add your own Gmail address
4. Back in Credentials → **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: anything (e.g. "YT Agent Desktop")
5. Click **Download JSON**
6. Rename the downloaded file to `client_secret.json`
7. Place it in the root of the project (replacing the placeholder)

### 4d. First-time authentication

On your first run, a browser window will open asking you to sign in with Google and grant permission. This only happens once — the token is saved locally as `youtube_token.pickle`.

> If you see a "Google hasn't verified this app" warning, click **Advanced → Go to (app name) (unsafe)**. This is expected for personal OAuth apps in testing mode.

---

## Step 5 — Customise your channel

Open `config.py` and edit:

```python
CHANNEL_DESCRIPTION = """
Describe your channel clearly here.
The more specific, the better the AI's topic and script quality.
Example: "Educational science channel for curious adults, style of Veritasium."
"""
CHANNEL_NAME = "Your Channel Name"
```

---

## Step 6 — Launch

### GUI mode (recommended)

```powershell
.\run.bat
```

On Windows, double-click `run.bat` or run the command above. It always uses
`venv\Scripts\python.exe`, verifies Python 3.13, and enables UTF-8 output.
It stops with an explanation if the environment uses a different Python.
Avoid bare `python`, `py` or `pip`: their defaults may select another version.

Opens at **http://localhost:7070** — configure everything visually and click Generate.

Before opening the GUI (or `pipeline.py`), `run.bat` now:

- Checks the project Python 3.13 environment, creating it if missing.
- Resolves dependency updates against `requirements.txt`, then automatically installs
  compatible stable versions, including their dependencies. MoviePy remains 1.0.3;
  other packages stay within the declared version limits. Newer incompatible versions
  are reported without installing them. A version snapshot is saved before changes.
- Checks imports, package conflicts, and a tiny FFmpeg render after updating.
- Verifies the configured Gemini model/key and Pexels key with small API requests.
  The Gemini check lists models; it does not spend generation tokens or verify
  generation quota/billing. The Pexels check uses one search request.
- Refreshes saved YouTube login when a refresh token is available. Missing/rejected
  credentials show instructions; the checker never creates accounts, supplies new keys,
  opens an OAuth consent flow, or removes your saved token on refresh failure.
- Synthesizes a short Edge TTS sample without replacing your narration files.

Results appear in the terminal and the **Kiểm tra khởi động** panel in the GUI.
`output/startup_report.json` stores the latest report without keys or tokens;
`output/dependencies_before_update.txt` stores package versions before the last update.
After changing a key, reopen the studio to run the checks again. API access failures
are warnings so you can open Settings; broken Python dependencies stop startup.
When offline, checks report that availability could not be verified rather than
assuming a key has expired. If Studio is already running, updates wait until you
close the old instance and launch again.

```powershell
# Check and apply compatible dependency updates, then exit without starting the GUI
.\run.bat --check

# Inspect available updates and services without installing library updates
.\run.bat --check-only
```

Commands such as `run.bat --version`, `run.bat -m pip ...` and tests bypass automatic
startup checks so maintenance commands do not trigger another update run.

In **Settings → AI Model**, choose a Gemini text model and save settings.
Use **Tải lại model** to refresh the list from Google with your saved API key.
Model access, quotas and charges depend on your Google API project.

For macOS/Linux, launch with `venv/bin/python gui.py`.

### CLI mode

```powershell
.\run.bat pipeline.py
```

Check the interpreter with `.\run.bat --version`. Install packages with
`.\run.bat -m pip install -r requirements.txt` so pip uses the same Python.

VS Code settings point to the project environment whether you open `Youtube_AI`
or the repository folder. If VS Code remembers an older interpreter, run
**Python: Select Interpreter** once and select `venv\Scripts\python.exe`,
then open a new terminal.

Runs the full pipeline in the terminal. The review dashboard opens automatically in your browser when the video is ready.

---

## Step 7 — Add background music (optional)

### Natural Vietnamese narration and synchronized pictures

The free Edge TTS workflow now explicitly requests word timing. With
**Settings → Nhịp đọc tự nhiên** enabled, the app shortens only long silent gaps,
keeps short breathing pauses, protects words that have timing metadata, and
normalizes speech loudness. The default long-pause target is **450 ms**; use a
higher value for a calmer delivery. Speed and pitch remain at your selected values.

After changing these options, run **Narration** again, then **Music** (if enabled)
and **Render Video**. You can skip Research, Script and Images to reuse them.
Generate a new Script as well to use the improved Vietnamese storytelling prompts
and subject-specific English image queries.

`narration_timing.json` records the spoken section boundaries and fingerprints of
the script/audio. Both normal videos and Shorts use those boundaries to schedule
pictures. Visual crossfades no longer shorten the narration. Captions are adjusted
after pause processing, and changed scripts cannot silently reuse old narration.
Older outputs without timing metadata use an estimate based on actual audio length;
run Narration again for measured timing.

Rendering preserves the entire audio, uses AAC at 192 kb/s and H.264 CRF 20, and
prepares MP4s for fast playback. Existing render-speed preferences are preserved.
The image cache supports both current multi-image names and older single-image names.

If Microsoft returns no timing metadata, the app explicitly reports approximate
caption alignment. If audio post-processing fails, it preserves the original speech
and reports the problem rather than returning an empty audio file.

Drop any `.mp3` or `.wav` files into the `music library/` folder.  
The pipeline will automatically shuffle and loop them to match the video duration.

The repo includes a few dark cinematic tracks to get you started. Replace or add to them freely.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named 'moviepy'` | Run `pip install -r requirements.txt` again |
| `ffmpeg not found` | Install ffmpeg and ensure it's on your PATH |
| `RESOURCE_EXHAUSTED` / 429 | Gemini quota hit — wait until midnight Pacific or switch starting model |
| Edge TTS WebSocket `403 Invalid response status` | Run `.\run.bat -m pip install --upgrade "edge-tts>=7.3.1,<8"`, then close and reopen the studio. Older clients use an authentication format Microsoft no longer accepts. Retry Narration using the saved research/script/images. |
| `youtube_token.pickle` auth error | Delete the file and re-run to re-authenticate |
| Tiny/ugly video text | Install DejaVu fonts: `sudo apt install fonts-dejavu` (Linux) |
| Flask port already in use | Change `REVIEW_PORT` in `config.py` |
| `client_secret.json` error | Ensure you replaced the placeholder with your real Google credentials |
| Captions out of sync | Switch to a different `VOICE_ID` in `config.py` (see voice list in file) |

---

## Optional: Run as a background service

To keep the studio running continuously (e.g. on a home server):

```bash
# Using nohup (Linux/Mac)
nohup python gui.py > studio.log 2>&1 &

# Using screen
screen -S yt-studio
python gui.py
# Detach: Ctrl+A then D
```

---

If you get stuck, [open an issue](https://github.com/yourusername/youtube-ai-agent/issues) with your error message and OS.


## Nội dung bám chủ đề và tư liệu phù hợp

Trong **I Pick → Exact Topic**, nhập câu hỏi hoặc chủ đề cụ thể. Chạy lại **Research → Script → Ảnh & video → Narration → Music → Render** khi đổi chủ đề.

- **Research** chuẩn bị câu trả lời chính, phạm vi, dàn ý có ví dụ và cầu nối giữa các ý. Chủ đề nhập tay được giữ nguyên; không bị đổi theo phong cách kênh hay nhóm chủ đề ngẫu nhiên.
- **Script** viết lời kể từ bản chuẩn bị, rồi thực hiện một lượt biên tập độc lập về bám chủ đề, mạch kể, lặp ý, câu trả lời cho hook và yêu cầu hình ảnh. Nếu chưa đạt, tự sửa một lần; còn lỗi thì thông báo để chạy lại, không tiếp tục xuất bản nháp lỗi.
- Mỗi đoạn có `visual_plan`: đối tượng, câu lời kể cần minh họa, mục đích, truy vấn ảnh và video bằng tiếng Anh. Không ép mọi chủ đề vào hình ảnh vũ trụ, phòng thí nghiệm hoặc gương mặt ngạc nhiên.
- **Ảnh & video** dùng cùng Pexels API key, tìm cả ảnh lẫn clip MP4. Kết quả được xếp theo độ phù hợp rồi Gemini xem ảnh/khung xem trước để chọn. Không chọn ngẫu nhiên, không thay bằng một chủ đề khác khi thiếu kết quả. Cảnh thiếu tư liệu giữ vị trí của nó và dùng nền chữ/phụ đề.
- Clip tư liệu được bỏ tiếng gốc, giữ giọng đọc và nhạc của video. Clip ngắn được lặp bằng chuyển mờ để tránh đứng hình/giật về đầu. Hình ảnh đi theo thứ tự lời kể; khi có mốc từ từ Narration, cảnh đổi theo câu được trích trong `visual_plan`.
- **Review** có bản chuẩn bị nội dung, toàn bộ lời kể, yêu cầu từng cảnh và nguồn Pexels đã chọn. `media_manifest.json` lưu nguồn, tác giả, lý do chọn, cảnh thiếu và dấu nhận diện kịch bản; đổi kịch bản thì phải chạy lại bước tư liệu.

Quy trình này gọi Gemini thêm cho biên tập và kiểm tra tư liệu, nên dùng nhiều lượt API hơn trước. Không cần thư viện hay key mới. Bản chuẩn bị hiện do AI tổng hợp, **chưa tra cứu/kiểm chứng nguồn web**; màn hình Review ghi rõ điều này. Việc chọn clip dựa trên khung xem trước, nên vẫn cần xem lại toàn bộ bản dựng trước khi đăng.

Tài liệu API tư liệu: [Pexels API](https://www.pexels.com/api/documentation/).
