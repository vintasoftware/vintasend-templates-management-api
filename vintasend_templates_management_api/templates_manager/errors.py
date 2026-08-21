"""The API's error type and its mapping onto the contract's status codes.

Rendering an ``ApiError`` into the response envelope is deliberately not done here --
``api.py`` owns that, so there is exactly one place that decides what an error looks like
on the wire, and validation and auth failures go through the same code as raised errors.
"""

from pydantic import JsonValue

from .contract import ApiErrorCode


# Several codes deliberately share a status. INVALID_STATUS_TRANSITION is a 409 that says
# specifically *why* a status change was refused, which a UI branches on to explain the
# lifecycle rather than showing a bare "conflict". TEMPLATE_COMPOSITION_ERROR is a 409 for
# the same reason PREVIEW_UNAVAILABLE is: the request was well formed and the stored
# template is what cannot be assembled -- a missing base, a loop, a malformed tag -- which
# is a fact about the template the caller asked about, and the message says which.
STATUS_BY_CODE: dict[str, int] = {
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "INVALID_STATUS_TRANSITION": 409,
    "PREVIEW_UNAVAILABLE": 409,
    "TEMPLATE_COMPOSITION_ERROR": 409,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    """Error carrying an API error code, turned into the documented status code and
    error envelope by the handler registered in ``api.py``."""

    def __init__(self, code: ApiErrorCode, message: str, details: JsonValue | None = None) -> None:
        super().__init__(message)
        self.code: ApiErrorCode = code
        self.message = message
        self.status = STATUS_BY_CODE[code]
        self.details = details

    @classmethod
    def bad_request(cls, message: str, details: JsonValue | None = None) -> "ApiError":
        return cls("BAD_REQUEST", message, details)

    @classmethod
    def not_found(cls, message: str) -> "ApiError":
        return cls("NOT_FOUND", message)

    @classmethod
    def conflict(cls, message: str) -> "ApiError":
        return cls("CONFLICT", message)

    @classmethod
    def invalid_transition(cls, message: str) -> "ApiError":
        return cls("INVALID_STATUS_TRANSITION", message)

    @classmethod
    def preview_unavailable(cls, message: str) -> "ApiError":
        return cls("PREVIEW_UNAVAILABLE", message)

    @classmethod
    def composition_error(cls, message: str) -> "ApiError":
        return cls("TEMPLATE_COMPOSITION_ERROR", message)
