from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, BinaryIO

import httpx

from apps.integrations.whatsapp.client import WhatsAppApiError
from apps.integrations.whatsapp.config import access_token_from_env, api_version_from_env

logger = logging.getLogger(__name__)

# HTTP statuses where retrying cannot help (media expired / gone on Meta's side).
_PERMANENT_STATUSES = frozenset({404, 410})


class WhatsAppMediaError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        if retryable is None:
            retryable = status_code not in _PERMANENT_STATUSES
        self.retryable = retryable


class WhatsAppMediaTooLarge(WhatsAppMediaError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True)
class MediaMetadata:
    download_url: str
    mime_type: str
    sha256: str
    file_size: int | None


@dataclass(frozen=True)
class MediaDownloadResult:
    mime_type: str
    size: int
    sha256: str


def _resolve_media_metadata(
    *,
    media_id: str,
    access_token: str | None,
    api_version: str | None,
) -> tuple[MediaMetadata, dict[str, str]]:
    token = (access_token or access_token_from_env()).strip()
    if not token:
        raise WhatsAppMediaError("WHATSAPP_ACCESS_TOKEN missing")
    if not media_id:
        raise WhatsAppMediaError("media_id missing", retryable=False)

    version = (api_version or api_version_from_env()).strip() or "v23.0"
    meta_url = f"https://graph.facebook.com/{version}/{media_id.strip()}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        meta_response = httpx.get(meta_url, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise WhatsAppMediaError(f"media metadata HTTP error: {exc}") from exc

    if meta_response.status_code >= 400:
        raise WhatsAppMediaError(
            f"media metadata error {meta_response.status_code}: {meta_response.text[:500]}",
            status_code=meta_response.status_code,
        )

    meta = meta_response.json()
    if not isinstance(meta, dict):
        raise WhatsAppMediaError("media metadata is not JSON object")

    download_url = str(meta.get("url") or "").strip()
    mime_type = str(meta.get("mime_type") or "image/jpeg").strip()
    if not download_url:
        raise WhatsAppMediaError("media metadata missing url")

    try:
        file_size = int(meta.get("file_size")) if meta.get("file_size") not in (None, "") else None
    except (TypeError, ValueError):
        file_size = None
    return (
        MediaMetadata(
            download_url=download_url,
            mime_type=mime_type,
            sha256=str(meta.get("sha256") or "").strip().lower(),
            file_size=file_size,
        ),
        headers,
    )


def fetch_whatsapp_media(
    *,
    media_id: str,
    access_token: str | None = None,
    api_version: str | None = None,
) -> tuple[bytes, str]:
    """Download WhatsApp media binary via Meta Graph API (in memory)."""
    metadata, headers = _resolve_media_metadata(
        media_id=media_id, access_token=access_token, api_version=api_version
    )

    try:
        file_response = httpx.get(
            metadata.download_url,
            headers=headers,
            timeout=60.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise WhatsAppMediaError(f"media download HTTP error: {exc}") from exc

    if file_response.status_code >= 400:
        raise WhatsAppMediaError(
            f"media download error {file_response.status_code}: {file_response.text[:200]}",
            status_code=file_response.status_code,
        )

    return file_response.content, metadata.mime_type


def fetch_whatsapp_media_to_file(
    *,
    media_id: str,
    dest: BinaryIO,
    max_bytes: int,
    access_token: str | None = None,
    api_version: str | None = None,
) -> MediaDownloadResult:
    """Stream media into ``dest`` with a size cap; verify Meta's sha256/file_size.

    Same Graph API steps as :func:`fetch_whatsapp_media`, but never holds the whole
    file in memory (video/audio/large documents).
    """
    metadata, headers = _resolve_media_metadata(
        media_id=media_id, access_token=access_token, api_version=api_version
    )
    if max_bytes and metadata.file_size is not None and metadata.file_size > max_bytes:
        raise WhatsAppMediaTooLarge(
            f"media size {metadata.file_size} exceeds limit {max_bytes}"
        )

    digest = hashlib.sha256()
    size = 0
    try:
        with httpx.stream(
            "GET",
            metadata.download_url,
            headers=headers,
            timeout=60.0,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                raise WhatsAppMediaError(
                    f"media download error {response.status_code}",
                    status_code=response.status_code,
                )
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if max_bytes and size > max_bytes:
                    raise WhatsAppMediaTooLarge(f"media exceeds limit {max_bytes}")
                digest.update(chunk)
                dest.write(chunk)
    except httpx.HTTPError as exc:
        raise WhatsAppMediaError(f"media download HTTP error: {exc}") from exc

    sha256 = digest.hexdigest()
    if metadata.file_size is not None and metadata.file_size != size:
        raise WhatsAppMediaError(
            f"media size mismatch: expected {metadata.file_size}, got {size}"
        )
    if metadata.sha256 and metadata.sha256 != sha256:
        raise WhatsAppMediaError("media sha256 mismatch with Meta metadata")
    return MediaDownloadResult(mime_type=metadata.mime_type, size=size, sha256=sha256)


def extract_media_from_message(raw_message: dict[str, Any]) -> tuple[str, str, str]:
    """Return (media_id, mime_type, caption) from WhatsApp inbound message payload."""
    message_type = str(raw_message.get("type") or "").strip().lower()
    if message_type == "image":
        payload = raw_message.get("image") or {}
        return (
            str(payload.get("id") or "").strip(),
            str(payload.get("mime_type") or "image/jpeg").strip(),
            str(payload.get("caption") or "").strip(),
        )
    if message_type == "document":
        payload = raw_message.get("document") or {}
        return (
            str(payload.get("id") or "").strip(),
            str(payload.get("mime_type") or "application/octet-stream").strip(),
            str(payload.get("caption") or "").strip(),
        )
    return "", "", ""


@dataclass(frozen=True)
class MediaDescriptor:
    kind: str  # image | document | video | audio | voice | sticker
    media_id: str
    mime_type: str
    caption: str
    filename: str
    sha256: str


_ARCHIVABLE_TYPES = ("image", "document", "video", "audio", "sticker")


def extract_media_descriptor(raw_message: dict[str, Any]) -> MediaDescriptor | None:
    """Describe any archivable media in an inbound message or Business App echo.

    Separate from :func:`extract_media_from_message`, which intake code relies on.
    """
    message_type = str(raw_message.get("type") or "").strip().lower()
    if message_type not in _ARCHIVABLE_TYPES:
        return None
    payload = raw_message.get(message_type)
    if not isinstance(payload, dict):
        return None
    media_id = str(payload.get("id") or "").strip()
    if not media_id:
        return None
    kind = message_type
    if message_type == "audio" and payload.get("voice"):
        kind = "voice"
    return MediaDescriptor(
        kind=kind,
        media_id=media_id,
        mime_type=str(payload.get("mime_type") or "").strip(),
        caption=str(payload.get("caption") or "").strip(),
        filename=str(payload.get("filename") or "").strip(),
        sha256=str(payload.get("sha256") or "").strip().lower(),
    )
