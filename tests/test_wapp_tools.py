"""Unit tests for .wapp tool capability sandbox and permission generator."""

from __future__ import annotations

import pytest

from toolweave.wapp_tools import (
    AppToolSandbox,
    SandboxAuditEvent,
    ToolSandboxDeniedError,
    generate_wapp_tool_permissions,
)


def test_generate_wapp_tool_permissions_classifies_http_methods_and_deduplicates() -> None:
    specs = [
        {"name": "weather.forecast", "method": "GET"},
        {"tool_id": "calendar.events", "http_method": "post"},
        {"operation_id": "lights.delete", "method": "DELETE"},
        {"name": "weather.forecast", "method": "HEAD"},  # duplicate :read
        {"id": "notes.search", "read_only": True},
        {"name": "notes.append", "mutating": True},
        {"name": "generic.ping"},
    ]
    perms = generate_wapp_tool_permissions(specs)
    assert perms == [
        "mcp:tool:calendar.events:write",
        "mcp:tool:generic.ping",
        "mcp:tool:lights.delete:write",
        "mcp:tool:notes.append:write",
        "mcp:tool:notes.search:read",
        "mcp:tool:weather.forecast:read",
    ]
    # Sorted and deduplicated
    assert perms == sorted(set(perms))


def test_generate_wapp_tool_permissions_rejects_wildcards_and_missing_names() -> None:
    with pytest.raises(ValueError, match="Wildcard"):
        generate_wapp_tool_permissions([{"name": "weather.*", "method": "GET"}])

    with pytest.raises(ValueError, match="missing non-empty"):
        generate_wapp_tool_permissions([{"method": "GET"}])


def test_sandbox_allows_granted_and_denies_undeclared_before_handler_runs() -> None:
    events: list[SandboxAuditEvent] = []
    sandbox = AppToolSandbox(
        app_id="com.weave.weather",
        granted_permissions={"mcp:tool:weather.get:read", "mcp:tool:cache.kv"},
        audit_callback=events.append,
    )

    called = False

    def ok_handler(city: str) -> dict[str, str]:
        nonlocal called
        called = True
        return {"city": city, "temp": "72F"}

    out = sandbox.execute("weather.get", ok_handler, "Seattle", method="GET")
    assert called is True
    assert out == {"city": "Seattle", "temp": "72F"}

    # Base scope mcp:tool:cache.kv covers both read and write
    assert sandbox.execute("cache.kv", lambda: "ok", method="POST") == "ok"

    # Read-only grant must NOT allow POST/write on the same tool
    mutated = False

    def forbidden_write() -> str:
        nonlocal mutated
        mutated = True
        return "should_not_run"

    with pytest.raises(ToolSandboxDeniedError) as exc_info:
        sandbox.execute("weather.get", forbidden_write, method="POST")
    assert mutated is False
    assert exc_info.value.app_id == "com.weave.weather"
    assert exc_info.value.tool_name == "weather.get"
    assert exc_info.value.required_scope == "mcp:tool:weather.get:write"

    # Completely undeclared tool is denied before handler runs
    with pytest.raises(ToolSandboxDeniedError):
        sandbox.execute("device.unlock", forbidden_write, method="POST")
    assert mutated is False

    # Audit log and callback recorded all 4 decisions
    assert len(sandbox.audit_log) == 4
    assert len(events) == 4
    assert [e.allowed for e in events] == [True, True, False, False]
    assert events[2].to_dict()["required_scope"] == "mcp:tool:weather.get:write"


def test_sandbox_rejects_implicit_wildcards() -> None:
    sandbox = AppToolSandbox(
        app_id="com.weave.untrusted",
        granted_permissions={"*", "mcp:*", "mcp:tool:*", "mcp:tool:*:read"},
    )
    assert sandbox.granted_permissions == frozenset()
    assert sandbox.is_allowed("weather.get", method="GET") is False
    with pytest.raises(ToolSandboxDeniedError):
        sandbox.execute("weather.get", lambda: "boom", method="GET")


@pytest.mark.asyncio
async def test_sandbox_execute_async() -> None:
    sandbox = AppToolSandbox(
        app_id="com.weave.asyncapp",
        granted_permissions={"mcp:tool:async.fetch:read"},
    )

    async def fetch_data() -> str:
        return "async-ok"

    assert await sandbox.execute_async("async.fetch", fetch_data, access="read") == "async-ok"

    with pytest.raises(ToolSandboxDeniedError):
        await sandbox.execute_async("async.fetch", fetch_data, access="write")
