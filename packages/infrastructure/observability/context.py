from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            object.__setattr__(self, "request_id", f"req_{uuid4().hex}")
