"""Tripletex API error types and response parsing."""
from typing import Any


class TripletexError(Exception):
    """Raised when a Tripletex API call returns a non-2xx status."""

    def __init__(
        self,
        status_code: int,
        message: str,
        validation_messages: list[dict] | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.validation_messages = validation_messages or []
        super().__init__(f"HTTP {status_code}: {message}")

    def to_agent_message(self) -> str:
        """
        Format the error so Gemini can read it and self-correct in one retry.
        Returned as the tool result when a call fails.
        """
        parts = [f"Error {self.status_code}: {self.message}"]
        if self.validation_messages:
            parts.append("Validation errors:")
            for vm in self.validation_messages:
                field = vm.get("field", "unknown")
                msg = vm.get("message", "")
                parts.append(f"  - {field}: {msg}")
        return "\n".join(parts)


def parse_error_response(status_code: int, body: Any) -> TripletexError:
    if isinstance(body, dict):
        message = (
            body.get("message")
            or body.get("developerMessage")
            or str(body)
        )
        validation_messages = body.get("validationMessages") or []
    else:
        message = str(body)
        validation_messages = []
    return TripletexError(status_code, message, validation_messages)
