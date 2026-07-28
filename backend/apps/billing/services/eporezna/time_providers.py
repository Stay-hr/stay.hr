from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    def now(self) -> datetime: ...


class UuidProvider(Protocol):
    def new(self) -> UUID: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemUuidProvider:
    def new(self) -> UUID:
        return uuid.uuid4()
