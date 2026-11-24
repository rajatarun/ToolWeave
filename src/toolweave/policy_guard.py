"""Declarative tool execution policy guard for ToolWeave.

Enforces ``agent-policy-v1`` tool allowlists, denylists, per-tool call quotas,
and recursive sensitive-argument redaction before tool execution.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REDACTED_VALUE = "[REDACTED]"

DEFAULT_REDACT_ARG_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "password",
        "token",
        "secret",
        "authorization",
        "access_token",
        "refresh_token",
        "private_key",
        "client_secret",
    }
)


class ToolPolicyGuard:
    """Declarative fail-closed guard for tool invocation policies and argument redaction."""

    def __init__(
        self,
        allowed_tools: set[str] | Iterable[str] | None = None,
        denied_tools: set[str] | Iterable[str] | None = None,
        max_calls_per_tool: Mapping[str, int] | None = None,
        redact_arg_keys: set[str] | Iterable[str] | None = None,
        *,
        enforcement_mode: str = "enforce",
    ) -> None:
        self.allowed_tools: set[str] | None = (
            {str(t).strip() for t in allowed_tools if str(t).strip()}
            if allowed_tools is not None
            else None
        )
        self.denied_tools: set[str] = (
            {str(t).strip() for t in denied_tools if str(t).strip()}
            if denied_tools is not None
            else set()
        )
        self.max_calls_per_tool: dict[str, int] = {}
        if max_calls_per_tool is not None:
            for tool, limit in max_calls_per_tool.items():
                limit_int = int(limit)
                if limit_int < 0:
                    raise ValueError(f"max_calls_per_tool for '{tool}' must be >= 0")
                self.max_calls_per_tool[str(tool).strip()] = limit_int

        keys_source = DEFAULT_REDACT_ARG_KEYS if redact_arg_keys is None else redact_arg_keys
        self.redact_arg_keys: set[str] = {str(k).strip().lower() for k in keys_source if str(k).strip()}

        mode = str(enforcement_mode or "enforce").strip().lower()
        if mode not in {"enforce", "audit", "disabled"}:
            raise ValueError(f"Unsupported enforcement_mode: {enforcement_mode!r}")
        self.enforcement_mode: str = mode
        self._call_counts: dict[str, int] = {}

    @classmethod
    def from_agent_policy(cls, policy: dict[str, Any]) -> ToolPolicyGuard:
        """Construct a ``ToolPolicyGuard`` from an ``agent-policy-v1`` dictionary."""
        if not isinstance(policy, Mapping):
            raise TypeError("policy must be a mapping")

        schema = policy.get("schema")
        if schema is not None and schema != "agent-policy-v1":
            raise ValueError(f"Unsupported policy schema: {schema!r}")

        raw_allowed = policy.get("allowed_tools")
        allowed_tools: set[str] | None = None
        if raw_allowed is not None:
            if not isinstance(raw_allowed, (list, set, tuple)):
                raise TypeError("allowed_tools must be a list or set of strings")
            allowed_tools = {str(t).strip() for t in raw_allowed if str(t).strip()}

        raw_denied = policy.get("denied_tools")
        denied_tools: set[str] | None = None
        if raw_denied is not None:
            if not isinstance(raw_denied, (list, set, tuple)):
                raise TypeError("denied_tools must be a list or set of strings")
            denied_tools = {str(t).strip() for t in raw_denied if str(t).strip()}

        raw_quotas = policy.get("max_calls_per_tool")
        max_calls_per_tool: dict[str, int] | None = None
        if raw_quotas is not None:
            if not isinstance(raw_quotas, Mapping):
                raise TypeError("max_calls_per_tool must be a mapping")
            max_calls_per_tool = {str(k): int(v) for k, v in raw_quotas.items()}

        raw_redact = policy.get("redact_arg_keys")
        redact_arg_keys: set[str] | None = None
        if raw_redact is not None:
            if not isinstance(raw_redact, (list, set, tuple)):
                raise TypeError("redact_arg_keys must be a list or set of strings")
            redact_arg_keys = {str(k).strip().lower() for k in raw_redact if str(k).strip()}

        enforcement_mode = str(policy.get("enforcement_mode") or "enforce")
        return cls(
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            max_calls_per_tool=max_calls_per_tool,
            redact_arg_keys=redact_arg_keys,
            enforcement_mode=enforcement_mode,
        )

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                key_str = str(key)
                if key_str.strip().lower() in self.redact_arg_keys:
                    sanitized[key_str] = REDACTED_VALUE
                else:
                    sanitized[key_str] = self._sanitize_value(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_value(item) for item in value]
        return value

    def sanitize_args(self, args: Mapping[str, Any] | None) -> dict[str, Any]:
        """Return a deep copy of ``args`` with configured sensitive keys redacted."""
        if not args:
            return {}
        return dict(self._sanitize_value(args))

    def check_call(self, tool_name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Evaluate whether ``tool_name`` is permitted and return sanitized arguments.

        Returns ``{"allowed": bool, "reason": str | None, "audit_violations": list[str],
        "sanitized_args": dict}``.

        - ``"disabled"``: bypasses all checks (``allowed=True``, ``audit_violations=[]``).
        - ``"audit"``: evaluates all rules, records violations in ``audit_violations``,
          and returns ``allowed=True`` so execution proceeds in audit mode.
        - ``"enforce"``: fails closed on the first violation (``allowed=False``).
        """
        normalized_tool = str(tool_name or "").strip()
        sanitized_args = self.sanitize_args(args)

        if self.enforcement_mode == "disabled":
            return {
                "allowed": True,
                "reason": None,
                "audit_violations": [],
                "sanitized_args": sanitized_args,
            }

        violations: list[str] = []

        if not normalized_tool:
            violations.append("empty_tool_name")
        else:
            if normalized_tool in self.denied_tools:
                violations.append(f"tool_denied:{normalized_tool}")

            if self.allowed_tools is not None and normalized_tool not in self.allowed_tools:
                violations.append(f"tool_not_allowed:{normalized_tool}")

            if normalized_tool in self.max_calls_per_tool:
                limit = self.max_calls_per_tool[normalized_tool]
                used = self._call_counts.get(normalized_tool, 0)
                if used >= limit:
                    violations.append(f"tool_quota_exceeded:{normalized_tool}:{used}/{limit}")

        if violations:
            if self.enforcement_mode == "audit":
                return {
                    "allowed": True,
                    "reason": None,
                    "audit_violations": violations,
                    "sanitized_args": sanitized_args,
                }
            return {
                "allowed": False,
                "reason": violations[0],
                "audit_violations": [],
                "sanitized_args": sanitized_args,
            }

        return {
            "allowed": True,
            "reason": None,
            "audit_violations": [],
            "sanitized_args": sanitized_args,
        }

    def record_call(self, tool_name: str) -> int:
        """Record an invocation of ``tool_name`` and return its updated call count."""
        normalized_tool = str(tool_name or "").strip()
        if not normalized_tool:
            return 0
        next_count = self._call_counts.get(normalized_tool, 0) + 1
        self._call_counts[normalized_tool] = next_count
        return next_count

    def get_call_count(self, tool_name: str) -> int:
        """Return the number of recorded calls for ``tool_name``."""
        return self._call_counts.get(str(tool_name or "").strip(), 0)

    def reset_counts(self) -> None:
        """Clear all recorded per-tool call counters."""
        self._call_counts.clear()
