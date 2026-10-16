"""Safe consumer-facing errors. Never include remote bodies or credential URLs."""


class BookError(Exception):
    def __init__(self, code: str, message: str, *, recoverable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "recoverable": self.recoverable}
