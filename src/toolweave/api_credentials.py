"""Credentials for the APIs ToolWeave calls, one Secrets Manager secret per API.

The executor used to send ``Authorization: Bearer 123`` to every cataloged
API: a placeholder credential, handed to every third party in the catalog.
Now an API gets a credential only if its owner stored one, under
``toolweave/api/<api_id>`` (``TOOLWEAVE_API_SECRET_PREFIX``), as JSON::

    {"host": "api.example.com", "type": "bearer", "token": "..."}
    {"host": "api.example.com", "type": "header", "name": "X-Api-Key", "value": "..."}
    {"host": "api.example.com", "type": "basic", "username": "...", "password": "..."}

``host`` is required and must equal the request's host: a credential stored
for one API is never sent to another host, whatever the catalog later says
about that API's base URL. No secret means no credential header, which is
what an API that needs none should get. A secret that exists but cannot be
read or parsed refuses the call (``CredentialUnavailable``): sending the
request unauthenticated would turn a broken secret into a confusing 401 from
the far side, or worse, an anonymous write that succeeds.

Reads are cached for five minutes, misses included, so a rotation takes
effect without a deploy and a missing secret is not a Secrets Manager call
per request. Values are never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENV_PREFIX = "TOOLWEAVE_API_SECRET_PREFIX"
DEFAULT_PREFIX = "toolweave/api/"
_TTL_SECONDS = 300.0
_API_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FORBIDDEN_NAMES = frozenset({"host", "cookie", "content-length", "transfer-encoding", "connection"})

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_lock = threading.Lock()
_client: Any = None


class CredentialUnavailable(Exception):
    """A credential is configured for this API but cannot be used."""


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _secrets() -> Any:
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("secretsmanager")
    return _client


def _load(api_id: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(api_id)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]
    prefix = os.environ.get(ENV_PREFIX) or DEFAULT_PREFIX
    try:
        raw = _secrets().get_secret_value(SecretId=prefix + api_id).get("SecretString")
    except Exception as exc:
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            value = None
        else:
            logger.warning("api credential read failed api_id=%s code=%s", api_id, code or type(exc).__name__)
            raise CredentialUnavailable("credential for this API cannot be read") from exc
    else:
        try:
            value = json.loads(raw or "")
        except ValueError as exc:
            raise CredentialUnavailable("credential for this API is not JSON") from exc
        if not isinstance(value, dict):
            raise CredentialUnavailable("credential for this API is not a JSON object")
    with _lock:
        _cache[api_id] = (now, value)
    return value


def headers_for(api_id: str, url: str) -> dict[str, str]:
    """The credential header for ``api_id``, if its owner stored one for this URL's host."""
    if not api_id or not _API_ID.fullmatch(api_id):
        return {}
    value = _load(api_id)
    if value is None:
        return {}
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    stored_host = str(value.get("host") or "").lower().rstrip(".")
    if not stored_host:
        raise CredentialUnavailable("credential has no host; refusing to send it anywhere")
    if stored_host != host:
        raise CredentialUnavailable("credential is for a different host")
    kind = value.get("type")
    if kind == "bearer" and isinstance(value.get("token"), str) and value["token"]:
        return {"Authorization": f"Bearer {value['token']}"}
    if kind == "basic" and isinstance(value.get("username"), str) and isinstance(value.get("password"), str):
        pair = f"{value['username']}:{value['password']}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    if kind == "header":
        name, header_value = value.get("name"), value.get("value")
        if (
            isinstance(name, str)
            and _HEADER_NAME.fullmatch(name)
            and name.lower() not in _FORBIDDEN_NAMES
            and isinstance(header_value, str)
            and header_value
            and "\r" not in header_value
            and "\n" not in header_value
        ):
            return {name: header_value}
    raise CredentialUnavailable("credential has an unknown or incomplete type")
