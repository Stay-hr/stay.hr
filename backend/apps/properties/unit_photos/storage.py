"""MediaStorage abstraction for unit listing photos (ADR 0015)."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from django.conf import settings


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def storage_key_for_checksum(checksum: str, suffix: str = ".jpg") -> str:
    """Content-addressed key — same bytes may share a blob across UnitPhotos."""
    return f"unit_photos/{checksum[:2]}/{checksum}{suffix}"


class MediaStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """Persist bytes; return storage_ref (usually same as key)."""

    @abstractmethod
    def open(self, ref: str) -> bytes:
        """Read original bytes by storage_ref."""

    @abstractmethod
    def public_url(self, ref: str) -> str:
        """URL for provider use — must obey immutable URL policy."""

    @abstractmethod
    def exists(self, ref: str) -> bool:
        ...


class LocalStorage(MediaStorage):
    """Filesystem under MEDIA_ROOT (dev / current bind-mount)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.MEDIA_ROOT)

    def _path(self, ref: str) -> Path:
        # Prevent path escape
        path = (self.root / ref).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"Invalid storage ref: {ref}")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return key

    def open(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()

    def public_url(self, ref: str) -> str:
        base = settings.MEDIA_URL.rstrip("/")
        return f"{base}/{ref.lstrip('/')}"

    def exists(self, ref: str) -> bool:
        return self._path(ref).is_file()


def default_media_storage() -> MediaStorage:
    return LocalStorage()
