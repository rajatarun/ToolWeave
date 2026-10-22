"""Bind a caller-supplied plan to the catalog before anything is sent.

``post_tool`` takes ``api_call_plan`` as an MCP tool *argument*. pre_tool
builds it from the catalog, but nothing makes a caller send pre_tool's output:
any client holding the ``execute`` scope could pass
``{"base_url": "http://10.0.0.5:7687", "path": "/", "headers": {...}}`` and
the Lambda would make that request from inside the VPC and return the body.
That is server-side request forgery, with the response handed back.

Two checks close it, independent of each other:

1. **The plan must be a cataloged endpoint** (``bind``). Method, base URL and
   path template must match one ``EndpointEntry``; path parameters are single
   segments (no ``/``, ``..``, ``?``, ``#``, or their percent-encodings);
   query parameters and headers must be ones the spec declares, and a header
   that carries authority (``Authorization``, ``Host``, ``Cookie``, …) is
   never taken from a plan.
2. **The destination must be on the public internet** (``check_egress``),
   checked again at execution time for GETs and commits alike: no loopback,
   private, link-local (the Lambda runtime API and instance metadata live
   there), multicast or reserved address, as a literal or by DNS. A spec
   uploaded with an internal ``servers`` URL passes (1) and fails here.
   ``TOOLWEAVE_ALLOW_PRIVATE_EGRESS=true`` opts out, for local development
   against a service on localhost.

DNS is resolved once for the check and again by the HTTP client, so a
rebinding resolver can still answer differently the second time. Closing that
needs connecting to the checked address, which the HTTP client does not offer;
the check still removes the direct path, which is the one an agent can be
talked into.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import EndpointEntry, PreToolResponse

ENV_ALLOW_PRIVATE_EGRESS = "TOOLWEAVE_ALLOW_PRIVATE_EGRESS"

# Headers that carry identity, routing or framing. A plan never sets them.
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "forwarded",
    }
)
_FORBIDDEN_HEADER_PREFIXES = ("x-forwarded-", "x-amz-", "x-real-ip", "proxy-")
_PARAM = re.compile(r"\{([^{}/]+)\}")
_BAD_SEGMENT = re.compile(r"(^\.{1,2}$)|[/?#\\]")
_INTERNAL_SUFFIXES = (".internal", ".local", ".localhost")


class PlanRejected(ValueError):
    """The plan is not a call this catalog can make."""


class EgressDenied(ValueError):
    """The destination is not on the public internet."""


@dataclass(frozen=True)
class BoundCall:
    entry: EndpointEntry
    method: str
    base_url: str
    path: str
    query_params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None


def _template_regex(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    last = 0
    for match in _PARAM.finditer(template):
        parts.append(re.escape(template[last : match.start()]))
        parts.append(f"(?P<p{len(parts)}>[^/]+)")
        last = match.end()
    parts.append(re.escape(template[last:]))
    return re.compile("^" + "".join(parts) + "$")


def _segments_safe(template: str, path: str) -> bool:
    match = _template_regex(template).match(path)
    if match is None:
        return False
    for value in match.groupdict().values():
        decoded = unquote(unquote(value))  # double-encoding is still a traversal
        if not decoded or _BAD_SEGMENT.search(decoded):
            return False
    return True


def _header_allowed(name: str) -> bool:
    lower = name.lower()
    return lower not in _FORBIDDEN_HEADERS and not lower.startswith(_FORBIDDEN_HEADER_PREFIXES)


def bind(plan: PreToolResponse, catalog: list[EndpointEntry]) -> BoundCall:
    """The cataloged call ``plan`` describes, or ``PlanRejected``."""
    method = (plan.method or "").upper()
    path = plan.path or ""
    if not path.startswith("/") or "://" in path:
        raise PlanRejected("path must be a path on the endpoint's base URL")
    candidates = [
        e
        for e in catalog
        if e.method.upper() == method
        and e.base_url
        and e.base_url.rstrip("/") == (plan.base_url or "").rstrip("/")
        and _segments_safe(e.path, path)
    ]
    if plan.operation_id:
        candidates = [e for e in candidates if e.operation_id == plan.operation_id] or candidates
    if not candidates:
        raise PlanRejected("plan does not match a cataloged endpoint")
    entry = candidates[0]

    declared_query = {p.name for p in entry.parameters if p.location == "query"}
    unknown = sorted(set(plan.query_params or {}) - declared_query)
    if unknown:
        raise PlanRejected(f"query parameter not in the spec: {unknown[0]}")
    declared_headers = {p.name.lower() for p in entry.parameters if p.location == "header"}
    headers: dict[str, str] = {}
    for name, value in (plan.headers or {}).items():
        if not _header_allowed(name):
            raise PlanRejected(f"header {name} cannot be set by a plan")
        if name.lower() not in declared_headers:
            raise PlanRejected(f"header not in the spec: {name}")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise PlanRejected(f"header {name} has an invalid value")
        headers[name] = value

    if method == "GET" and plan.body:
        raise PlanRejected("a GET carries no body")
    return BoundCall(
        entry=entry,
        method=method,
        base_url=entry.base_url,
        path=path,
        query_params=dict(plan.query_params or {}),
        headers=headers,
        body=plan.body,
    )


def _private(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def check_egress(url: str, *, resolver: Any = socket.getaddrinfo) -> None:
    """Raise ``EgressDenied`` unless ``url`` resolves only to public addresses."""
    if (os.environ.get(ENV_ALLOW_PRIVATE_EGRESS) or "").strip().lower() == "true":
        return
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise EgressDenied("only http(s) destinations")
    if parts.username or parts.password:
        raise EgressDenied("credentials in the URL")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise EgressDenied("no host")
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise EgressDenied("internal host name")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _private(host):
            raise EgressDenied("non-public address")
        return
    try:
        infos = resolver(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise EgressDenied("host does not resolve") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(_private(a.split("%", 1)[0]) for a in addresses):
        raise EgressDenied("host resolves to a non-public address")
