"""Only the API capability providers need; file transfer stays transport-specific."""
from typing import Protocol


class JsonTransport(Protocol):
    def json(self, method: str, url: str, **kwargs) -> dict: ...
