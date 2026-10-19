"""Bearer-token check for the deployed ``/mcp`` route.

Two kinds of bearer token are accepted, decided by
``cipherweave_verify.BearerAuth`` (the same code the other Weaves run):

* a CipherWeave access token — an ES256 JWT with ``aud`` = ``toolweave``,
  verified in-process against CipherWeave's JWKS (``CIPHERWEAVE_JWKS_URL``);
* while ``ACCEPT_LEGACY_TOKEN`` is true (the default, for the migration), the
  static token described below. It is read only when the presented token is
  not a valid CipherWeave token, so with the flag off it is never read.

The static token value is never in source or in the template. ``MCP_AUTH_TOKEN_SECRET_NAME``
is the name of an AWS Secrets Manager secret. The Lambda reads ``SecretString``
at request time and caches a successful read in the execution environment.

Comparison hashes both sides and then uses ``hmac.compare_digest``, so a
length mismatch is not a fast path and the compare does not raise. A missing
name, a failed lookup, an empty secret, a missing header, or a mismatch all
fail closed the same way: HTTP 401 with a JSON-RPC error. The body does not
say which of those happened.

Routes other than ``/mcp`` are not checked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from cipherweave_verify import BearerAuth

logger = logging.getLogger(__name__)

TOKEN_AUDIENCE = "toolweave"
SECRET_NAME_ENV = "MCP_AUTH_TOKEN_SECRET_NAME"
UNAUTHORIZED_CODE = -32001
_CACHE_TTL_SECONDS = 60
_MAX_PRESENTED_LEN = 4096
_JSON_KEYS = ("token", "bearerToken", "bearer_token", "value")
_BEARER_RE = re.compile(r"(?i)bearer\s+(\S+)")

# (secret name, token, monotonic expiry)
_cache: tuple[str, str, float] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def tokens_equal(presented: str, expected: str) -> bool:
    """Constant-time equality for two strings that may differ in length."""
    presented_digest = hashlib.sha256(presented.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(presented_digest, expected_digest)


def credentials_ok(presented: str | None, expected: str | None) -> bool:
    """True only when both sides are non-empty and equal.

    The digest compare always runs, including when one side is missing, so
    the failure path does not return before the compare.
    """
    presented_s = presented if isinstance(presented, str) else ""
    expected_s = expected if isinstance(expected, str) else ""
    match = tokens_equal(presented_s, expected_s)
    if not presented_s or not expected_s:
        return False
    return match


def token_from_secret_string(raw: str | None) -> str | None:
    """Accept a raw token or a small JSON object that carries one.

    ``aws secretsmanager create-secret --secret-string <random>`` stores the
    raw token. A JSON object may instead hold it under ``token``,
    ``bearerToken``, ``bearer_token``, or ``value``.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text[0] != "{":
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in _JSON_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    return None


def _normalize_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            value = value[0] if value else ""
        if not isinstance(value, str):
            if value is None:
                continue
            value = str(value)
        headers[str(key).lower()] = value
    return headers


def extract_bearer(headers: Any) -> str | None:
    """Return the bearer token, or None when the header is absent or not Bearer."""
    raw = _normalize_headers(headers).get("authorization", "")
    if not raw:
        return None
    match = _BEARER_RE.fullmatch(raw.strip())
    if match is None:
        return None
    token = match.group(1).strip()
    if not token or len(token) > _MAX_PRESENTED_LEN:
        return None
    return token


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def _secrets_client() -> Any:
    import boto3

    return boto3.client("secretsmanager")


def _secret_text(response: dict) -> str | None:
    raw = response.get("SecretString")
    if isinstance(raw, str):
        return raw
    binary = response.get("SecretBinary")
    if isinstance(binary, str):
        return binary
    if isinstance(binary, (bytes, bytearray)):
        return bytes(binary).decode("utf-8")
    return None


def read_configured_token(
    *,
    secrets_client: Any = None,
    now: float | None = None,
) -> str | None:
    """Load the configured token, or None when it cannot be read.

    None fails closed. Lookup errors are logged by type and code only.
    The token itself is never written to the log. Failures are not cached,
    so creating the secret takes effect on the next request.
    """
    global _cache
    name = (os.environ.get(SECRET_NAME_ENV) or "").strip()
    if not name:
        logger.warning("MCP auth secret name is not set")
        return None

    current = now if now is not None else time.monotonic()
    if _cache is not None and _cache[0] == name and _cache[2] > current:
        return _cache[1]

    client = secrets_client if secrets_client is not None else _secrets_client()
    try:
        response = client.get_secret_value(SecretId=name)
        token = token_from_secret_string(_secret_text(response))
    except Exception as exc:  # noqa: BLE001 — any lookup failure must fail closed
        logger.warning(
            "mcp auth token lookup failed type=%s code=%s",
            type(exc).__name__,
            _error_code(exc),
        )
        return None

    if not token:
        logger.warning("mcp auth secret is empty")
        return None

    _cache = (name, token, current + _CACHE_TTL_SECONDS)
    return token


def _path_candidates(event: dict) -> list[str]:
    candidates: list[str] = []
    raw = event.get("rawPath")
    if isinstance(raw, str) and raw:
        candidates.append(raw)
    http = (event.get("requestContext") or {}).get("http") or {}
    if isinstance(http, dict):
        path = http.get("path")
        if isinstance(path, str) and path:
            candidates.append(path)
    path = event.get("path")
    if isinstance(path, str) and path:
        candidates.append(path)
    proxy = (event.get("pathParameters") or {}).get("proxy")
    if isinstance(proxy, str) and proxy:
        candidates.append("/" + proxy.lstrip("/"))
    return candidates


def is_mcp_path(event: dict) -> bool:
    """True for the MCP route and a stage-prefixed form of it (``/prod/mcp``)."""
    for candidate in _path_candidates(event):
        path = candidate.split("?", 1)[0]
        if not path.startswith("/"):
            path = "/" + path
        normalized = path.rstrip("/") or "/"
        if normalized == "/mcp" or normalized.endswith("/mcp"):
            return True
    return False


def _is_valid_id(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _raw_body(event: dict) -> str:
    body = event.get("body")
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8")
    if not isinstance(body, str):
        return ""
    if event.get("isBase64Encoded") and body:
        import base64

        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return body


def _peek_id(event: dict) -> Any:
    raw = _raw_body(event).strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or "id" not in message:
        return None
    ident = message["id"]
    return ident if _is_valid_id(ident) else None


def unauthorized_response(req_id: Any) -> dict[str, Any]:
    """One body for every failure. Missing, malformed, and wrong tokens look the same."""
    ident = req_id if _is_valid_id(req_id) else None
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "WWW-Authenticate": 'Bearer realm="toolweave"',
        },
        "body": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": UNAUTHORIZED_CODE, "message": "Unauthorized"},
            }
        ),
    }


# The static-token loader is looked up at call time, so a request with a good
# CipherWeave token never reads Secrets Manager.
_bearer_auth = BearerAuth(TOKEN_AUDIENCE, legacy_token=lambda: read_configured_token())


def reject_if_unauthorized(event: dict) -> dict[str, Any] | None:
    """Return a 401 Lambda response, or None when the request may proceed.

    Non-``/mcp`` routes return None without reading the secret.
    """
    if not isinstance(event, dict) or not is_mcp_path(event):
        return None
    presented = extract_bearer(event.get("headers"))
    if not _bearer_auth.check(presented).ok:
        logger.warning("MCP unauthorized")
        return unauthorized_response(_peek_id(event))
    return None
