"""HTTP bearer authentication."""

import hmac

from fastapi import Header, HTTPException

from oce.shared.config import get_settings


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
    )


def _extract_bearer(authorization: str | None) -> str:
    """The key in ``Authorization: Bearer <key>``; missing or malformed is 401."""
    if authorization is None:
        raise _unauthorized("You didn't provide an API key.")
    if not authorization.startswith("Bearer "):
        raise _unauthorized("Invalid API key format. Expected 'Bearer <key>'")
    return authorization.removeprefix("Bearer ")


async def verify_api_key(authorization: str | None = Header(default=None)) -> str:
    """Data-plane authentication against ``API_KEY``."""
    api_key = _extract_bearer(authorization)
    if not hmac.compare_digest(api_key, get_settings().api_key):
        raise _unauthorized("Invalid API key provided")
    return api_key


async def verify_admin_key(authorization: str | None = Header(default=None)) -> str:
    """Admin authentication.

    Without ``ADMIN_API_KEY`` the admin routes accept ``API_KEY`` so personal
    mode works with no extra configuration; once it is set, only the admin
    key is accepted.
    """
    settings = get_settings()
    expected = settings.admin_api_key or settings.api_key
    key = _extract_bearer(authorization)
    if not hmac.compare_digest(key, expected):
        raise _unauthorized("Invalid admin key provided")
    return key
