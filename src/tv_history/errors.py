from __future__ import annotations


def error_response(
    code: str,
    message: str,
    retryable: bool = False,
    **details,
) -> dict:
    error = {"code": code, "message": message, "retryable": retryable}
    error.update(details)
    return {"error": error}


def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "rate limit",
            "too many requests",
            "http 429",
            "status 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "status 500",
            "status 502",
            "status 503",
            "status 504",
        )
    )
