"""Inbound Telegram voice/audio validation, local faster-whisper + cloud STT transcription, and spoken routing."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.media import MediaValidationError, is_heic_bytes

MAX_INBOUND_AUDIO_BYTES = 20 * 1024 * 1024
MAX_VOICE_DURATION_SECONDS = 600
MAX_PENDING_VOICE_NOTES = 32
PENDING_VOICE_TTL_SECONDS = 900.0
MAX_TRANSCRIPT_PREVIEW_CHARS = 600

DEFAULT_VOICE_PROVIDER = "auto"
DEFAULT_LOCAL_VOICE_MODEL = "large-v3-turbo"
DEFAULT_OPENAI_VOICE_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_GROQ_VOICE_MODEL = "whisper-large-v3-turbo"
DEFAULT_GEMINI_VOICE_MODEL = "gemini-2.5-flash"

SUPPORTED_VOICE_PROVIDERS = frozenset(
    {
        "auto",
        "off",
        "none",
        "disabled",
        "local",
        "faster-whisper",
        "openai",
        "groq",
        "gemini-api",
        "google",
    }
)
DISABLED_VOICE_VALUES = frozenset({"off", "none", "disabled", "false", "0"})

VOICE_PROVIDER_API_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini-api": "https://generativelanguage.googleapis.com/v1beta",
}

OVERSIZED_AUDIO_MESSAGE = "⚠️ Voice/audio clip exceeds Telegram's 20 MB bot download limit."
LONG_AUDIO_MESSAGE = "⚠️ Voice/audio clip exceeds the 10-minute transcription limit."
UNSUPPORTED_AUDIO_MESSAGE = (
    "⚠️ Unsupported or corrupted audio format. Supported formats: OGG/Opus, M4A, MP3, WAV, FLAC, and WebM."
)

_SUPPORTED_AUDIO_EXTS = (".ogg", ".oga", ".opus", ".m4a", ".mp4", ".mp3", ".wav", ".flac", ".webm")
_SUPPORTED_AUDIO_MIMES = frozenset(
    {
        "audio/ogg",
        "audio/opus",
        "audio/vorbis",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/aac",
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/flac",
        "audio/x-flac",
        "audio/webm",
    }
)

SAFE_VOICE_SUBPROCESS_ENV_KEYS = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

_SPOKEN_PREFIX_RE = re.compile(
    r"^\s*(?:(?:hey|ok|okay)\s+)?(?P<head>[A-Za-z0-9][A-Za-z0-9 _.-]{0,30}?)\s*[:,;]\s+(?P<body>\S.*)$",
    re.DOTALL,
)

_FASTER_WHISPER_SCRIPT = """
import json
import os
import sys
from faster_whisper import WhisperModel

audio_path, model_name, init_prompt = sys.argv[1], sys.argv[2], sys.argv[3]
threads = min(8, max(2, (os.cpu_count() or 4) // 2))
model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
segments, _ = model.transcribe(
    audio_path,
    beam_size=5,
    vad_filter=True,
    initial_prompt=init_prompt or None,
)
text = " ".join(seg.text.strip() for seg in segments if seg.text.strip()).strip()
print(json.dumps({"text": text}))
""".strip()


class VoiceTranscriptionError(RuntimeError):
    """Raised when voice transcription is unavailable or fails cleanly."""


@dataclass(frozen=True)
class InboundVoiceCandidate:
    file_id: str
    declared_size: int = 0
    duration_seconds: int = 0
    is_voice_note: bool = True


@dataclass(frozen=True)
class PendingVoiceNote:
    token: str
    bot_role: str
    msg_id: int
    transcript: str
    created_at: float


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        del newurl
        raise urllib.error.HTTPError(req.full_url, code, f"Redirects disallowed ({msg})", headers, fp)


def _is_safe_voice_api_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.username or parsed.password or not parsed.hostname:
        return False
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return True
    return scheme == "http" and parsed.hostname.lower() in ("127.0.0.1", "localhost", "::1")


def default_voice_venv_dir(env: Mapping[str, str] | None = None) -> Path:
    env_map = os.environ if env is None else env
    explicit = env_map.get("HERDR_VOICE_VENV", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_raw = env_map.get("HERDR_TELEGRAM_STATE_DIR", "").strip()
    state_dir = Path(state_raw).expanduser() if state_raw else (Path.home() / ".local" / "state" / "herdr-telegram")
    return state_dir / "voice-venv"


def find_local_whisper_python(env: Mapping[str, str] | None = None) -> str:
    venv_dir = default_voice_venv_dir(env)
    cand = venv_dir / "bin" / "python"
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    if env is None and importlib.util.find_spec("faster_whisper") is not None:
        return sys.executable
    return ""


def inspect_inbound_voice_message(
    msg: Mapping[str, Any],
    *,
    max_bytes: int = MAX_INBOUND_AUDIO_BYTES,
    max_duration_seconds: int = MAX_VOICE_DURATION_SECONDS,
) -> InboundVoiceCandidate | str | None:
    """Return InboundVoiceCandidate, a user-facing rejection message, or None when msg has no voice/audio."""
    voice = msg.get("voice")
    if isinstance(voice, dict) and str(voice.get("file_id") or "").strip():
        fsize = int(voice.get("file_size") or 0)
        dur = int(voice.get("duration") or 0)
        if fsize > max_bytes:
            return OVERSIZED_AUDIO_MESSAGE
        if dur > max_duration_seconds:
            return LONG_AUDIO_MESSAGE
        return InboundVoiceCandidate(
            file_id=str(voice["file_id"]).strip(),
            declared_size=fsize,
            duration_seconds=dur,
            is_voice_note=True,
        )

    audio = msg.get("audio")
    if isinstance(audio, dict) and str(audio.get("file_id") or "").strip():
        fsize = int(audio.get("file_size") or 0)
        dur = int(audio.get("duration") or 0)
        if fsize > max_bytes:
            return OVERSIZED_AUDIO_MESSAGE
        if dur > max_duration_seconds:
            return LONG_AUDIO_MESSAGE
        return InboundVoiceCandidate(
            file_id=str(audio["file_id"]).strip(),
            declared_size=fsize,
            duration_seconds=dur,
            is_voice_note=False,
        )

    doc = msg.get("document")
    if isinstance(doc, dict) and str(doc.get("file_id") or "").strip():
        mime = str(doc.get("mime_type") or "").strip().lower()
        fname = str(doc.get("file_name") or "").strip().lower()
        is_audio_doc = mime in _SUPPORTED_AUDIO_MIMES or (
            (mime.startswith("audio/") or mime in ("", "application/octet-stream"))
            and fname.endswith(_SUPPORTED_AUDIO_EXTS)
        )
        if is_audio_doc:
            fsize = int(doc.get("file_size") or 0)
            if fsize > max_bytes:
                return OVERSIZED_AUDIO_MESSAGE
            return InboundVoiceCandidate(
                file_id=str(doc["file_id"]).strip(),
                declared_size=fsize,
                duration_seconds=0,
                is_voice_note=False,
            )
        if mime.startswith("audio/"):
            return UNSUPPORTED_AUDIO_MESSAGE

    return None


def sniff_audio_bytes(data: bytes) -> tuple[str, str] | None:
    """Validate common audio container headers and return (extension, mime_type)."""
    if len(data) < 12:
        return None
    if data[:4] == b"OggS":
        return ".ogg", "audio/ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if data[:4] == b"fLaC":
        return ".flac", "audio/flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "audio/webm"
    if data[4:8] == b"ftyp" and not is_heic_bytes(data):
        return ".m4a", "audio/mp4"
    if data[:3] == b"ID3":
        return ".mp3", "audio/mpeg"
    # Raw MPEG ADTS/MP3 frame sync: 11 set bits, valid MPEG version (!= 01), valid layer (!= 00), bitrate != 1111
    b0, b1, b2 = data[0], data[1], data[2]
    if b0 == 0xFF and (b1 & 0xE0) == 0xE0 and ((b1 >> 3) & 0x03) != 0x01 and ((b1 >> 1) & 0x03) != 0x00:
        if ((b2 >> 4) & 0x0F) != 0x0F:
            return ".mp3", "audio/mpeg"
    return None


def resolve_voice_backend(
    provider: str = DEFAULT_VOICE_PROVIDER,
    model: str = "",
    *,
    api_key: str = "",
    api_url: str = "",
    local_python: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str, str, str]:
    """Resolve (provider, model, local_python, api_url, api_key) for speech-to-text."""
    env_map = os.environ if env is None else env
    prov = (provider or "auto").strip().lower() or "auto"
    mod = model.strip()

    if prov in DISABLED_VOICE_VALUES or (mod and mod.lower() in DISABLED_VOICE_VALUES):
        return "off", "", "", "", ""

    if prov == "faster-whisper":
        prov = "local"
    elif prov == "google":
        prov = "gemini-api"

    if prov == "auto" and ":" in mod:
        prefix, rest = mod.split(":", 1)
        cand = prefix.strip().lower()
        if cand == "faster-whisper":
            cand = "local"
        elif cand == "google":
            cand = "gemini-api"
        if cand in ("local", "openai", "groq", "gemini-api"):
            prov = cand
            mod = rest.strip()
            if mod.lower() in DISABLED_VOICE_VALUES:
                return "off", "", "", "", ""

    py_bin = local_python if local_python is not None else find_local_whisper_python(env_map)
    explicit_key = (api_key or env_map.get("HERDR_VOICE_API_KEY", "")).strip()
    explicit_url = (api_url or env_map.get("HERDR_VOICE_API_URL", "")).strip()

    if prov == "auto":
        if py_bin:
            prov = "local"
        elif explicit_key:
            url_low = explicit_url.lower()
            if "groq.com" in url_low:
                prov = "groq"
            elif "googleapis.com" in url_low:
                prov = "gemini-api"
            else:
                prov = "openai"
        elif env_map.get("OPENAI_API_KEY", "").strip():
            prov = "openai"
        elif env_map.get("GROQ_API_KEY", "").strip():
            prov = "groq"
        elif (env_map.get("GEMINI_API_KEY") or env_map.get("GOOGLE_API_KEY") or "").strip():
            prov = "gemini-api"
        else:
            return "unconfigured", "", "", "", ""

    if prov == "local":
        if not py_bin:
            return "unconfigured", mod or DEFAULT_LOCAL_VOICE_MODEL, "", "", ""
        return "local", (mod or DEFAULT_LOCAL_VOICE_MODEL), py_bin, "", ""

    if prov == "openai":
        resolved_key = explicit_key or env_map.get("OPENAI_API_KEY", "").strip()
        resolved_url = (explicit_url or VOICE_PROVIDER_API_URLS["openai"]).rstrip("/")
        if not resolved_key and not resolved_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return "unconfigured", mod or DEFAULT_OPENAI_VOICE_MODEL, "", resolved_url, ""
        return "openai", (mod or DEFAULT_OPENAI_VOICE_MODEL), "", resolved_url, resolved_key

    if prov == "groq":
        resolved_key = explicit_key or env_map.get("GROQ_API_KEY", "").strip()
        resolved_url = (explicit_url or VOICE_PROVIDER_API_URLS["groq"]).rstrip("/")
        if not resolved_key:
            return "unconfigured", mod or DEFAULT_GROQ_VOICE_MODEL, "", resolved_url, ""
        return "groq", (mod or DEFAULT_GROQ_VOICE_MODEL), "", resolved_url, resolved_key

    if prov == "gemini-api":
        resolved_key = (
            explicit_key or env_map.get("GEMINI_API_KEY", "").strip() or env_map.get("GOOGLE_API_KEY", "").strip()
        )
        resolved_url = (explicit_url or VOICE_PROVIDER_API_URLS["gemini-api"]).rstrip("/")
        if not resolved_key:
            return "unconfigured", mod or DEFAULT_GEMINI_VOICE_MODEL, "", resolved_url, ""
        return "gemini-api", (mod or DEFAULT_GEMINI_VOICE_MODEL), "", resolved_url, resolved_key

    return "unconfigured", "", "", "", ""


def build_vocabulary_prompt(workspace_labels: Sequence[str] = ()) -> str:
    """Build a short Whisper initial_prompt with live space names and common engineering vocabulary."""
    seen: set[str] = set()
    clean_spaces: list[str] = []
    for raw in workspace_labels:
        cleaned = " ".join(raw.split()).strip()
        if not cleaned or not re.search(r"[A-Za-z0-9]", cleaned):
            continue
        low = cleaned.lower()
        if low in seen:
            continue
        seen.add(low)
        clean_spaces.append(cleaned[:28])
        if len(clean_spaces) >= 12:
            break
    core_terms = "Herdr, git, PR, pytest, ruff, systemd, CLI, API, OAuth, 2FA, TypeScript, Python"
    if clean_spaces:
        return f"Spaces: {', '.join(clean_spaces)}. Terms: {core_terms}."[:220]
    return f"Terms: {core_terms}."


def _normalize_spoken_token(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower())
    return re.sub(r"_{2,}", "_", slug).strip("_")


def extract_spoken_space_prefix(
    transcript: str,
    *,
    allowed_tokens: Collection[str],
    resolve_fn: Callable[[str], str | None],
) -> tuple[str | None, str]:
    """If transcript opens with '<space>, <body>' or '<space>: <body>' matching a live space/handle, split it."""
    m = _SPOKEN_PREFIX_RE.match(transcript)
    if not m:
        return None, transcript
    head_raw = m.group("head").strip()
    body = m.group("body").strip()
    if not head_raw or not body or len(head_raw.split()) > 3:
        return None, transcript

    norm_allowed = {_normalize_spoken_token(tok) for tok in allowed_tokens if _normalize_spoken_token(tok)}
    head_norm = _normalize_spoken_token(head_raw)
    if not head_norm or head_norm not in norm_allowed:
        return None, transcript

    for cand in (head_raw, head_norm):
        resolved = resolve_fn(cand)
        if resolved:
            return resolved, body
    return None, transcript


class VoiceTranscriber:
    """Speech-to-text transcriber supporting local faster-whisper (on-demand, 0 MB idle RAM) and cloud APIs."""

    def __init__(
        self,
        *,
        provider: str = DEFAULT_VOICE_PROVIDER,
        model: str = "",
        api_key: str = "",
        api_url: str = "",
        local_python: str | None = None,
        timeout_seconds: float = 60.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        prov, mod, py_bin, url, key = resolve_voice_backend(
            provider=provider,
            model=model,
            api_key=api_key,
            api_url=api_url,
            local_python=local_python,
            env=env,
        )
        self.provider = prov
        self.model = mod
        self.local_python = py_bin
        self.api_url = url
        self._api_key = key
        self.timeout_seconds = max(5.0, timeout_seconds)

    @property
    def voice_label(self) -> str:
        if self.provider in ("off", "unconfigured"):
            return self.provider
        return f"{self.provider}:{self.model}" if self.model else self.provider

    def transcribe_bytes(
        self,
        data: bytes,
        *,
        workspace_labels: Sequence[str] = (),
    ) -> str:
        if self.provider == "off":
            raise VoiceTranscriptionError("Voice transcription is disabled in configuration (voice_provider=off).")
        if self.provider == "unconfigured":
            raise VoiceTranscriptionError(
                "Voice transcription is not set up yet. Run 'herdr-telegram setup-voice' on your machine "
                "(or configure HERDR_VOICE_API_KEY)."
            )
        if len(data) > MAX_INBOUND_AUDIO_BYTES:
            raise MediaValidationError(OVERSIZED_AUDIO_MESSAGE)

        sniffed = sniff_audio_bytes(data)
        if sniffed is None:
            raise MediaValidationError(UNSUPPORTED_AUDIO_MESSAGE)
        ext, mime_type = sniffed
        vocab_prompt = build_vocabulary_prompt(workspace_labels)

        if self.provider == "local":
            with tempfile.TemporaryDirectory(prefix="herdr-tg-voice-") as tmp_dir:
                os.chmod(tmp_dir, 0o700)
                audio_path = Path(tmp_dir) / f"note{ext}"
                fd = os.open(audio_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    view = memoryview(data)
                    written = 0
                    while written < len(view):
                        n = os.write(fd, view[written:])
                        if n <= 0:
                            raise OSError("Short write saving temporary voice clip")
                        written += n
                finally:
                    os.close(fd)
                return self._transcribe_local(audio_path, vocab_prompt)

        if self.provider in ("openai", "groq"):
            return self._transcribe_openai_multipart(data, f"voice{ext}", mime_type, vocab_prompt)

        if self.provider == "gemini-api":
            return self._transcribe_gemini(data, mime_type, vocab_prompt)

        raise VoiceTranscriptionError(f"Unsupported voice provider: {self.provider}")

    def _scrubbed_local_env(self, *, offline: bool) -> dict[str, str]:
        env: dict[str, str] = {"HF_HUB_DISABLE_TELEMETRY": "1"}
        for key in SAFE_VOICE_SUBPROCESS_ENV_KEYS:
            val = os.environ.get(key)
            if val:
                env[key] = val
        if offline:
            env["HF_HUB_OFFLINE"] = "1"
        return env

    def _transcribe_local(self, audio_path: Path, vocab_prompt: str) -> str:
        cmd = [self.local_python, "-c", _FASTER_WHISPER_SCRIPT, str(audio_path), self.model, vocab_prompt]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            env=self._scrubbed_local_env(offline=True),
        )
        if proc.returncode != 0:
            # Retry once online in case the requested HuggingFace CTranslate2 model is not cached locally yet
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=max(self.timeout_seconds, 120.0),
                check=False,
                env=self._scrubbed_local_env(offline=False),
            )
        if proc.returncode != 0:
            err_tail = (proc.stderr or proc.stdout or "faster-whisper exited with error").strip().splitlines()
            short_err = err_tail[-1][:180] if err_tail else "unknown error"
            raise VoiceTranscriptionError(f"Local Whisper failed: {short_err}")

        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            return ""
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise VoiceTranscriptionError("Local Whisper returned invalid JSON output") from exc
        return str(parsed.get("text") or "").strip()

    def _transcribe_openai_multipart(
        self,
        data: bytes,
        filename: str,
        mime_type: str,
        vocab_prompt: str,
    ) -> str:
        if not _is_safe_voice_api_url(self.api_url):
            raise VoiceTranscriptionError("Refusing insecure voice API URL (HTTPS required for remote hosts).")
        boundary = f"herdrvoice{secrets.token_hex(12)}"
        parts: list[bytes] = []

        def _add_field(name: str, val: str) -> None:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())

        _add_field("model", self.model)
        _add_field("response_format", "json")
        if vocab_prompt:
            _add_field("prompt", vocab_prompt)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n".encode()
        )
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "herdr-telegram/0.2",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            f"{self.api_url}/audio/transcriptions",
            data=body,
            headers=headers,
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=self.timeout_seconds) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.close()
            raise VoiceTranscriptionError(f"{self.provider} transcription HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise VoiceTranscriptionError(f"{self.provider} transcription failed ({type(exc).__name__})") from None
        return str(raw.get("text") or "").strip()

    def _transcribe_gemini(self, data: bytes, mime_type: str, vocab_prompt: str) -> str:
        if not _is_safe_voice_api_url(self.api_url):
            raise VoiceTranscriptionError("Refusing insecure Gemini voice API URL (HTTPS required).")
        safe_model = urllib.parse.quote(self.model, safe="-_.")
        endpoint = f"{self.api_url}/models/{safe_model}:generateContent"
        instruction = (
            "Transcribe this voice message verbatim into plain text. "
            "Output ONLY the spoken words with normal punctuation, or an empty string if silent. "
            f"Context vocabulary: {vocab_prompt}"
        )
        payload = json.dumps(
            {
                "contents": [
                    {
                        "parts": [
                            {"text": instruction},
                            {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": base64.b64encode(data).decode("ascii"),
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {"temperature": 0.0},
            }
        ).encode()
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
                "User-Agent": "herdr-telegram/0.2",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=self.timeout_seconds) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.close()
            raise VoiceTranscriptionError(f"gemini-api transcription HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise VoiceTranscriptionError(f"gemini-api transcription failed ({type(exc).__name__})") from None

        candidates = raw.get("candidates") or []
        if not candidates:
            return ""
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
        return " ".join(t.strip() for t in texts if t.strip()).strip()


def install_local_voice_venv(
    *,
    model: str = DEFAULT_LOCAL_VOICE_MODEL,
    venv_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Create isolated faster-whisper venv and pre-cache the CTranslate2 model for instant offline use."""
    target_venv = venv_dir if venv_dir is not None else default_voice_venv_dir(env)
    target_venv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    uv_bin = shutil.which("uv")
    py_bin = target_venv / "bin" / "python"

    if uv_bin:
        venv_proc = subprocess.run([uv_bin, "venv", "--clear", str(target_venv)], check=False)
        if venv_proc.returncode != 0:
            return False, f"uv venv failed with exit code {venv_proc.returncode}"
        pip_proc = subprocess.run(
            [uv_bin, "pip", "install", "--python", str(py_bin), "faster-whisper", "av<17"],
            check=False,
        )
        if pip_proc.returncode != 0:
            return False, f"uv pip install faster-whisper failed with exit code {pip_proc.returncode}"
    else:
        venv_proc = subprocess.run([sys.executable, "-m", "venv", "--clear", str(target_venv)], check=False)
        if venv_proc.returncode != 0:
            return False, f"python -m venv failed with exit code {venv_proc.returncode}"
        pip_proc = subprocess.run(
            [str(py_bin), "-m", "pip", "install", "--upgrade", "pip", "faster-whisper", "av<17"],
            check=False,
        )
        if pip_proc.returncode != 0:
            return False, f"pip install faster-whisper failed with exit code {pip_proc.returncode}"

    warm_env = dict(os.environ)
    warm_env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    warm_proc = subprocess.run(
        [
            str(py_bin),
            "-c",
            "import sys; from faster_whisper import WhisperModel; "
            'WhisperModel(sys.argv[1], device="cpu", compute_type="int8")',
            model,
        ],
        env=warm_env,
        check=False,
    )
    if warm_proc.returncode != 0:
        return False, f"Installed faster-whisper, but failed downloading model {model!r}"
    return True, f"Ready: local faster-whisper ({model}) installed in {target_venv}"
