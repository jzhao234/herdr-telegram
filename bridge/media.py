"""Safe inbound/outbound Telegram image sniffing, workspace media storage, and multipart encoding."""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_INBOUND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_IMAGE_BYTES = 20 * 1024 * 1024
TELEGRAM_SEND_PHOTO_MAX_BYTES = 10 * 1024 * 1024
TELEGRAM_SEND_PHOTO_MAX_DIM_SUM = 10000
TELEGRAM_SEND_PHOTO_MAX_ASPECT = 20.0
TELEGRAM_CAPTION_MAX_CHARS = 1024

DEFAULT_MEDIA_MAX_AGE_SECONDS = 7 * 86400
DEFAULT_MEDIA_MAX_FILES = 50
DEFAULT_MEDIA_MAX_TOTAL_BYTES = 200 * 1024 * 1024

HEIC_HINT_MESSAGE = (
    "⚠️ iPhone HEIC files are not supported directly by coding agents. "
    "Resend as a standard Telegram Photo (not 'Send as File'), or export as PNG or JPEG."
)
UNSUPPORTED_IMAGE_MESSAGE = "⚠️ Unsupported image format. Supported formats: JPEG, PNG, WebP, and GIF."
OVERSIZED_IMAGE_MESSAGE = "⚠️ Image exceeds Telegram's 20 MB bot download limit."

_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"})
_SUPPORTED_DOC_MIMES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"})
_SUPPORTED_DOC_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_HEIC_EXTS = (".heic", ".heif")
_OTHER_IMAGE_EXTS = (".svg", ".svgz", ".bmp", ".tif", ".tiff", ".avif", ".ico")


class MediaValidationError(ValueError):
    """Raised when an inbound or outbound media payload fails validation."""


@dataclass(frozen=True)
class ImageMetadata:
    fmt: str
    ext: str
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True)
class InboundImageCandidate:
    file_id: str
    declared_size: int = 0


@dataclass(frozen=True)
class PreparedOutboundMedia:
    data: bytes
    filename: str
    metadata: ImageMetadata

    @property
    def telegram_method(self) -> str:
        """Choose sendPhoto for standard JPEG/PNG within Telegram's photo limits, else sendDocument."""
        if self.metadata.fmt not in ("jpeg", "png"):
            return "sendDocument"
        if len(self.data) > TELEGRAM_SEND_PHOTO_MAX_BYTES:
            return "sendDocument"
        w, h = self.metadata.width, self.metadata.height
        if w > 0 and h > 0:
            if (w + h) > TELEGRAM_SEND_PHOTO_MAX_DIM_SUM:
                return "sendDocument"
            if (max(w, h) / max(1, min(w, h))) > TELEGRAM_SEND_PHOTO_MAX_ASPECT:
                return "sendDocument"
        return "sendPhoto"

    @property
    def telegram_field(self) -> str:
        return "photo" if self.telegram_method == "sendPhoto" else "document"


def is_heic_bytes(data: bytes) -> bool:
    """Detect ISO BMFF HEIC/HEIF headers."""
    if len(data) < 12 or data[4:8] != b"ftyp":
        return False
    major = data[8:12]
    if major in _HEIC_BRANDS:
        return True
    box_len = struct.unpack(">I", data[:4])[0]
    end = min(len(data), max(16, min(box_len, 128)))
    for offset in range(16, end - 3, 4):
        if data[offset : offset + 4] in _HEIC_BRANDS:
            return True
    return False


def _parse_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    idx = 2
    n = len(data)
    sof_markers = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
    while idx < n - 1:
        if data[idx] != 0xFF:
            idx += 1
            continue
        while idx < n and data[idx] == 0xFF:
            idx += 1
        if idx >= n:
            break
        marker = data[idx]
        idx += 1
        if marker in (0x00, 0x01) or (0xD0 <= marker <= 0xD9):
            continue
        if idx + 2 > n:
            break
        seg_len = struct.unpack(">H", data[idx : idx + 2])[0]
        if seg_len < 2 or idx + seg_len > n:
            break
        if marker in sof_markers and seg_len >= 7:
            height, width = struct.unpack(">HH", data[idx + 3 : idx + 7])
            return int(width), int(height)
        idx += seg_len
    return 0, 0


def _parse_webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        raw_w, raw_h = struct.unpack("<HH", data[26:30])
        return raw_w & 0x3FFF, raw_h & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return 0, 0


def sniff_image_bytes(data: bytes) -> ImageMetadata | None:
    """Validate image magic bytes and extract dimensions for JPEG, PNG, GIF, and WebP."""
    if len(data) < 10:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        if width > 0 and height > 0:
            return ImageMetadata(fmt="png", ext=".png", mime_type="image/png", width=int(width), height=int(height))
        return None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        if width > 0 and height > 0:
            return ImageMetadata(fmt="gif", ext=".gif", mime_type="image/gif", width=int(width), height=int(height))
        return None
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _parse_webp_dimensions(data)
        return ImageMetadata(fmt="webp", ext=".webp", mime_type="image/webp", width=width, height=height)
    if data[:3] == b"\xff\xd8\xff":
        width, height = _parse_jpeg_dimensions(data)
        return ImageMetadata(fmt="jpeg", ext=".jpg", mime_type="image/jpeg", width=width, height=height)
    return None


def inspect_inbound_image_message(
    msg: Mapping[str, Any],
    *,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
) -> InboundImageCandidate | str | None:
    """Return InboundImageCandidate, a user-facing rejection string, or None when msg has no image."""
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        valid_items = [p for p in photos if isinstance(p, dict) and str(p.get("file_id") or "").strip()]
        if not valid_items:
            return None
        under_cap = [p for p in valid_items if int(p.get("file_size") or 0) <= max_bytes]
        if not under_cap:
            return OVERSIZED_IMAGE_MESSAGE
        chosen = under_cap[-1]
        return InboundImageCandidate(
            file_id=str(chosen["file_id"]).strip(),
            declared_size=int(chosen.get("file_size") or 0),
        )

    doc = msg.get("document")
    if isinstance(doc, dict) and str(doc.get("file_id") or "").strip():
        mime = str(doc.get("mime_type") or "").strip().lower()
        fname = str(doc.get("file_name") or "").strip().lower()
        if mime in ("image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence") or fname.endswith(
            _HEIC_EXTS
        ):
            return HEIC_HINT_MESSAGE
        is_supported_doc = mime in _SUPPORTED_DOC_MIMES or (
            mime in ("", "application/octet-stream") and fname.endswith(_SUPPORTED_DOC_EXTS)
        )
        if is_supported_doc:
            fsize = int(doc.get("file_size") or 0)
            if fsize > max_bytes:
                return OVERSIZED_IMAGE_MESSAGE
            return InboundImageCandidate(file_id=str(doc["file_id"]).strip(), declared_size=fsize)
        if mime.startswith("image/") or fname.endswith(_OTHER_IMAGE_EXTS):
            return UNSUPPORTED_IMAGE_MESSAGE

    return None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        n = os.write(fd, view[written:])
        if n <= 0:
            raise OSError("Short write while saving file")
        written += n


def _ensure_safe_dir_no_symlink(path: Path, mode: int = 0o700, *, enforce_mode: bool = True) -> bool:
    """Create or verify a real directory without following symlinks on the target component."""
    created = False
    try:
        lst = os.lstat(path)
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
            return False
    except FileNotFoundError:
        try:
            os.mkdir(path, mode)
            created = True
            lst = os.lstat(path)
        except OSError:
            return False
    except OSError:
        return False

    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
        return False

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, open_flags)
    except OSError:
        return False
    try:
        fst = os.fstat(fd)
        if not stat.S_ISDIR(fst.st_mode) or (fst.st_dev, fst.st_ino) != (lst.st_dev, lst.st_ino):
            return False
        if (enforce_mode or created) and hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
        return True
    finally:
        os.close(fd)


def _ensure_local_gitignore(base_dir: Path) -> bool:
    """Write .herdr-telegram/.gitignore with '*' so Git ignores saved images in worktrees and subfolders."""
    gi_path = base_dir / ".gitignore"
    try:
        lst = os.lstat(gi_path)
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            return False
        existing = gi_path.read_text(encoding="utf-8", errors="replace")
        if "*" in {ln.strip() for ln in existing.splitlines()}:
            return True
    except FileNotFoundError:
        pass
    except OSError:
        return False

    tmp_gi = base_dir / f".gitignore.tmp_{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp_gi, flags, 0o600)
        try:
            _write_all(fd, b"# Automatically created by herdr-telegram\n*\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_gi, gi_path)
        return True
    except OSError:
        tmp_gi.unlink(missing_ok=True)
        return False


def ensure_git_info_exclude(workspace_dir: Path, pattern: str = ".herdr-telegram/") -> None:
    """Add .herdr-telegram/ to the repository's common .git/info/exclude (including worktrees and subfolders)."""
    try:
        cur = workspace_dir.resolve()
    except OSError:
        return
    git_dir: Path | None = None
    for candidate in (cur, *cur.parents):
        dot_git = candidate / ".git"
        try:
            if dot_git.is_symlink():
                continue
            if dot_git.is_dir():
                git_dir = dot_git
                break
            if dot_git.is_file():
                lines_raw = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
                first = lines_raw[0].strip() if lines_raw else ""
                if first.lower().startswith("gitdir:"):
                    raw_target = first[7:].strip()
                    resolved_git = (candidate / raw_target).resolve()
                    if resolved_git.is_dir():
                        commondir_file = resolved_git / "commondir"
                        if commondir_file.is_file() and not commondir_file.is_symlink():
                            cd_lines = commondir_file.read_text(encoding="utf-8", errors="replace").splitlines()
                            if cd_lines and cd_lines[0].strip():
                                common_resolved = (resolved_git / cd_lines[0].strip()).resolve()
                                if common_resolved.is_dir():
                                    git_dir = common_resolved
                                    break
                        git_dir = resolved_git
                        break
        except OSError:
            continue
    if git_dir is None:
        return
    try:
        info_dir = git_dir / "info"
        if not _ensure_safe_dir_no_symlink(info_dir, mode=0o755, enforce_mode=False):
            return
        exclude_file = info_dir / "exclude"
        if exclude_file.is_symlink():
            return
        existing = exclude_file.read_text(encoding="utf-8", errors="replace") if exclude_file.exists() else ""
        lines = {ln.strip() for ln in existing.splitlines()}
        unanchored = pattern.lstrip("/")
        if unanchored in lines:
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{unanchored}\n")
    except OSError:
        return


def prune_media_dir(
    media_dir: Path,
    *,
    keep_paths: Sequence[Path] = (),
    max_age_seconds: float = DEFAULT_MEDIA_MAX_AGE_SECONDS,
    max_files: int = DEFAULT_MEDIA_MAX_FILES,
    max_total_bytes: int = DEFAULT_MEDIA_MAX_TOTAL_BYTES,
    now: float | None = None,
) -> None:
    """Prune stale or excess tg_* image files by age, count, and total byte budget."""
    if not media_dir.is_dir():
        return
    protected = {p.resolve() for p in keep_paths}
    curr_ts = now if now is not None else time.time()
    entries: list[tuple[float, int, Path]] = []
    for entry in media_dir.iterdir():
        if not entry.name.startswith("tg_") or entry.is_symlink():
            continue
        try:
            resolved = entry.resolve()
            if resolved in protected:
                continue
            st = entry.stat()
            if not stat.S_ISREG(st.st_mode):
                continue
            if max_age_seconds > 0 and (curr_ts - st.st_mtime) > max_age_seconds:
                entry.unlink(missing_ok=True)
                continue
            entries.append((st.st_mtime, st.st_size, entry))
        except OSError:
            continue

    entries.sort(key=lambda item: item[0], reverse=True)
    kept_bytes = 0
    for idx, (_, size, path) in enumerate(entries):
        if idx >= max_files or (kept_bytes + size) > max_total_bytes:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            kept_bytes += size


def resolve_media_dir(workspace_cwd: Path | str | None, fallback_state_dir: Path) -> Path:
    """Prefer <workspace_cwd>/.herdr-telegram/media when safe (no symlinks), else fallback state dir."""
    if workspace_cwd:
        raw_path = Path(workspace_cwd)
        try:
            if raw_path.is_dir():
                resolved_ws = raw_path.resolve()
                base = resolved_ws / ".herdr-telegram"
                media = base / "media"
                if (
                    _ensure_safe_dir_no_symlink(base, mode=0o700)
                    and _ensure_local_gitignore(base)
                    and _ensure_safe_dir_no_symlink(media, mode=0o700)
                ):
                    ensure_git_info_exclude(resolved_ws)
                    return media
        except OSError:
            pass
    fallback_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fallback_media = fallback_state_dir / "media"
    if not _ensure_safe_dir_no_symlink(fallback_media, mode=0o700):
        raise MediaValidationError(f"State media directory '{fallback_media}' is not a safe directory.")
    return fallback_media


def save_inbound_image(
    data: bytes,
    *,
    workspace_cwd: Path | str | None,
    fallback_state_dir: Path,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
) -> Path:
    """Validate magic bytes and write an inbound Telegram image atomically with 0600 permissions."""
    if not data:
        raise MediaValidationError("⚠️ Downloaded Telegram image was empty.")
    if len(data) > max_bytes:
        raise MediaValidationError(OVERSIZED_IMAGE_MESSAGE)
    if is_heic_bytes(data):
        raise MediaValidationError(HEIC_HINT_MESSAGE)
    meta = sniff_image_bytes(data)
    if meta is None:
        raise MediaValidationError(UNSUPPORTED_IMAGE_MESSAGE)

    media_dir = resolve_media_dir(workspace_cwd, fallback_state_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_path = media_dir / f"tg_{stamp}_{secrets.token_hex(4)}{meta.ext}"
    tmp_path = media_dir / f".tmp_{stamp}_{secrets.token_hex(6)}{meta.ext}"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp_path, final_path)
    prune_media_dir(media_dir, keep_paths=(final_path,))
    return final_path


def format_image_prompt(caption_body: str, saved_paths: Sequence[Path]) -> str:
    """Format the prompt delivered to a Herdr agent referencing one or more saved local image paths."""
    body = caption_body.strip()
    if len(saved_paths) == 1:
        path_str = str(saved_paths[0])
        if body:
            return (
                f"{body}\n\n[Attached Telegram image saved locally at {path_str} - read/view this image file directly.]"
            )
        return f"Please inspect the attached Telegram image saved locally at {path_str}."
    joined = "\n".join(f"- {p}" for p in saved_paths)
    if body:
        return f"{body}\n\n[Attached Telegram images saved locally:\n{joined}\nRead/view these image files directly.]"
    return f"Please inspect the attached Telegram images saved locally:\n{joined}"


def read_outbound_image(
    path: Path | str,
    *,
    max_bytes: int = MAX_OUTBOUND_IMAGE_BYTES,
) -> PreparedOutboundMedia:
    """Open an outbound image once with O_NOFOLLOW, verify regular file + magic bytes, and prepare upload."""
    target = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MediaValidationError(f"Refusing to send symlink '{target}'; pass the real image file.") from exc
        if exc.errno == errno.ENOENT:
            raise MediaValidationError(f"Image file not found: {target}") from exc
        raise MediaValidationError(f"Cannot open image file '{target}': {exc.strerror or exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MediaValidationError(f"Refusing to send non-regular file '{target}'.")
        if st.st_size <= 0:
            raise MediaValidationError(f"Image file '{target}' is empty.")
        if st.st_size > max_bytes:
            raise MediaValidationError(f"Image file '{target}' exceeds the 20 MB upload limit.")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise MediaValidationError(f"Image file '{target}' exceeds the 20 MB upload limit.")
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)

    if not data:
        raise MediaValidationError(f"Image file '{target}' is empty.")
    if is_heic_bytes(data):
        raise MediaValidationError(f"HEIC image '{target}' is not supported; convert to PNG or JPEG first.")
    meta = sniff_image_bytes(data)
    if meta is None:
        raise MediaValidationError(f"File '{target}' is not a valid JPEG, PNG, WebP, or GIF image.")

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", target.stem).strip("._") or "image"
    filename = f"{stem[:64]}{meta.ext}"
    return PreparedOutboundMedia(data=data, filename=filename, metadata=meta)


def encode_multipart_formdata(
    fields: Mapping[str, str],
    *,
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    """Encode stdlib-only multipart/form-data payload and Content-Type header."""
    boundary = f"herdrtg_{secrets.token_hex(16)}"
    safe_filename = re.sub(r'["\\\r\n]+', "_", filename)
    parts: list[bytes] = []
    for key, val in fields.items():
        safe_key = re.sub(r'["\\\r\n]+', "_", str(key))
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        parts.append(f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'.encode("ascii"))
        parts.append(str(val).encode("utf-8"))
        parts.append(b"\r\n")

    safe_file_field = re.sub(r'["\\\r\n]+', "_", file_field)
    parts.append(f"--{boundary}\r\n".encode("ascii"))
    parts.append(f'Content-Disposition: form-data; name="{safe_file_field}"; filename="{safe_filename}"\r\n'.encode())
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
