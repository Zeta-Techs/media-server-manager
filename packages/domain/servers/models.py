from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ServerDraft:
    name: str
    address: str
    token: str
    enabled: bool = True
    pinyin_mode: str = "first_letter"
    skip_libraries: str = ""

    def validate(self) -> "ServerDraft":
        name = self.name.strip()
        address = self.address.strip().rstrip("/")
        if not name:
            raise ValueError("server name is required")
        parsed = urlparse(address)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("server address must be a valid http or https URL")
        if not self.token.strip():
            raise ValueError("server token is required")
        if self.pinyin_mode not in {"first_letter", "full_spell"}:
            raise ValueError("invalid pinyin mode")
        return ServerDraft(name, address, self.token.strip(), self.enabled, self.pinyin_mode, self.skip_libraries)
