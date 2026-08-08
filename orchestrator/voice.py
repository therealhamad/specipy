"""Spoken briefing via Maya Research's hosted TTS.

Verified against the live endpoint on 2026-08-08:

    POST https://tts.mayaresearch.ai/tts
    Authorization: Bearer <key>
    {"text": ..., "voice": "Ananya", "language": "en", "model": "Maya 2 Native"}
    -> 200, Content-Type: audio/L16; rate=24000; channels=1

Two things that surprise you if you assume an OpenAI-shaped TTS API:

  * `MAYA_API_URL` is a base URL. The synthesis path is `/tts`; the root
    returns a health payload, so posting to the root looks like a success and
    yields no audio.
  * the response is **raw 16-bit PCM**, not MP3 or WAV. A browser `<audio>`
    element cannot play it, so it is wrapped in a WAV container here before
    being written to disk.

Model names come from the endpoint itself ("Maya 2 Native", "Maya 2 Native
Emotional", "Maya 2 Global"). "veena" is the underlying model family, not an
accepted value for the `model` field.

Precedence: live synthesis -> cached clip -> no audio (the UI shows the text).
"""

from __future__ import annotations

import base64
import io
import os
import re
import wave
from dataclasses import dataclass

import httpx

from orchestrator import config

SYNTHESIS_PATH = "/tts"
FALLBACK_STEM = "fallback"
FALLBACK_EXTENSIONS = (".wav", ".mp3")


@dataclass
class VoiceResult:
    url: str | None
    source: str  # "maya" | "cached" | "none"
    detail: str = ""


def synthesis_url() -> str:
    """The synthesis endpoint, whether MAYA_API_URL is a base or a full path."""
    base = config.MAYA_API_URL.rstrip("/")
    if not base:
        return ""
    if base.endswith(SYNTHESIS_PATH) or "/tts" in base.rsplit("/", 1)[-1]:
        return base
    return base + SYNTHESIS_PATH


def _cached() -> VoiceResult:
    for extension in FALLBACK_EXTENSIONS:
        path = os.path.join(config.AUDIO_DIR, FALLBACK_STEM + extension)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return VoiceResult(
                url=f"/static/audio/{FALLBACK_STEM}{extension}",
                source="cached",
                detail="Served the pre-generated briefing.",
            )
    return VoiceResult(
        url=None,
        source="none",
        detail=(
            "No audio available. Check MAYA_API_URL/MAYA_API_KEY, or save a clip with "
            "`make voice-check`."
        ),
    )


def payload(text: str) -> dict[str, object]:
    body: dict[str, object] = {"text": text, "language": config.MAYA_LANGUAGE}
    if config.MAYA_MODEL:
        body["model"] = config.MAYA_MODEL
    if config.MAYA_VOICE:
        body["voice"] = config.MAYA_VOICE
    return body


def _pcm_to_wav(raw: bytes, rate: int, channels: int, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a RIFF/WAVE container so browsers can play it."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(rate)
        handle.writeframes(raw)
    return buffer.getvalue()


def audio_payload(response: httpx.Response) -> tuple[bytes, str] | None:
    """Extract (bytes, file extension) from a synthesis response.

    Handles raw PCM (which needs containerising), already-containerised audio,
    and JSON wrappers carrying base64 or a URL.
    """
    content_type = (response.headers.get("content-type") or "").lower()

    if content_type.startswith("audio/l16") or "pcm" in content_type:
        rate_match = re.search(r"rate=(\d+)", content_type)
        channels_match = re.search(r"channels=(\d+)", content_type)
        rate = int(rate_match.group(1)) if rate_match else 24000
        channels = int(channels_match.group(1)) if channels_match else 1
        return _pcm_to_wav(response.content, rate, channels), ".wav"

    if content_type.startswith("audio/wav") or content_type.startswith("audio/x-wav"):
        return response.content, ".wav"
    if content_type.startswith("audio/mpeg") or content_type.startswith("audio/mp3"):
        return response.content, ".mp3"
    if content_type.startswith("audio/ogg"):
        return response.content, ".ogg"
    if content_type.startswith("audio/") or content_type == "application/octet-stream":
        return response.content, ".wav"

    try:
        data = response.json()
    except ValueError:
        return (response.content, ".wav") if response.content else None

    if not isinstance(data, dict):
        return None
    for key in ("audio_base64", "audio_content", "audio", "data"):
        value = data.get(key)
        if isinstance(value, str) and len(value) > 128:
            try:
                return base64.b64decode(value, validate=False), ".wav"
            except Exception:
                continue
    for key in ("url", "audio_url", "output_url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            try:
                fetched = httpx.get(value, timeout=30.0)
                fetched.raise_for_status()
                return fetched.content, os.path.splitext(value)[1] or ".mp3"
            except Exception:
                return None
    return None


def synthesize(text: str, run_id: str, timeout: float = 90.0) -> VoiceResult:
    url = synthesis_url()
    if not (url and config.MAYA_API_KEY):
        result = _cached()
        if result.source == "cached":
            result.detail = "Maya not configured; served the pre-generated briefing."
        return result

    try:
        response = httpx.post(
            url,
            json=payload(text),
            headers={
                "Authorization": f"Bearer {config.MAYA_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        extracted = audio_payload(response)
    except Exception as exc:
        result = _cached()
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError):
            detail += f" — {exc.response.text[:200]}"
        result.detail = f"Maya request failed ({detail}). {result.detail}"
        return result

    if not extracted:
        result = _cached()
        result.detail = (
            f"Maya returned no usable audio (content-type "
            f"{response.headers.get('content-type')!r}). {result.detail}"
        )
        return result

    audio, extension = extracted
    os.makedirs(config.AUDIO_DIR, exist_ok=True)
    filename = f"{run_id}{extension}"
    with open(os.path.join(config.AUDIO_DIR, filename), "wb") as fh:
        fh.write(audio)
    return VoiceResult(
        url=f"/static/audio/{filename}",
        source="maya",
        detail=f"Synthesised {len(audio)} bytes via Maya ({config.MAYA_MODEL}).",
    )
