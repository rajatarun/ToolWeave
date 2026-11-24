"""`.wapp` tool capability sandbox and least-privilege permission generator for ToolWeave.

Provides:
- ``generate_wapp_tool_permissions(tool_specs)``: deterministically derives sorted,
  deduplicated ``mcp:tool:<name>[:read|:write]`` scopes for ``weave-app.yaml``.
- ``AppToolSandbox``: fail-closed runtime guard that blocks undeclared tool calls
  with ``ToolSandboxDeniedError`` before any handler/HTTP call executes, while
  recording every allow/deny decision to an audit log and optional callback.

Zero ``weaveos`` imports (standalone dual-mode).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_READ_ONLY_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")
_WILDCARD_SCOPES: frozenset[str] = frozenset({"*", "mcp:*", "mcp:tool:*", "mcp:tool:*:read", "mcp:tool:*:write"})


class ToolSandboxDeniedError(PermissionError):
    """Raised when a `.wapp` attempts to invoke a tool not covered by its granted scopes."""

    def __init__(
        self,
        *,
        app_id: str,
        tool_name: str,
        required_scope: str,
        reason: str = "permission_not_granted",
    ) -> None:
        self.app_id = app_id
        self.tool_name = tool_name
        self.required_scope = required_scope
        self.reason = reason
        super().__init__(
            f"App '{app_id}' denied tool '{tool_name}' "
            f"(required scope '{required_scope}'): {reason}"
        )


@dataclass(frozen=True)
class SandboxAuditEvent:
    """Structured allow/deny decision recorded by ``AppToolSandbox``."""

    app_id: str
    tool_name: str
    required_scope: str
    allowed: bool
    reason: str
    timestamp_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "tool_name": self.tool_name,
            "required_scope": self.required_scope,
            "allowed": self.allowed,
            "reason": self.reason,
            "timestamp_utc": self.timestamp_utc,
        }


def _normalize_tool_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        raise ValueError("Tool name must be a non-empty string")
    if "*" in name:
        raise ValueError(f"Wildcard characters are not allowed in tool name: {name!r}")
    if not _TOOL_NAME_RE.match(name):
        raise ValueError(f"Invalid tool name for scope generation: {name!r}")
    return name


def _classify_access(
    *,
    method: str | None = None,
    mutating: bool | None = None,
    read_only: bool | None = None,
    access: str | None = None,
) -> str | None:
    """Return ``'read'``, ``'write'``, or ``None`` from method/mutability metadata."""
    if access is not None:
        norm = str(access).strip().lower()
        if norm not in ("read", "write"):
            raise ValueError(f"access must be 'read' or 'write', got {access!r}")
        return norm

    if method is not None and str(method).strip():
        verb = str(method).strip().upper()
        if verb in _READ_ONLY_METHODS:
            return "read"
        if verb in _MUTATING_METHODS:
            return "write"
        # Unknown HTTP verb fails safe to write
        return "write"

    if mutating is not None:
        return "write" if bool(mutating) else "read"

    if read_only is not None:
        return "read" if bool(read_only) else "write"

    return None


def generate_wapp_tool_permissions(tool_specs: list[dict[str, Any]]) -> list[str]:
    """Derive sorted, deduplicated least-privilege ``mcp:tool:<name>`` scopes.

    Accepts a list of tool or ``ToolPipeline`` step specs. For each spec:
    - Extracts tool name from ``name``, ``tool_id``, ``operation_id``, or ``id``.
    - Classifies HTTP ``method`` / ``http_method`` (``GET``/``HEAD``/``OPTIONS`` ->
      ``:read``; ``POST``/``PUT``/``PATCH``/``DELETE`` -> ``:write``) or explicit
      ``mutating`` / ``read_only`` / ``access`` hints.
    - Emits ``mcp:tool:<name>:read`` / ``mcp:tool:<name>:write`` when access is
      known, or ``mcp:tool:<name>`` otherwise.
    """
    if not isinstance(tool_specs, list):
        raise TypeError("tool_specs must be a list of dict objects")

    scopes: set[str] = set()
    for idx, spec in enumerate(tool_specs):
        if not isinstance(spec, Mapping):
            raise TypeError(f"tool_specs[{idx}] must be a mapping")

        raw_name = (
            spec.get("name")
            or spec.get("tool_id")
            or spec.get("operation_id")
            or spec.get("id")
        )
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(
                f"tool_specs[{idx}] missing non-empty 'name', 'tool_id', 'operation_id', or 'id'"
            )
        tool_name = _normalize_tool_name(raw_name)

        access = _classify_access(
            method=spec.get("method") or spec.get("http_method"),
            mutating=spec.get("mutating"),
            read_only=spec.get("read_only"),
            access=spec.get("access"),
        )
        if access is not None:
            scopes.add(f"mcp:tool:{tool_name}:{access}")
        else:
            scopes.add(f"mcp:tool:{tool_name}")

    return sorted(scopes)


class AppToolSandbox:
    """Fail-closed runtime capability sandbox for `.wapp` tool invocations.

    Never expands wildcards (``*``, ``mcp:*``, ``mcp:tool:*``). Every allow or
    deny decision is recorded in ``self.audit_log`` and forwarded to
    ``audit_callback`` if provided.
    """

    def __init__(
        self,
        app_id: str,
        granted_permissions: set[str] | Iterable[str],
        *,
        audit_callback: Callable[[SandboxAuditEvent], None] | None = None,
    ) -> None:
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("app_id must be a non-empty string")
        self.app_id = app_id.strip()
        # Filter out any wildcard entries so wildcards can never implicitly grant access.
        cleaned: set[str] = set()
        for perm in granted_permissions:
            p = str(perm or "").strip()
            if not p or "*" in p or p in _WILDCARD_SCOPES:
                continue
            cleaned.add(p)
        self.granted_permissions: frozenset[str] = frozenset(cleaned)
        self._audit_callback = audit_callback
        self.audit_log: list[SandboxAuditEvent] = []

    def _required_and_matching_scopes(
        self,
        tool_name: str,
        *,
        access: str | None = None,
        method: str | None = None,
    ) -> tuple[str, set[str]]:
        clean_name = _normalize_tool_name(tool_name)
        resolved_access = _classify_access(method=method, access=access)
        base_scope = f"mcp:tool:{clean_name}"
        if resolved_access is not None:
            required = f"{base_scope}:{resolved_access}"
            return required, {required, base_scope}
        return base_scope, {base_scope}

    def _record_audit(
        self,
        *,
        tool_name: str,
        required_scope: str,
        allowed: bool,
        reason: str,
    ) -> SandboxAuditEvent:
        event = SandboxAuditEvent(
            app_id=self.app_id,
            tool_name=tool_name,
            required_scope=required_scope,
            allowed=allowed,
            reason=reason,
            timestamp_utc=datetime.now(UTC).isoformat(),
        )
        self.audit_log.append(event)
        if self._audit_callback is not None:
            self._audit_callback(event)
        return event

    def is_allowed(
        self,
        tool_name: str,
        *,
        access: str | None = None,
        method: str | None = None,
    ) -> bool:
        """Evaluate and audit whether ``tool_name`` is permitted under ``granted_permissions``."""
        try:
            required_scope, accepted_scopes = self._required_and_matching_scopes(
                tool_name,
                access=access,
                method=method,
            )
        except ValueError as exc:
            self._record_audit(
                tool_name=str(tool_name),
                required_scope="",
                allowed=False,
                reason=f"invalid_tool_request:{exc}",
            )
            return False

        matched = bool(self.granted_permissions & accepted_scopes)
        self._record_audit(
            tool_name=tool_name.strip(),
            required_scope=required_scope,
            allowed=matched,
            reason="granted" if matched else "permission_not_granted",
        )
        return matched

    def assert_allowed(
        self,
        tool_name: str,
        *,
        access: str | None = None,
        method: str | None = None,
    ) -> None:
        """Verify permission or raise ``ToolSandboxDeniedError`` before execution."""
        try:
            required_scope, accepted_scopes = self._required_and_matching_scopes(
                tool_name,
                access=access,
                method=method,
            )
        except ValueError as exc:
            self._record_audit(
                tool_name=str(tool_name),
                required_scope="",
                allowed=False,
                reason=f"invalid_tool_request:{exc}",
            )
            raise ToolSandboxDeniedError(
                app_id=self.app_id,
                tool_name=str(tool_name),
                required_scope="",
                reason=f"invalid_tool_request:{exc}",
            ) from exc

        if self.granted_permissions & accepted_scopes:
            self._record_audit(
                tool_name=tool_name.strip(),
                required_scope=required_scope,
                allowed=True,
                reason="granted",
            )
            return

        self._record_audit(
            tool_name=tool_name.strip(),
            required_scope=required_scope,
            allowed=False,
            reason="permission_not_granted",
        )
        raise ToolSandboxDeniedError(
            app_id=self.app_id,
            tool_name=tool_name.strip(),
            required_scope=required_scope,
            reason="permission_not_granted",
        )

    def execute(
        self,
        tool_name: str,
        handler: Callable[..., Any],
        *args: Any,
        access: str | None = None,
        method: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Guard synchronous tool execution, raising ``ToolSandboxDeniedError`` on deny."""
        self.assert_allowed(tool_name, access=access, method=method)
        return handler(*args, **kwargs)

    async def execute_async(
        self,
        tool_name: str,
        handler: Callable[..., Any],
        *args: Any,
        access: str | None = None,
        method: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Guard async or sync tool execution, raising ``ToolSandboxDeniedError`` on deny."""
        self.assert_allowed(tool_name, access=access, method=method)
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
