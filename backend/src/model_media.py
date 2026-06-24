"""Native multimodal model OUTPUT — capability registry, request params,
response extraction, and storage for model-emitted images / audio / video.

This is the single import point for the chat/agent media pipeline:

  * `get_output_modalities()` / `audio_options()` — what a model can emit (for the
    model picker badges and composer toggles).
  * `media_request_params()` — extra OpenAI-compatible payload keys to request
    media output (image `modalities`, or audio `modalities`+`audio`).
  * `extract_media_from_openai_delta()` / `_message()` / `_anthropic_block()` plus
    `MediaStreamAccumulator` — pull media out of streaming/non-streaming responses.
  * `audio_oneshot_events()` — for audio output we do a single NON-streaming
    upstream request (avoids the pcm16/stream-only constraint) and synthesise the
    normal SSE protocol around it.
  * `store_model_media()` — write the binary to GENERATED_IMAGES_DIR, insert an
    owner'd GalleryImage row, return a small reference dict.

CRITICAL: binaries are written to files and served by /api/generated-image/{name};
only URLs/ids/transcripts ever go into message metadata — never base64.
"""
import asyncio
import base64
import io
import json
import logging
import re
import uuid
import wave
from pathlib import Path

import httpx

from src.constants import GENERATED_IMAGES_DIR

logger = logging.getLogger(__name__)

# ── Capability registry ───────────────────────────────────────────────────────
# Mirrors services/hwfit/image_models.py IMAGE_MODEL_REGISTRY shape. Matched by
# model-id substring (longest match wins) NOT provider, because _detect_provider
# has no "minimax" etc. — most media models reach us through OpenRouter / generic
# OpenAI-compatible proxies that all report as "openai"/"openrouter".
CHAT_MODEL_MEDIA_REGISTRY = [
    # ── Audio output (OpenAI gpt-4o-audio family) ──
    {"match": "gpt-4o-audio", "output_modalities": ["text", "audio"]},
    {"match": "gpt-4o-mini-audio", "output_modalities": ["text", "audio"]},
    {"match": "gpt-audio", "output_modalities": ["text", "audio"]},
    # ── Image output (reached via OpenRouter `modalities:["image","text"]`) ──
    {"match": "gemini-2.5-flash-image", "output_modalities": ["text", "image"]},
    {"match": "gemini-2.0-flash-exp", "output_modalities": ["text", "image"]},
    {"match": "gemini-2.0-flash-preview-image", "output_modalities": ["text", "image"]},
    {"match": "gemini-3-pro-image", "output_modalities": ["text", "image"]},
    {"match": "nano-banana", "output_modalities": ["text", "image"]},
    {"match": "flux", "output_modalities": ["text", "image"]},
    # ── Video output (ASYNC job-based; routed to the inline video branch in
    #    chat_stream). Match SPECIFIC video model ids only — never a bare vendor
    #    name like "minimax", which would wrongly capture the MiniMax-M3 *text*
    #    model and misroute every chat to video generation. ──
    {"match": "hailuo", "output_modalities": [], "video": {"async": True, "model": "MiniMax-Hailuo-02"}},
    {"match": "video-01", "output_modalities": [], "video": {"async": True}},
    {"match": "t2v-01", "output_modalities": [], "video": {"async": True}},
    {"match": "i2v-01", "output_modalities": [], "video": {"async": True}},
    {"match": "veo-", "output_modalities": [], "video": {"async": True}},
    {"match": "sora", "output_modalities": [], "video": {"async": True}},
]

# OpenAI audio voices / formats (defaults; a registry entry may narrow them).
AUDIO_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]
AUDIO_FORMATS = ["mp3", "wav", "opus", "flac", "aac", "pcm16"]
DEFAULT_AUDIO_VOICE = "alloy"
DEFAULT_AUDIO_FORMAT = "mp3"

_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/webp": "webp", "image/gif": "gif",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/wave": "wav", "audio/ogg": "ogg",
    "audio/opus": "opus", "audio/mp4": "m4a", "audio/aac": "aac", "audio/flac": "flac",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
}
_DEFAULT_MIME = {"image": "image/png", "audio": "audio/mpeg", "video": "video/mp4"}
_DEFAULT_EXT = {"image": "png", "audio": "mp3", "video": "mp4"}
_AUDIO_FORMAT_MIME = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg",
    "flac": "audio/flac", "aac": "audio/aac", "pcm16": "audio/pcm",
}


# ── Capability lookups ────────────────────────────────────────────────────────
def _best_registry_entry(model):
    m = (model or "").lower()
    best, best_len = None, -1
    for e in CHAT_MODEL_MEDIA_REGISTRY:
        match = (e.get("match") or "").lower()
        if match and match in m and len(match) > best_len:
            best, best_len = e, len(match)
    return best


def registry_output_modalities(model):
    """STRICT registry hit only (no heuristics). Used to gate request params so we
    never send `modalities` to a model that would 400 on it."""
    e = _best_registry_entry(model)
    return list(e.get("output_modalities") or []) if e else []


def _heuristic_modalities(model):
    m = (model or "").lower()
    mods = ["text"]
    if "audio" in m:  # gpt-4o-audio, *-audio-preview, audio-* …
        mods.append("audio")
    if ":image" in m or "image-preview" in m or "image-generation" in m or "-image" in m:
        mods.append("image")
    return mods


def video_capability(model):
    """Return the async-video marker dict for a model, or None."""
    return (_best_registry_entry(model) or {}).get("video")


# Models that generate media through a DEDICATED provider API (MiniMax
# image-01 / speech-02 / Hailuo), as opposed to chat models that emit media
# inline. Matched by model-id substring (longest match wins).
MEDIA_GEN_MODELS = [
    {"match": "image-01", "kind": "image"},
    {"match": "speech-02", "kind": "audio"},
    {"match": "speech-01", "kind": "audio"},
    {"match": "music-1", "kind": "music"},
    {"match": "music-01", "kind": "music"},
    {"match": "minimax-hailuo", "kind": "video"},
    {"match": "hailuo", "kind": "video"},
    {"match": "t2v-01", "kind": "video"},
    {"match": "i2v-01", "kind": "video"},
    {"match": "s2v-01", "kind": "video"},
]


def media_gen_kind(model):
    """For a dedicated media-generation model, return 'image'|'audio'|'video';
    else None. Drives the chat_stream media-generation branch."""
    m = (model or "").lower()
    best, best_len = None, -1
    for e in MEDIA_GEN_MODELS:
        if e["match"] in m and len(e["match"]) > best_len:
            best, best_len = e["kind"], len(e["match"])
    return best


def get_output_modalities(provider, model, endpoint=None):
    """Lenient capability list for UI badges + extraction enablement. Precedence:
    per-endpoint override → registry → heuristics, plus the async-video marker."""
    if endpoint is not None:
        raw = getattr(endpoint, "output_modalities", None)
        if raw:
            try:
                v = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(v, list) and v:
                    return v
            except Exception:
                pass
    mods = registry_output_modalities(model) or _heuristic_modalities(model)
    if video_capability(model) and "video" not in mods:
        mods = mods + ["video"]
    k = media_gen_kind(model)  # dedicated-API media models (image-01, speech-02, …)
    if k and k not in mods:
        mods = mods + [k]
    return mods


def audio_options(model):
    """Voices/formats to offer for an audio-capable model (registry may narrow)."""
    a = (_best_registry_entry(model) or {}).get("audio") or {}
    return {"voices": a.get("voices") or AUDIO_VOICES, "formats": a.get("formats") or AUDIO_FORMATS}


# ── Request construction ──────────────────────────────────────────────────────
def media_request_params(provider, model, *, want_audio=False, audio_voice=None, audio_format=None):
    """Extra keys to merge into an OpenAI-compatible payload. Image-output models
    always request image `modalities`; audio is opt-in via `want_audio`. Returns
    {} for anything not strictly registered as media-capable."""
    reg = registry_output_modalities(model)
    if "image" in reg:
        return {"modalities": ["image", "text"]}
    if want_audio and "audio" in reg:
        fmt = (audio_format or DEFAULT_AUDIO_FORMAT).lower()
        if fmt not in AUDIO_FORMATS:
            fmt = DEFAULT_AUDIO_FORMAT
        return {"modalities": ["text", "audio"],
                "audio": {"voice": audio_voice or DEFAULT_AUDIO_VOICE, "format": fmt}}
    return {}


def wants_audio_output(params):
    """True when media_request_params() asked for audio (→ use the one-shot path)."""
    return "audio" in (params.get("modalities") or [])


# ── Response extraction ───────────────────────────────────────────────────────
def _parse_data_url(url):
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    try:
        header, b64 = url.split(",", 1)
        mime = header[5:].split(";")[0] or None
        return mime, b64
    except Exception:
        return None, None


def _iter_image_urls(container):
    imgs = container.get("images") if isinstance(container, dict) else None
    if not isinstance(imgs, list):
        return
    for it in imgs:
        url = None
        if isinstance(it, dict):
            iu = it.get("image_url")
            if isinstance(iu, dict):
                url = iu.get("url")
            elif isinstance(iu, str):
                url = iu
            url = url or it.get("url")
        elif isinstance(it, str):
            url = it
        if url:
            yield url


def _image_url_to_media(url):
    mime, b64 = _parse_data_url(url)
    if b64:
        return {"media": "image", "mime": mime or "image/png", "b64": b64}
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return {"media": "image", "mime": None, "src_url": url}
    return None


def extract_media_from_openai_delta(delta):
    """Media items from a streaming `choices[].delta` (OpenRouter images / audio)."""
    out = []
    if not isinstance(delta, dict):
        return out
    for url in _iter_image_urls(delta):
        it = _image_url_to_media(url)
        if it:
            out.append(it)
    aud = delta.get("audio")
    if isinstance(aud, dict) and (aud.get("data") or aud.get("transcript")):
        out.append({"media": "audio", "mime": "audio/wav", "b64": aud.get("data") or "",
                    "transcript": aud.get("transcript") or "", "partial": True})
    return out


def extract_media_from_openai_message(message):
    """Media items from a non-streaming `choices[].message` (images / full audio)."""
    out = []
    if not isinstance(message, dict):
        return out
    for url in _iter_image_urls(message):
        it = _image_url_to_media(url)
        if it:
            out.append(it)
    aud = message.get("audio")
    if isinstance(aud, dict) and aud.get("data"):
        out.append({"media": "audio", "mime": "audio/wav", "b64": aud["data"],
                    "transcript": aud.get("transcript") or ""})
    return out


def extract_media_from_anthropic_block(block):
    """Dormant: no current first-party Claude model emits media. Built for
    wire-format completeness (and Anthropic-compatible proxies that might)."""
    if isinstance(block, dict) and block.get("type") == "image":
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return [{"media": "image", "mime": src.get("media_type") or "image/png", "b64": src["data"]}]
    return []


class MediaStreamAccumulator:
    """Collects media across a stream. Images arrive whole; audio fragments are
    concatenated per choice `index` and decoded only at finalize()."""

    def __init__(self):
        self._images = []
        self._audio = {}

    def feed(self, item):
        media = item.get("media")
        if media == "image":
            self._images.append(item)
        elif media == "audio":
            idx = item.get("index", 0)
            slot = self._audio.setdefault(idx, {"b64": [], "transcript": [], "mime": item.get("mime")})
            if item.get("b64"):
                slot["b64"].append(item["b64"])
            if item.get("transcript"):
                slot["transcript"].append(item["transcript"])

    def feed_many(self, items):
        for it in items or []:
            self.feed(it)

    def has_media(self):
        return bool(self._images or self._audio)

    def drain(self):
        out = list(self._images)
        for idx, slot in self._audio.items():
            out.append({"media": "audio", "mime": slot.get("mime") or "audio/wav",
                        "b64": "".join(slot["b64"]),
                        "transcript": "".join(slot["transcript"]), "index": idx})
        self._images.clear()
        self._audio.clear()
        return out


# ── Storage ───────────────────────────────────────────────────────────────────
def _ext_for_mime(mime, media):
    return _MIME_EXT.get((mime or "").lower(), _DEFAULT_EXT.get(media, "bin"))


def _pcm16_to_wav(pcm, rate=24000, channels=1):
    """Wrap raw 16-bit PCM (OpenAI audio is 24kHz mono) in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _download(url):
    """SSRF-checked fetch of a remote media URL (rare — OpenRouter sends data:)."""
    try:
        from src.url_safety import check_outbound_url
        import os as _os
        ok, reason = check_outbound_url(
            url, block_private=_os.getenv("IMAGE_BLOCK_PRIVATE_IPS", "false").lower() == "true")
        if not ok:
            logger.warning("Refusing unsafe media URL: %s", reason)
            return None, None
    except Exception:
        pass
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        if r.status_code == 200:
            return r.content, (r.headers.get("content-type") or "").split(";")[0] or None
    except Exception as e:
        logger.warning("media download failed: %s", e)
    return None, None


def _save_gallery_row(filename, *, media, prompt, model, session_id, owner, size=None):
    """Insert an owner'd GalleryImage row (the owner is what makes the serving
    endpoint's per-user auth work). Returns the new id or ''."""
    try:
        from core.database import SessionLocal, GalleryImage
        new_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add(GalleryImage(
                id=new_id,
                filename=filename,
                prompt=(prompt or "")[:2000],
                model=model,
                owner=owner,
                session_id=session_id,
                media_type=media,
                file_size=size,
            ))
            db.commit()
        finally:
            db.close()
        return new_id
    except Exception as e:
        logger.warning("gallery row insert failed: %s", e)
        return ""


def store_model_media(item, *, prompt="", model="", session_id=None, owner=None):
    """Write a model-emitted media binary to disk + gallery, return a reference
    dict {media, mime, url, id, transcript?, duration_secs?} — or None on failure.
    Never returns base64."""
    media = item.get("media")
    mime = item.get("mime") or _DEFAULT_MIME.get(media)
    try:
        raw = None
        if item.get("b64"):
            raw = base64.b64decode(item["b64"])
        elif item.get("src_url"):
            raw, dl_mime = _download(item["src_url"])
            mime = mime or dl_mime
        if not raw:
            return None
        # Raw PCM (streamed audio) → wrap in WAV so the browser can play it.
        if mime in ("audio/pcm", "audio/pcm16", "audio/l16") or item.get("pcm16"):
            raw = _pcm16_to_wav(raw)
            mime = "audio/wav"
        mime = mime or _DEFAULT_MIME.get(media, "application/octet-stream")
        ext = _ext_for_mime(mime, media)

        d = Path(GENERATED_IMAGES_DIR)
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        (d / filename).write_bytes(raw)

        gid = _save_gallery_row(
            filename, media=media, prompt=prompt or item.get("transcript") or "",
            model=model, session_id=session_id, owner=owner, size=len(raw))

        ref = {"media": media, "mime": mime, "url": f"/api/generated-image/{filename}", "id": gid}
        if item.get("transcript"):
            ref["transcript"] = item["transcript"]
        if item.get("duration_secs"):
            ref["duration_secs"] = item["duration_secs"]
        return ref
    except Exception as e:
        logger.warning("store_model_media failed: %s", e)
        return None


def store_all(items, *, prompt="", model="", session_id=None, owner=None):
    """Store a list of drained media items; return the list of reference dicts."""
    refs = []
    for it in items or []:
        ref = store_model_media(it, prompt=prompt, model=model, session_id=session_id, owner=owner)
        if ref:
            refs.append(ref)
    return refs


# ── Audio: single non-streaming request synthesised into the SSE protocol ─────
def _audio_mime_from_payload(payload):
    fmt = ((payload.get("audio") or {}).get("format") or DEFAULT_AUDIO_FORMAT).lower()
    return _AUDIO_FORMAT_MIME.get(fmt, "audio/mpeg")


async def audio_oneshot_events(target_url, payload, headers, *, model="", session_id=None,
                               owner=None, timeout=180):
    """Audio output is requested NON-streaming (avoids the pcm16/stream-only
    constraint and honours mp3/wav/opus). Performs one request and yields the
    normal stream_llm SSE protocol: a text delta, a model_media event, usage,
    then [DONE]."""
    p = dict(payload)
    p["stream"] = False
    p.pop("stream_options", None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(target_url, json=p, headers=headers)
        if r.status_code >= 400:
            yield f'event: error\ndata: {json.dumps({"status": r.status_code, "text": r.text[:500]})}\n\n'
            return
        data = r.json()
        choice = (data.get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}

        text = msg.get("content")
        if isinstance(text, list):
            text = "".join(seg.get("text", "") for seg in text if isinstance(seg, dict))
        audio = msg.get("audio") or {}
        transcript = audio.get("transcript") or ""
        out_text = (text or "").strip() or transcript
        if out_text:
            yield f'data: {json.dumps({"delta": out_text})}\n\n'

        if audio.get("data"):
            mime = _audio_mime_from_payload(p)
            item = {"media": "audio", "mime": mime, "b64": audio["data"], "transcript": transcript}
            if mime == "audio/pcm":
                item["pcm16"] = True
            ref = store_model_media(item, prompt=transcript, model=model,
                                    session_id=session_id, owner=owner)
            if ref:
                yield f'data: {json.dumps({"type": "model_media", **ref})}\n\n'

        usage = data.get("usage") or {}
        if usage:
            yield ('data: ' + json.dumps({"type": "usage", "data": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0)}}) + '\n\n')
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield f'event: error\ndata: {json.dumps({"status": 502, "text": str(e)})}\n\n'


# ── Async video generation (submit → poll → download), MiniMax-style ──────────
def _video_api_base(base_url):
    """Normalise a chat endpoint URL to the provider's /v1 API root."""
    base = (base_url or "").rstrip("/")
    for suf in ("/chat/completions", "/completions", "/v1/messages", "/messages"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


async def generate_video_events(base_url, headers, model, prompt, *, session_id=None,
                                owner=None, poll_interval=5, max_wait=600):
    """Drive an async video job and yield the stream_llm SSE protocol:
    a status `delta`, `media_job_progress` ticks, then a final `model_media`
    (video) once stored. Targets the MiniMax video API (submit → query → files);
    other providers (veo/sora) surface a clear error rather than crashing.
    Does NOT emit [DONE] — the caller owns end-of-stream + persistence."""
    base = _video_api_base(base_url)
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{base}/video_generation",
                                  json={"model": model, "prompt": prompt}, headers=h)
            if r.status_code >= 400:
                yield f'data: {json.dumps({"delta": f"[Video submit failed: {r.status_code} {r.text[:200]}]"})}\n\n'
                return
            task_id = (r.json() or {}).get("task_id")
            if not task_id:
                yield f'data: {json.dumps({"delta": "[Video submit returned no task_id]"})}\n\n'
                return
            yield f'data: {json.dumps({"type": "media_job_progress", "status": "queued", "task_id": task_id})}\n\n'

            waited, file_id = 0, None
            while waited < max_wait:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                q = await client.get(f"{base}/query/video_generation",
                                     params={"task_id": task_id}, headers=h)
                qj = q.json() if q.status_code < 400 else {}
                status = qj.get("status", "")
                yield f'data: {json.dumps({"type": "media_job_progress", "status": status or "processing", "task_id": task_id})}\n\n'
                yield ": heartbeat\n\n"
                if status in ("Success", "success"):
                    file_id = qj.get("file_id")
                    break
                if status in ("Fail", "Failed", "fail"):
                    yield f'data: {json.dumps({"delta": "[Video generation failed]"})}\n\n'
                    return
            if not file_id:
                yield f'data: {json.dumps({"delta": "[Video generation timed out]"})}\n\n'
                return

            fr = await client.get(f"{base}/files/retrieve", params={"file_id": file_id}, headers=h)
            download_url = None
            if fr.status_code < 400:
                download_url = ((fr.json() or {}).get("file") or {}).get("download_url")
            if not download_url:
                yield f'data: {json.dumps({"delta": "[Could not retrieve generated video URL]"})}\n\n'
                return

            ref = store_model_media(
                {"media": "video", "mime": "video/mp4", "src_url": download_url},
                prompt=prompt, model=model, session_id=session_id, owner=owner)
            if ref:
                yield f'data: {json.dumps({"type": "model_media", **ref})}\n\n'
            else:
                yield f'data: {json.dumps({"delta": "[Failed to store generated video]"})}\n\n'
    except Exception as e:
        yield f'data: {json.dumps({"delta": f"[Video generation error: {e}]"})}\n\n'


# ── MiniMax dedicated image / audio generation (verified against api.minimax.io)
async def minimax_image_events(base_url, headers, model, prompt, *, session_id=None, owner=None):
    """MiniMax image-01 via POST /v1/image_generation → data.image_base64[0] (JPEG).
    Yields a status delta + a final model_media(image). No [DONE] (caller owns it)."""
    base = _video_api_base(base_url)
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(f"{base}/image_generation", headers=h, json={
                "model": model, "prompt": prompt, "aspect_ratio": "1:1",
                "response_format": "base64", "n": 1, "prompt_optimizer": True})
        if r.status_code >= 400:
            yield f'data: {json.dumps({"delta": f"[Image generation failed: HTTP {r.status_code} {r.text[:200]}]"})}\n\n'
            return
        j = r.json() or {}
        br = j.get("base_resp") or {}
        if br.get("status_code"):
            yield f'data: {json.dumps({"delta": "[Image generation failed: " + (br.get("status_msg") or "error") + "]"})}\n\n'
            return
        data = j.get("data") or {}
        imgs = data.get("image_base64") or []
        urls = data.get("image_urls") or []
        if imgs:
            ref = store_model_media({"media": "image", "mime": "image/jpeg", "b64": imgs[0]},
                                    prompt=prompt, model=model, session_id=session_id, owner=owner)
        elif urls:
            ref = store_model_media({"media": "image", "mime": None, "src_url": urls[0]},
                                    prompt=prompt, model=model, session_id=session_id, owner=owner)
        else:
            ref = None
        if ref:
            yield f'data: {json.dumps({"type": "model_media", **ref})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "[Image generation returned no data]"})}\n\n'
    except Exception as e:
        yield f'data: {json.dumps({"delta": f"[Image generation error: {e}]"})}\n\n'


# ── Voice-directive extraction (man/woman, emotion, tone) ────────────────────
# The T2A API exposes voice_id (a fixed system voice), emotion (7 moods), and
# voice_modify (pitch/intensity/timbre sliders). Natural-language prompts like
# "generate a man's voice saying X" or "say X in a happy, deep voice" need to
# be parsed so we pass those fields through — otherwise the API plays X with
# the default voice regardless of what was asked.
#
# MiniMax T2A v2 system-voice catalogue (English subset; full list lives at
# platform.minimax.io/docs/faq/system-voice-id). Mapped from natural-language
# descriptors → voice_id. The first match wins; prefer explicit voice names
# ("a Wise_Woman voice") before falling back to gender/age.
_MINIMAX_ENGLISH_VOICE_ALIASES = {
    # gender × age → sensible default
    "narrator": "English_expressive_narrator",
    "storyteller": "English_CaptivatingStoryteller",
    "young man": "English_ReservedYoungMan",
    "young woman": "English_Graceful_Lady",
    "old man": "English_Deep-VoicedGentleman",
    "elderly man": "English_Deep-VoicedGentleman",
    "old woman": "English_MatureBoss",
    "elderly woman": "English_MatureBoss",
    "teen boy": "English_SadTeen",
    "teen girl": "English_SadTeen",
    "child": "English_AnimeCharacter",
    "kid": "English_AnimeCharacter",
    "robot": "English_Lucky_Robot",
    # singular/plural — checked after the multi-word combos above
    "man": "English_ManWithDeepVoice",
    "male": "English_ManWithDeepVoice",
    "guy": "English_Trustworth_Man",
    "boy": "English_ReservedYoungMan",
    "woman": "English_Graceful_Lady",
    "female": "English_Graceful_Lady",
    "lady": "English_Graceful_Lady",
    "girl": "English_radiant_girl",
}

# Explicit MiniMax voice IDs are accepted verbatim (case-insensitive). Anything
# not in this set is ignored so a stray word can't pick a random voice.
_MINIMAX_VALID_VOICE_IDS = {
    "English_expressive_narrator", "English_radiant_girl",
    "English_magnetic_voiced_man", "English_compelling_lady1",
    "English_Aussie_Bloke", "English_captivating_female1",
    "English_Upbeat_Woman", "English_Trustworth_Man",
    "English_CalmWoman", "English_UpsetGirl",
    "English_Gentle-voiced_man", "English_Whispering_girl",
    "English_Diligent_Man", "English_Graceful_Lady",
    "English_ReservedYoungMan", "English_PlayfulGirl",
    "English_ManWithDeepVoice", "English_MaturePartner",
    "English_FriendlyPerson", "English_MatureBoss",
    "English_Debator", "English_LovelyGirl",
    "English_Steadymentor", "English_Deep-VoicedGentleman",
    "English_Wiselady", "English_CaptivatingStoryteller",
    "English_DecentYoungMan", "English_SentimentalLady",
    "English_ImposingManner", "English_SadTeen",
    "English_PassionateWarrior", "English_WiseScholar",
    "English_Soft-spokenGirl", "English_SereneWoman",
    "English_ConfidentWoman", "English_PatientMan",
    "English_Comedian", "English_BossyLeader",
    "English_Strong-WilledBoy", "English_StressedLady",
    "English_AssertiveQueen", "English_AnimeCharacter",
    "English_Jovialman", "English_WhimsicalGirl",
    "English_Kind-heartedGirl", "Wise_Woman", "English_Lucky_Robot",
}

# emotion field — exact API enum (T2A v2). Map synonyms → enum value.
# Each synonym may be either a single token (matched with word boundaries so
# 'angry' doesn't match 'angrily') OR an explicit suffix form ('angrily') that
# we list separately. Order matters: longer/specific forms come first.
_EMOTION_KEYWORDS = [
    (("happy", "cheerful", "joyful", "delighted", "excited", "enthusiastic", "enthusiasm", "in a good mood"), "happy"),
    (("sad", "depressed", "down", "melancholy", "sorrowful", "gloomy"), "sad"),
    (("angrily", "angry", "mad", "furious", "irritated", "annoyed", "outraged"), "angry"),
    (("scared", "afraid", "fearful", "terrified", "anxious", "nervous"), "fearful"),
    (("disgusted", "revolted", "sickened", "appalled"), "disgusted"),
    (("surprised", "shocked", "amazed", "astonished"), "surprised"),
    (("calm", "peaceful", "relaxed", "serene", "soothing", "tranquil"), "calm"),
    (("fluently", "fluent", "smoothly", "smooth"), "fluent"),
    (("whispering", "whisper", "soft-spoken"), "whisper"),
]

# voice_modify sliders (-100..100). Words are matched as whole tokens where
# possible so they don't eat substrings of unrelated words ("lowly" must not
# trigger pitch=-50). For each axis the first match wins — the user gave one
# instruction per axis, we don't accumulate. Order in the union list below
# is significant for axis assignment; see _detect_tone_axes below.
_PITCH_PATTERNS = [
    (r"\b(deep(?:er)?|low(?:er)?|deep[-\s]?voiced|deeper[-\s]?voice)\b", -50),
    (r"\b(high(?:er)?|high[-\s]?pitched|bright(?:er)?|shrill)\b", 50),
]
_INTENSITY_PATTERNS = [
    (r"\b(soft(?:ly)?|gentle(?:ly)?|quiet(?:ly)?|tender(?:ly)?|mild(?:ly)?)\b", 50),
    (r"\b(loud(?:ly)?|forceful(?:ly)?|strong(?:ly)?|intense(?:ly)?|powerful(?:ly)?)\b", -50),
]
_TIMBRE_PATTERNS = [
    (r"\b(rich(?:ly)?|full(?:er)?|warm(?:ly)?|resonant)\b", -30),
    (r"\b(crisp(?:ly)?|sharp(?:ly)?|clear(?:ly)?)\b", 30),
]


def _match_emotion(text, kw):
    """Emotion kw matcher: word-boundary for single tokens so 'angry' doesn't
    match 'angrily' (and 'angrily' is its own kw). Multi-word kws use plain
    substring."""
    if " " in kw or "'" in kw:
        return kw in text
    return re.search(rf"\b{re.escape(kw)}\b", text) is not None


def _strip_match(pattern, text):
    """Remove all matches of `pattern` from text (case-insensitive)."""
    return re.sub(pattern, " ", text, flags=re.I)


# ── Meta-instruction stripping ─────────────────────────────────────────────
# Users rarely type the exact thing they want TTS to say. They wrap it in
# instructions: "generate an audio of a man saying 'i love burgers' should be
# excited while he says it", "please read 'hello world' in a sad tone", "create
# a calm voice saying 'take a deep breath'". The T2A model will happily read
# the whole thing back literally, including the meta-instructions, which is
# almost never what the user wanted.
#
# `parse_voice_directives` already extracts the voice/emotion/tone fields.
# This pre-pass extracts the SPOKEN TEXT itself so what reaches the T2A API
# is just the words the user wanted spoken. Runs BEFORE voice extraction
# because quoted segments often contain gender/emotion words that we still
# want as directives (e.g. "an excited man" → emotion=happy, voice=male).
_META_FRAMING_PATTERNS = [
    # Leading imperative: "generate/create/make an audio of X", "read aloud: X",
    # "say X", "please produce a voice saying X", etc.
    r"^\s*(?:please\s+)?(?:generate|create|make|render|produce|synthesize|tts|read(?:\s+aloud)?|say|speak|narrate)\s+(?:an?\s+|the\s+|some\s+|a\s+piece\s+of\s+)?(?:audio|speech|voice|voiceover|tts|clip|recording|narration|message)\b[^:]*?[:\s]+",
    # Wrap clause mid-message: "of a man saying", "of a woman speaking",
    # "where he says", "that says/reads"
    r"\bof\s+(?:a|an|the)\s+[\w'-]+\s+(?:saying|reading|speaking|narrating)\b",
    r"\bsaying\s+the\s+following\s*:?\s*",
    r"\bthat\s+(?:says|reads|speaks|narrates)\b",
    r"\bwhere\s+(?:he|she|they|it)\s+says\b",
    r"\bwhere\s+(?:he|she|they|it)\s+is\s+saying\b",
    # Trailing directions: "should be excited while he says it",
    # "with enthusiasm", "with a happy tone", "while speaking in a deep voice"
    r"\bshould\s+be\s+\w+(?:\s+(?:while|when|as)\s+(?:he|she|they|it)\s+says?(?:\s+(?:it|this|them))?)?",
    r"\bwith\s+(?:enthusiasm|emotion|feeling|energy|passion|excitement|feeling)\b",
    r"\bin\s+a\s+(?:\w+\s+){0,3}tone\b",
    r"\b(?:while|as)\s+(?:speaking|saying|reading|narrating)(?:\s+(?:it|this|them))?\b",
]


def _strip_meta_framing(raw_text):
    """Strip meta-instructions and wrap clauses from a TTS request, leaving
    just the words the user wants spoken.

    Order matters:
      1. If a quoted segment is present, prefer its content as the candidate
         spoken text — by far the most common case (`"…"` / `'…'` / `«…»`).
      2. Otherwise strip leading imperative framing + wrap clauses.
      3. Strip trailing direction phrases.
      4. Tidy whitespace/punctuation.

    Conservative: returns the input (tidied) when nothing matched, so we
    never silently rewrite a clean prompt.
    """
    if not raw_text:
        return raw_text or ""

    text = raw_text

    # 1. Quoted segment wins.
    #
    # Care needed: an apostrophe inside a possessive like "man's" is the
    # SAME character as a single-quote delimiter. A naive `'(.*)'` regex
    # finds the WRONG pair — it greedily matches from the apostrophe in
    # "man's" to the first real opening quote, eating real content. We
    # instead enumerate every apostrophe position, consider each as a
    # candidate opener, and require (a) the opener to NOT be glued to a
    # letter on its left (excludes possessives) and (b) the body to look
    # like a real phrase, not a contraction (length > 3 or contains a
    # non-letter). The longest valid pair wins.
    candidates = []

    # Double quotes: never a possessive issue. Straightforward regex.
    for m in re.finditer(r'"([^"\n]+)"', text):
        body = m.group(1).strip()
        if len(body) >= 3:
            candidates.append((len(body), body))

    # Guillemets: same as double quotes, no ambiguity.
    for m in re.finditer(r"«([^»\n]+)»", text):
        body = m.group(1).strip()
        if len(body) >= 3:
            candidates.append((len(body), body))

    # Single quotes: enumerate opner/closer pairs manually so a possessive
    # "man's" doesn't claim the apostrophe before the real spoken text.
    single_quote_positions = [i for i, c in enumerate(text) if c == "'"]
    for i_idx, start in enumerate(single_quote_positions):
        # Reject openers glued to a letter (possessive / contraction).
        if start > 0 and text[start - 1].isalpha():
            continue
        # Reject openers that are inside a previously-claimed match
        # (greedy left-to-right binding for this opener).
        for end in single_quote_positions[i_idx + 1:]:
            body = text[start + 1:end].strip()
            if len(body) < 3:
                continue
            # Reject obvious contractions: s, t, re, ve, ll, d.
            if re.fullmatch(r"[a-zA-Z]+", body) and len(body) <= 3:
                continue
            # First valid body wins for this opener (greedy left-to-right).
            candidates.append((len(body), body))
            break

    quoted = max(candidates)[1] if candidates else None

    if quoted is not None:
        text = quoted
    else:
        # 2. No quotes → strip meta-framing around an inline spoken phrase.
        for pat in _META_FRAMING_PATTERNS:
            text = re.sub(pat, " ", text, flags=re.I)

    # 3. Tidy.
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s\"'\-:;,.]+", "", text)
    text = re.sub(r"[\s\"'\-:;,.]+$", "", text)
    # The "of a X" intro word sometimes survives (e.g. "of saying the
    # following" → after strip becomes "of"). Drop a leading "of" if it's
    # the only word left in a fragment.
    text = re.sub(r"^of\s+", "", text, flags=re.I)
    # A leading "the following" / "as follows" with no quoted content is
    # meaningless after stripping the wrap clause; drop it.
    text = re.sub(r"^(?:the\s+following|as\s+follows)\b[\s:]*", "", text, flags=re.I)

    return text or raw_text.strip()


def parse_voice_directives(raw_text):
    """Extract MiniMax T2A voice parameters from a free-form prompt.

    Returns ``(clean_text, voice_id, emotion, voice_modify)``:
      * ``clean_text`` — the spoken text with meta-instructions AND voice
        directives stripped (so the TTS doesn't read "a man's voice" or
        "generate an audio of…" literally).
      * ``voice_id`` — a MiniMax system voice id, or ``None`` to keep the
        default.
      * ``emotion`` — one of the T2A emotion enum values, or ``None``.
      * ``voice_modify`` — dict with any of ``pitch`` / ``intensity`` /
        ``timbre`` (each ``-100..100``), or ``None`` if nothing matched.

    Order matters: meta-instruction strip → voice-id → gender/age → emotion →
    tone → framing cleanup. Meta-instruction strip runs FIRST so a quoted
    spoken text like ``"i love burgers"`` is isolated before any inner words
    can be misread as directives (and a ``man`` inside the quote still maps
    to a male voice via step 2 on the *original* message, not the quote).

    Conservative on misses: never silently rewrites the spoken text — if no
    directive is found, returns the input verbatim (with whitespace tidied).
    """
    if not raw_text:
        return raw_text or "", None, None, None

    # 0. Meta-instruction strip: isolate the spoken text from wrapping
    #    instructions. We extract directives from BOTH the original message
    #    (so voice/emotion from the wrap are still found) and the cleaned
    #    text (so the TTS doesn't read the wrap back literally).
    pre_clean = _strip_meta_framing(raw_text)
    # Voice/emotion/tone are pulled from the *original* message — that's
    # where the user expressed them ("an excited man" carries both
    # emotion=happy and voice=male regardless of which word survived into
    # pre_clean). The TTS text comes from pre_clean.
    text = pre_clean
    lowered = raw_text.lower()  # match against the original
    voice_id = None
    emotion = None
    vm = {}

    # Normalized lookup: lowercase + strip underscores/hyphens so a user typing
    # any case variant of a MiniMax voice id (e.g. "English_CompellingLady1")
    # resolves to the canonical form ("English_compelling_lady1").
    def _norm(s):
        return re.sub(r"[_-]", "", s).lower()
    _valid_normalized = {_norm(v): v for v in _MINIMAX_VALID_VOICE_IDS}

    # 1. Explicit voice IDs (e.g. "a Wise_Woman voice") — checked before gender
    #    words so a user naming a specific voice wins.
    for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9_-]{3,})\b", raw_text):
        cand = m.group(1)
        canonical = _valid_normalized.get(_norm(cand))
        if canonical:
            voice_id = canonical
            lowered = _strip_match(rf"\b{re.escape(cand)}\b", lowered)
            break

    # 2. Gender/age descriptors. Multi-word combos first so "young man" beats
    #    the single "man".
    if voice_id is None:
        for alias, vid in _MINIMAX_ENGLISH_VOICE_ALIASES.items():
            if " " in alias and alias in lowered:
                voice_id = vid
                lowered = _strip_match(re.escape(alias), lowered)
                break
        if voice_id is None:
            for alias, vid in _MINIMAX_ENGLISH_VOICE_ALIASES.items():
                if " " not in alias and re.search(rf"\b{re.escape(alias)}\b", lowered):
                    voice_id = vid
                    lowered = _strip_match(rf"\b{re.escape(alias)}\b", lowered)
                    break

    # 3. Emotion — first match wins; later matches ignored so incidental words
    #    ("angry man") don't dominate over a more specific user intent.
    for kws, emoval in _EMOTION_KEYWORDS:
        for kw in kws:
            if _match_emotion(lowered, kw):
                emotion = emoval
                lowered = _strip_match(rf"\b{re.escape(kw)}\b", lowered)
                break
        if emotion:
            break

    # 4. Tone sliders — extracted BEFORE the framing strip so "in a deep voice"
    #    still yields pitch=-50 (the framing regex would otherwise eat "deep").
    for pat, val in _PITCH_PATTERNS:
        if re.search(pat, lowered):
            vm["pitch"] = val
            lowered = _strip_match(pat, lowered)
            break
    for pat, val in _INTENSITY_PATTERNS:
        if re.search(pat, lowered):
            vm["intensity"] = val
            lowered = _strip_match(pat, lowered)
            break
    for pat, val in _TIMBRE_PATTERNS:
        if re.search(pat, lowered):
            vm["timbre"] = val
            lowered = _strip_match(pat, lowered)
            break

    # 5. Framing cleanup on the spoken-text candidate — strips any leftover
    #    wrapping like "in a man's voice", "with a woman's voice", "a X voice
    #    saying". Run on `text` (pre_clean) so the final TTS payload is clean.
    text = re.sub(
        r"\b(in|with|using|by|via)\s+(a|an|the)\s+[\w']+\s+(voice|voices|tone|tones)\b",
        " ", text, flags=re.I)
    text = re.sub(
        r"\b(a|an|the)\s+[\w']+\s+(voice|voices)\s+(saying|speaking|reading|narrating|that\s+says|that\s+reads)\b",
        " ", text, flags=re.I)
    text = re.sub(
        r"\b(voice|tone)\s+(saying|speaking|reading|narrating)\b",
        " ", text, flags=re.I)

    # 6. Tidy: collapse whitespace, strip leading connectors and quotes.
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s\"'\-:;,.]+", "", text)
    text = re.sub(r"[\s\"'\-:;,.]+$", "", text)
    text = re.sub(r"^(that|which|who)\s+", "", text, flags=re.I)
    text = re.sub(r"^(say|saying|read|reading|speak|speaking|narrate|narrating)\s+", "", text, flags=re.I)

    # If our pre_clean + framing strip ate everything (a degenerate message
    # like just "generate an audio"), fall back to the raw_text so the user
    # gets *something* spoken rather than silence.
    return text or raw_text.strip(), voice_id, emotion, (vm or None)


async def minimax_audio_events(base_url, headers, model, text, *, voice=None, fmt="mp3",
                               emotion=None, voice_modify=None,
                               session_id=None, owner=None):
    """MiniMax speech (T2A) via POST /v1/t2a_v2 → data.audio (HEX-encoded mp3).
    Yields a status delta + a final model_media(audio). No [DONE] (caller owns it)."""
    base = _video_api_base(base_url)
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    voice_setting = {"voice_id": voice or "English_expressive_narrator",
                     "speed": 1.0, "vol": 1.0, "pitch": 0}
    if emotion:
        voice_setting["emotion"] = emotion
    body = {"model": model, "text": (text or "")[:5000], "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {"format": fmt, "sample_rate": 32000, "bitrate": 128000, "channel": 1}}
    if voice_modify:
        # Strip Nones so we only send axes the caller actually set.
        body["voice_modify"] = {k: int(v) for k, v in voice_modify.items() if isinstance(v, (int, float))}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{base}/t2a_v2", headers=h, json=body)
        if r.status_code >= 400:
            yield f'data: {json.dumps({"delta": f"[Audio generation failed: HTTP {r.status_code} {r.text[:200]}]"})}\n\n'
            return
        j = r.json() or {}
        br = j.get("base_resp") or {}
        if br.get("status_code"):
            yield f'data: {json.dumps({"delta": "[Audio generation failed: " + (br.get("status_msg") or "error") + "]"})}\n\n'
            return
        audio_hex = (j.get("data") or {}).get("audio") or ""
        if not audio_hex:
            yield f'data: {json.dumps({"delta": "[Audio generation returned no data]"})}\n\n'
            return
        b64 = base64.b64encode(bytes.fromhex(audio_hex)).decode()
        mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac", "pcm": "audio/wav"}.get(fmt, "audio/mpeg")
        ref = store_model_media({"media": "audio", "mime": mime, "b64": b64, "transcript": text[:500]},
                                prompt=(text or "")[:200], model=model, session_id=session_id, owner=owner)
        if ref:
            yield f'data: {json.dumps({"type": "model_media", **ref})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "[Failed to store generated audio]"})}\n\n'
    except Exception as e:
        yield f'data: {json.dumps({"delta": f"[Audio generation error: {e}]"})}\n\n'


async def minimax_music_events(base_url, headers, model, prompt, *, lyrics=None, fmt="mp3",
                               session_id=None, owner=None):
    """MiniMax music via POST /v1/music_generation → data.audio (HEX mp3). `prompt`
    is a style/mood description; `lyrics` defaults to instrumental. Renders as audio."""
    base = _video_api_base(base_url)
    h = dict(headers or {})
    h.setdefault("Content-Type", "application/json")
    body = {"model": model, "prompt": (prompt or "")[:600],
            "lyrics": (lyrics or "[instrumental]")[:600],
            "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": fmt}}
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            r = await client.post(f"{base}/music_generation", headers=h, json=body)
        if r.status_code >= 400:
            yield f'data: {json.dumps({"delta": f"[Music generation failed: HTTP {r.status_code} {r.text[:200]}]"})}\n\n'
            return
        j = r.json() or {}
        br = j.get("base_resp") or {}
        if br.get("status_code"):
            yield f'data: {json.dumps({"delta": "[Music generation failed: " + (br.get("status_msg") or "error") + "]"})}\n\n'
            return
        audio_hex = (j.get("data") or {}).get("audio") or ""
        if not audio_hex:
            yield f'data: {json.dumps({"delta": "[Music generation returned no data]"})}\n\n'
            return
        b64 = base64.b64encode(bytes.fromhex(audio_hex)).decode()
        ref = store_model_media({"media": "audio", "mime": "audio/mpeg", "b64": b64, "transcript": (prompt or "")[:200]},
                                prompt=(prompt or "")[:200], model=model, session_id=session_id, owner=owner)
        if ref:
            yield f'data: {json.dumps({"type": "model_media", **ref})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "[Failed to store generated music]"})}\n\n'
    except Exception as e:
        yield f'data: {json.dumps({"delta": f"[Music generation error: {e}]"})}\n\n'


def generate_media_events(kind, base_url, headers, model, prompt, *,
                          voice=None, emotion=None, voice_modify=None,
                          session_id=None, owner=None):
    """Dispatch a dedicated media-generation model to its provider adapter."""
    if kind == "image":
        return minimax_image_events(base_url, headers, model, prompt, session_id=session_id, owner=owner)
    if kind == "audio":
        return minimax_audio_events(
            base_url, headers, model, prompt,
            voice=voice, emotion=emotion, voice_modify=voice_modify,
            session_id=session_id, owner=owner,
        )
    if kind == "music":
        return minimax_music_events(base_url, headers, model, prompt, session_id=session_id, owner=owner)
    if kind == "video":
        return generate_video_events(base_url, headers, model, prompt, session_id=session_id, owner=owner)
    return None


# ── Smart routing: evaluate a chat message → pick a media modality + model ─────
# Latest-first candidate lists per kind. MiniMax does NOT expose media models via
# /v1/models, so "always use the latest version" is maintained here: put the
# newest version FIRST and routing auto-selects it (with graceful fallback to the
# next candidate if the newest isn't available on the account). Bump these when
# MiniMax ships a newer image/speech/music/video model.
MINIMAX_MEDIA_CANDIDATES = {
    "image": ["image-01"],
    "audio": ["speech-02-hd", "speech-02-turbo"],
    "music": ["music-1.5", "music-01"],
    "video": ["MiniMax-Hailuo-02", "T2V-01-Director", "video-01"],
}


def media_candidates(kind):
    """Ordered (newest-first) model candidates for a media kind."""
    return list(MINIMAX_MEDIA_CANDIDATES.get(kind) or [])


def latest_media_model(kind):
    """The newest media model for a kind (first candidate), or None."""
    c = MINIMAX_MEDIA_CANDIDATES.get(kind) or []
    return c[0] if c else None


# kind → latest model id (derived; the candidate list above is the source of truth).
MINIMAX_MEDIA_FOR_KIND = {k: v[0] for k, v in MINIMAX_MEDIA_CANDIDATES.items() if v}

# Cheap pre-gate so normal Q&A never pays for a classification call. Broad on
# purpose (recall over precision) — the LLM classifier makes the real decision.
_MEDIA_GATE = re.compile(
    r"\b(draw|sketch|illustrate|paint|render|animate)\b"
    r"|\b(generate|create|make|produce|design|give me|need|want|gimme)\b.{0,30}"
    r"\b(image|picture|photo|drawing|art|logo|wallpaper|portrait|illustration|icon|"
    r"video|animation|clip|gif|movie|audio|speech|voice|narration|song|music|sound)\b"
    r"|\b(image|picture|photo|video|animation|audio|speech|voice|song)\b.{0,15}\b(of|for|showing)\b"
    r"|\b(read .{0,20}aloud|text to speech|tts|voiceover|say this|speak this|narrate)\b",
    re.I)


def maybe_media_request(text):
    """Fast heuristic gate: does this message plausibly ask to generate media?"""
    return bool(text and _MEDIA_GATE.search(text))


def _parse_intent_json(raw):
    """Parse the LLM classifier's JSON. Returns ``(kind, prompt, voice, emotion,
    voice_modify)``. The voice fields are optional (None when the classifier
    didn't surface them); a regex fallback in ``parse_voice_directives`` is
    applied by callers so we never lose a user-specified voice characteristic
    to an LLM mis-extraction."""
    if not raw:
        return None, None, None, None, None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None, None, None, None, None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None, None, None, None, None
    action = (obj.get("action") or "").strip().lower()
    prompt = (obj.get("prompt") or "").strip()
    voice = obj.get("voice") or obj.get("voice_id") or None
    emotion = obj.get("emotion") or None
    vm_raw = obj.get("voice_modify") or obj.get("tone") or None
    vm = None
    if isinstance(vm_raw, dict):
        vm = {k: int(v) for k, v in vm_raw.items()
              if k in ("pitch", "intensity", "timbre") and isinstance(v, (int, float))}
        vm = vm or None
    if action not in ("image", "audio", "video", "music") or not prompt:
        return None, None, None, None, None
    return action, prompt, voice, emotion, vm


_INTENT_SYSTEM = (
    "You are an intent router for a chat app that can GENERATE media. Decide whether "
    "the user's latest message is asking to CREATE/GENERATE new media. Reply with ONLY a "
    "JSON object and nothing else:\n"
    '{"action":"image|audio|music|video|chat","prompt":"<concise generation prompt>",'
    '"voice":"<voice_id or null>","emotion":"<emotion or null>",'
    '"voice_modify":{"pitch":<-100..100>,"intensity":<-100..100>,"timbre":<-100..100>}}\n'
    "- image: a picture/photo/art/logo/illustration.\n"
    "- audio: spoken speech / text-to-speech / read-aloud / voiceover. prompt = the EXACT text "
    "to be spoken aloud. If the user's message contains a quoted segment (in single or double "
    "quotes), that quoted segment IS the prompt. NEVER include any framing, wrap, or direction "
    "phrases in the prompt — strip them into the voice/emotion/voice_modify fields instead. "
    "Stripped examples (do NOT put these in prompt):\n"
    "  - leading imperatives: 'generate an audio of', 'create a voice saying', 'please read', "
    "'say the following', 'tts', 'speak this'\n"
    "  - wrap clauses: 'of a man saying', 'where he says', 'that says', 'saying the following:'\n"
    "  - trailing directions: 'should be excited while he says it', 'with enthusiasm', "
    "'in a happy tone', 'while speaking in a deep voice'\n"
    "- music: a song, melody, instrumental, beat, or background/study music (prompt = a "
    "style/mood/genre description).\n"
    "- video: a video / animation / clip.\n"
    "- chat: questions, conversation, code, explanations, edits, or anything that is not a "
    "request to produce new media.\n"
    "Only choose image/audio/music/video when the user clearly wants new media generated.\n"
    "VOICE fields (leave null unless the user asked):\n"
    "  voice — MiniMax system voice id like English_ManWithDeepVoice, English_Graceful_Lady, "
    "English_expressive_narrator, English_CaptivatingStoryteller, etc. Pick one that matches "
    "the requested gender/age/persona (man/woman/boy/girl/elder/narrator/child/robot).\n"
    "  emotion — exactly one of: happy, sad, angry, fearful, disgusted, surprised, calm, "
    "fluent, whisper. Set only if the user asked for that mood.\n"
    "  voice_modify — pitch (-100 deep .. +100 bright), intensity (-100 strong .. +100 soft), "
    "timbre (-100 rich .. +100 crisp). Set only the axes the user described (deep/high/soft/"
    "loud/rich/crisp)."
)


async def classify_media_intent(text, url, model, headers):
    """Ask the chat model to classify a possible media-generation request.
    Returns ``(kind, prompt, voice, emotion, voice_modify)`` where any field
    may be ``None``. Callers should run ``parse_voice_directives()`` on the
    user's original message as a regex fallback — the LLM is non-deterministic
    and we never want a user-specified voice characteristic to be silently
    dropped."""
    from src.llm_core import llm_call_async  # lazy: llm_core imports this module
    msgs = [{"role": "system", "content": _INTENT_SYSTEM},
            {"role": "user", "content": (text or "")[:2000]}]
    try:
        raw = await llm_call_async(url, model, msgs, temperature=0, max_tokens=300, headers=headers)
    except Exception as e:
        logger.warning("media intent classify failed: %s", e)
        return None, None, None, None, None
    return _parse_intent_json(raw)


def resolve_media_endpoint(kind, owner=None):
    """Find a configured endpoint that can generate `kind` media (currently
    MiniMax api.minimax.io). Returns (base_url, headers, model) or None."""
    model = MINIMAX_MEDIA_FOR_KIND.get(kind)
    if not model:
        return None
    try:
        from core.database import SessionLocal, ModelEndpoint
    except Exception:
        return None
    try:
        from src.auth_helpers import owner_filter
    except Exception:
        owner_filter = None
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner and owner_filter:
            try:
                q = owner_filter(q, ModelEndpoint, owner)
            except Exception:
                pass
        for ep in q.all():
            if "minimax.io" in (ep.base_url or ""):
                key = ep.api_key
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                return ep.base_url, headers, model
    finally:
        db.close()
    return None


def media_record(kind, prompt, model=None):
    """A past-tense textual record saved into the assistant message so the model
    sees in later turns that it generated this media (unified context)."""
    p = (prompt or "").strip()
    if len(p) > 280:
        p = p[:280] + "…"
    if kind == "audio":
        return f'Here\'s the audio I generated saying: "{p}"'
    if kind == "music":
        return f"Here's the music I generated — {p}"
    if kind == "video":
        return f"Here's the video I generated for: {p}"
    return f"Here's the image I generated for: {p}"
