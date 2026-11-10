"""Topic-matched Pexels photos and silent B-roll, with preview review and provenance."""
import hashlib
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image
from google.genai import types

import config
from agents.gemini_client import generate
from agents.researcher import parse_json_object

PEXELS_SEARCH = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_SEARCH = "https://api.pexels.com/v1/videos/search"


def _media_fingerprint(script: dict) -> str:
    content = {"topic": script.get("topic"), "sections": [
        {key: value for key, value in section.items()
         if key in ("id", "narration", "visual_plan") or key.startswith("image_query")}
        for section in script.get("sections", [])]}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _search(query: str, kind: str, orientation: str) -> list:
    if not query.strip():
        return []
    try:
        response = requests.get(PEXELS_SEARCH if kind == "image" else PEXELS_VIDEO_SEARCH,
                                headers={"Authorization": config.PEXELS_API_KEY},
                                params={"query": query.strip(), "per_page": 8, "page": 1,
                                        "orientation": orientation, "locale": "en-US"}, timeout=20)
        if response.status_code in (401, 403):
            raise ValueError("Pexels từ chối tài khoản. Kiểm tra PEXELS_API_KEY trong Settings.")
        if response.status_code == 429:
            print("   ⚠ Pexels đã chạm hạn mức; chưa có tư liệu cho truy vấn này.")
            return []
        response.raise_for_status()
        return response.json().get("photos" if kind == "image" else "videos", [])
    except (requests.RequestException, json.JSONDecodeError):
        print(f"   ⚠ Không tìm được tư liệu cho '{query}'; không thay bằng chủ đề khác.")
        return []


def _download_bytes(url: str, maximum: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".pexels.com"):
        raise ValueError("URL tư liệu không thuộc CDN Pexels.")
    response = requests.get(url, timeout=40, stream=True, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("CDN không trả về tệp tư liệu.")
    try:
        if int(response.headers.get("Content-Length", 0)) > maximum:
            raise ValueError("Tư liệu vượt giới hạn tải.")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > maximum:
                raise ValueError("Tư liệu vượt giới hạn tải.")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def _preview(url: str) -> bytes:
    data = _download_bytes(url, 5 * 1024 * 1024)
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((480, 480))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80)
        return output.getvalue()


def _choose_video_file(files: list, orientation: str) -> dict | None:
    candidates = []
    for file in files:
        width, height = file.get("width") or 0, file.get("height") or 0
        if file.get("file_type") != "video/mp4" or not file.get("link") or not width or not height:
            continue
        if max(width, height) > 1920 or min(width, height) < 360:
            continue
        if (height > width) != (orientation == "portrait"):
            continue
        candidates.append(file)
    # Prefer HD without downloading a 4K file just to crop it down.
    return min(candidates, key=lambda file: abs(max(file["width"], file["height"]) - 1280), default=None)


def _photo_rank(photo: dict, query: str) -> tuple:
    words = set(re.findall(r"[a-z0-9]+", query.casefold())) - {"a", "the", "of", "in", "and", "with"}
    description = set(re.findall(r"[a-z0-9]+", (photo.get("alt", "") + " " + photo.get("url", "")).casefold()))
    return (len(words & description), min(photo.get("width", 0) * photo.get("height", 0), 4000000))


def _search_candidates(shot: dict, shot_index: int, orientation: str, seen: set) -> list:
    query = shot["query"]
    photos = sorted(_search(query, "image", orientation), key=lambda photo: _photo_rank(photo, query), reverse=True)
    videos = _search(shot.get("video_query") or query, "video", orientation)
    candidates = []
    # Two photo alternatives and one moving shot per planned beat; one review per section.
    for kind, results, limit in (("video", videos, 1), ("image", photos, 2)):
        added = 0
        for result in results:
            identity = f"{kind}:{result.get('id')}"
            if identity in seen:
                continue
            file = _choose_video_file(result.get("video_files", []), orientation) if kind == "video" else None
            if kind == "video" and (not file or not 2 <= (result.get("duration") or 0) <= 120):
                continue
            preview_url = result.get("image") if kind == "video" else result.get("src", {}).get("medium")
            download_url = file["link"] if file else (result.get("src", {}).get("large2x") or result.get("src", {}).get("original"))
            if not preview_url or not download_url:
                continue
            try:
                preview = _preview(preview_url)
            except (requests.RequestException, ValueError, OSError):
                continue
            candidates.append({"candidate_id": f"s{shot_index}_{kind}_{result['id']}", "identity": identity,
                               "shot": shot_index, "kind": kind, "query": query, "subject": shot["subject"],
                               "alt": result.get("alt", ""), "source_url": result.get("url", ""),
                               "creator": (result.get("user", {}).get("name", "") if kind == "video" else result.get("photographer", "")),
                               "download_url": download_url, "preview_bytes": preview,
                               "preview_url": preview_url, "duration": result.get("duration")})
            added += 1
            if added >= limit:
                break
    return candidates


def _review_candidates(topic: str, section: dict, candidates: list) -> list:
    if not candidates:
        return []
    prompt = f"""Select accurate visual illustrations for a Vietnamese video.
Exact topic: {topic}
Section narration: {section.get('narration', '')}
Required shots in narration order:
{json.dumps(section.get('visual_plan', []), ensure_ascii=False)}
You will see a labelled thumbnail for each candidate. Judge what is VISIBLE, not just the query.
For each shot choose at most ONE candidate from that same shot. Prefer video if equally suitable.
Reject images unrelated to the sentence, different species/people/places, generic neon/space/labs,
misleading depictions of mechanisms and unrelated stock metaphors. For a named event/person,
a thumbnail alone does not prove identity: reject unverifiable documentary claims.
An illustrative real-life example is acceptable ONLY if it is the example actually used in narration.
Choose NONE if nothing fits; filling every slot is not a goal. A video thumbnail only proves its
visible subject, not the accuracy of the full video or a historical event.
Describe only what is visible. A still photograph cannot prove a process, direction of change,
chemical reaction or cause: mark such a supporting photo illustrative, not documentary evidence.
Return ONLY JSON: {{"selections": [{{"shot": 0, "candidate_id": "provided ID",
"match": "direct or illustrative", "reason": "specific Vietnamese explanation of the visible match"}}],
"missing_shots": [0]}}. Never invent candidate IDs."""
    contents = [prompt]
    for candidate in candidates:
        contents.append(json.dumps({key: candidate.get(key) for key in ("candidate_id", "shot", "kind", "subject", "alt")}, ensure_ascii=False))
        contents.append(types.Part.from_bytes(data=candidate["preview_bytes"], mime_type="image/jpeg"))
    review = parse_json_object(generate(contents, temperature=0.1))
    if not isinstance(review.get("selections"), list):
        raise ValueError("Không có kết quả kiểm tra tư liệu hợp lệ.")
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    selected, used_shots, used_ids = [], set(), set()
    for selection in review["selections"]:
        if not isinstance(selection, dict):
            continue
        candidate = by_id.get(selection.get("candidate_id"))
        if (not candidate or selection.get("shot") != candidate["shot"] or candidate["shot"] in used_shots
                or candidate.get("identity") in used_ids or selection.get("match") not in ("direct", "illustrative")
                or not isinstance(selection.get("reason"), str) or not selection["reason"].strip()):
            continue
        selected.append({**candidate, "match": selection["match"], "reason": selection["reason"]})
        used_shots.add(candidate["shot"])
        used_ids.add(candidate.get("identity"))
    return sorted(selected, key=lambda candidate: candidate["shot"])


def _fetch_images(query: str, section_index: int, images_dir: str,
                  count: int = 1, orientation: str = "landscape",
                  img_num_start: int = 0, topic_hint: str = "", seen_photos: set = None) -> list:
    """Legacy photo helper: deterministic ranking, exact query, no unrelated fallback."""
    saved = []
    photos = sorted(_search(query, "image", orientation), key=lambda photo: _photo_rank(photo, query), reverse=True)
    for photo in photos:
        if seen_photos is not None and photo.get("id") in seen_photos:
            continue
        url = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("original")
        if not url:
            continue
        try:
            data = _download_bytes(url, 20 * 1024 * 1024)
            path = Path(images_dir, f"section_{section_index:02d}_img{img_num_start + len(saved) + 1:02d}.jpg")
            path.write_bytes(data)
            saved.append(str(path))
            if seen_photos is not None:
                seen_photos.add(photo.get("id"))
        except (requests.RequestException, OSError, ValueError):
            continue
        if len(saved) >= count:
            break
    return saved


def _section_shots(section: dict, is_shorts: bool) -> list:
    if section.get("visual_plan"):
        return section["visual_plan"][:4]
    # Old, hand-edited scripts can still be rendered; their candidates are also visually reviewed.
    shots = []
    for key in ("image_query", "image_query_2", "image_query_3", "image_query_4"):
        query = section.get(key, "").strip()
        if query and query not in [shot["query"] for shot in shots]:
            shots.append({"query": query, "video_query": query, "subject": query,
                          "narration_excerpt": section.get("narration", ""), "purpose": "Minh họa lời kể."})
    return shots[:4 if is_shorts else 3]


def download_images(script: dict, output_dir: str) -> dict:
    """Return {section_id: [photo/video paths]}, retaining the existing pipeline API."""
    root = Path(output_dir)
    fingerprint = _media_fingerprint(script)
    media_dir = root / "images" / f"media-{fingerprint[:12]}"
    media_dir.mkdir(parents=True, exist_ok=True)
    orientation = "portrait" if script.get("video_type") == "shorts" else "landscape"
    image_map, assets, coverage, seen = {}, [], [], set()
    for section in script.get("sections", []):
        sid = section["id"]
        shots = _section_shots(section, orientation == "portrait")
        candidates = []
        for index, shot in enumerate(shots):
            if shot.get("stock_ok", True):
                candidates.extend(_search_candidates(shot, index, orientation, seen))
        print(f"   → [{sid}] Kiểm tra độ phù hợp của {len(candidates)} ảnh/clip xem trước…")
        try:
            selected = _review_candidates(script.get("topic") or script.get("title", ""),
                                          {**section, "visual_plan": shots}, candidates)
        except Exception as error:
            print(f"   ⚠ Chưa xác minh được tư liệu đoạn {sid} ({type(error).__name__}); không chọn bừa.")
            selected = []
        paths, found_shots = [None] * len(shots), set()
        for candidate in selected:
            suffix = ".mp4" if candidate["kind"] == "video" else ".jpg"
            path = media_dir / f"section_{sid:02d}_{candidate['candidate_id']}{suffix}"
            try:
                data = _download_bytes(candidate["download_url"], 60 * 1024 * 1024 if suffix == ".mp4" else 20 * 1024 * 1024)
                temporary = path.with_suffix(suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(path)
                if suffix == ".mp4":
                    path.with_suffix(".poster.jpg").write_bytes(candidate["preview_bytes"])
                paths[candidate["shot"]] = str(path.resolve())
                found_shots.add(candidate["shot"])
                seen.add(candidate["identity"])
                assets.append({key: candidate[key] for key in ("kind", "query", "subject", "source_url", "creator", "match", "reason", "shot")}
                              | {"section_id": sid, "path": path.relative_to(root).as_posix()})
            except (requests.RequestException, OSError, ValueError):
                print(f"   ⚠ Không tải được tư liệu đã chọn cho đoạn {sid}.")
        image_map[sid] = paths
        missing = [index for index in range(len(shots)) if index not in found_shots]
        coverage.append({"section_id": sid, "planned": len(shots), "selected": len(found_shots), "missing_shots": missing})
        if missing or not shots:
            print(f"   ⚠ Đoạn {sid}: thiếu tư liệu phù hợp cho {len(missing) if shots else 1} cảnh; dùng chữ/phụ đề khi cần.")
        print(f"   → [{sid}] Đã chọn {len(found_shots)} tư liệu đúng nội dung.")
    manifest = {"version": 1, "script_hash": fingerprint, "assets": assets, "coverage": coverage,
                "provider": "Pexels", "provider_url": "https://www.pexels.com",
                "review_method": "Gemini preview review; video reviewed by thumbnail, not full footage"}
    temp = root / "media_manifest.json.tmp"
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(root / "media_manifest.json")
    return image_map


def load_saved_images(script: dict, output_dir: str) -> dict:
    root = Path(output_dir).resolve()
    manifest_path = root / "media_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("script_hash") != _media_fingerprint(script):
            raise ValueError("Tư liệu thuộc kịch bản/chủ đề khác. Chạy lại bước Images / Tư liệu ảnh & video.")
        counts = {item["section_id"]: item["planned"] for item in manifest.get("coverage", [])}
        result = {section["id"]: [None] * counts.get(section["id"], 0) for section in script.get("sections", [])}
        for asset in manifest.get("assets", []):
            path = (root / asset["path"]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Tư liệu đã đổi hoặc bị thiếu. Chạy lại bước Images / Tư liệu ảnh & video.")
            if asset["section_id"] in result:
                slots = result[asset["section_id"]]
                if not 0 <= asset["shot"] < len(slots):
                    raise ValueError("Mốc tư liệu không hợp lệ. Chạy lại Images / Tư liệu ảnh & video.")
                slots[asset["shot"]] = str(path)
        return result
    folder = root / "images"
    image_map = {}
    for section in script.get("sections", []):
        sid = section["id"]
        paths = sorted(folder.glob(f"section_{sid:02d}_img*.jpg"))
        legacy = folder / f"section_{sid:02d}.jpg"
        if not paths and legacy.exists():
            paths = [legacy]
        image_map[sid] = [str(path) for path in paths]
    if any(image_map.values()):
        print("   ⚠ Đang dùng ảnh cũ chưa có bản kiểm tra chủ đề; chạy lại Images để chọn tư liệu sát lời kể.")
    return image_map
