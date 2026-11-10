"""Shared Gemini client with model discovery and a fallback chain.

Availability, billing and quotas depend on the API key's project.
"""
import re
import time
from google import genai
from google.genai import types
import config

# Offline defaults, ordered by verified free-tier availability and performance.
_ALL_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash",
]

def normalize_model(model: str) -> str:
    """Accept Gemini text model IDs, including newly released versions."""
    if not isinstance(model, str):
        raise ValueError("Model must be a Gemini model ID.")
    name = model.strip().removeprefix("models/")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
        raise ValueError("Invalid Gemini model ID.")
    if any(part in name.lower() for part in (
        "image", "tts", "audio", "live", "embedding", "transcribe",
        "robotics", "omni", "nano-banana", "computer-use",
        "gemini-2.5", "gemini-2.0", "gemini-1.5", "gemini-1.0",
    )):
        raise ValueError("Choose a Gemini text model for research and scripts.")
    return name


def build_chain(starting_model: str | None = None) -> list:
    """Keep the selected model first, even when it is newer than our defaults."""
    preferred = normalize_model(starting_model) if starting_model else None
    if preferred and preferred in _ALL_MODELS:
        idx = _ALL_MODELS.index(preferred)
        return _ALL_MODELS[idx:] + _ALL_MODELS[:idx]
    if preferred:
        return [preferred] + list(_ALL_MODELS)
    return list(_ALL_MODELS)

_client = None
_client_key = None
_models_cache = None
_models_cache_key = None
_models_cache_until = 0.0


def _get_client() -> genai.Client:
    global _client, _client_key
    if _client is None or _client_key != config.GEMINI_API_KEY:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
        _client_key = config.GEMINI_API_KEY
    return _client


def get_available_models(refresh: bool = False) -> dict:
    """Discover text models using the saved key; retain defaults when offline."""
    global _models_cache, _models_cache_key, _models_cache_until
    key = config.GEMINI_API_KEY
    if (not refresh and _models_cache is not None
            and _models_cache_key == key and time.monotonic() < _models_cache_until):
        return {**_models_cache, "models": list(_models_cache["models"])}

    try:
        available = set()
        models = _get_client().models.list(config=types.ListModelsConfig(
            http_options=types.HttpOptions(timeout=10000)
        ))
        for model in models:
            if "generateContent" not in (model.supported_actions or []):
                continue
            try:
                available.add(normalize_model(model.name))
            except ValueError:
                continue
        if not available:
            raise ValueError("No text models returned.")
        ordered = [name for name in _ALL_MODELS if name in available]
        ordered += sorted(available.difference(ordered), reverse=True)
        result = {"models": ordered, "source": "api"}
    except Exception:
        result = {
            "models": list(_ALL_MODELS), "source": "fallback",
            "warning": "Không tải được danh sách từ Google. Đang hiển thị danh sách dự phòng; "
                       "hãy kiểm tra API key, kết nối mạng rồi tải lại.",
        }

    _models_cache = result
    _models_cache_key = key
    _models_cache_until = time.monotonic() + (300 if result["source"] == "api" else 30)
    return {**result, "models": list(result["models"])}


def _should_switch_model(err: Exception) -> bool:
    """Return True for any error code that means we should try the next model."""
    msg = str(err)
    return any(k in msg for k in [
        "429", "RESOURCE_EXHAUSTED", "quota", "rate limit",  # 429 – quota
        "503", "UNAVAILABLE", "overloaded", "high demand",   # 503 – overloaded
        "500", "INTERNAL",                                    # 500 – transient
        "504", "DEADLINE_EXCEEDED", "deadline", "timeout",   # 504 – timeout / deadline
        "404", "NOT_FOUND", "not found",                     # 404 – model gone
    ])


def generate(prompt: str | list, starting_model: str | None = None, *, temperature: float = 0.9) -> str:
    client = _get_client()
    chain  = build_chain(starting_model or getattr(config, "GEMINI_MODEL", None))

    for model in chain:
        for attempt in range(3):   # up to 3 attempts per model before moving on
            try:
                print(f"   → Using {model}…")
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=temperature),
                )
                return response.text.strip()

            except Exception as e:
                if _should_switch_model(e):
                    if attempt < 2:
                        wait = 15 * (attempt + 1)   # 15s then 30s
                        print(f"   ⚠ {model} error (attempt {attempt+1}/3) — waiting {wait}s…")
                        time.sleep(wait)
                    else:
                        print(f"   ⚠ {model} failed 3 times — trying next model…")
                else:
                    raise   # auth error or bug — surface immediately

    raise RuntimeError(
        "\n\n❌  All Gemini models failed.\n"
        "    • 503/500 = temporary — wait a few minutes and retry\n"
        "    • 429 = quota — wait until midnight Pacific or add billing\n"
        "      https://aistudio.google.com/apikey\n"
    )
