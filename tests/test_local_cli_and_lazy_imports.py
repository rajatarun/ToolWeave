"""Tests for ToolWeave lazy AWS/Mangum imports and local --specs-dir / SQLite CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from toolweave.local_catalog import SQLiteCatalogStore

SAMPLE_OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Home Thermostat API", "version": "1.0.0"},
    "servers": [{"url": "http://192.168.1.42:8080"}],
    "paths": {
        "/thermostat/status": {
            "get": {
                "summary": "Get current thermostat temperature and mode",
                "description": "Returns indoor temperature and HVAC operating mode.",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/thermostat/target": {
            "post": {
                "summary": "Set target thermostat temperature",
                "description": "Updates target temperature in Fahrenheit.",
                "responses": {"200": {"description": "Updated"}},
            }
        },
    },
}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server as srv

    return srv


def test_import_server_without_mangum() -> None:
    """Importing toolweave.server must succeed even when mangum is unavailable."""
    env = dict(os.environ)
    env["MCP_OBSERVATORY_ALLOW_DEV_SECRET"] = "1"
    code = (
        "import sys; sys.modules['mangum'] = None; "
        "import toolweave.server as srv; "
        "assert callable(srv.lambda_handler); "
        "assert callable(srv.main)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_pyproject_moves_aws_dependencies_to_optional_extra() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    core_deps = [d.lower() for d in data["project"]["dependencies"]]
    aws_deps = [d.lower() for d in data["project"]["optional-dependencies"]["aws"]]

    assert not any(d.startswith("mangum") for d in core_deps)
    assert not any(d.startswith("boto3") for d in core_deps)
    assert not any("mcp-observatory[aws]" in d for d in core_deps)
    assert any(d.startswith("mcp-observatory>=") for d in core_deps)

    assert any(d.startswith("mangum") for d in aws_deps)
    assert any(d.startswith("boto3") for d in aws_deps)
    assert any("mcp-observatory[aws]" in d for d in aws_deps)


@pytest.mark.asyncio
async def test_local_specs_dir_ingests_and_exposes_searchable_tools(
    server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "thermostat.json").write_text(
        json.dumps(SAMPLE_OPENAPI_SPEC), encoding="utf-8"
    )
    db_path = tmp_path / "catalog.db"

    # Ensure DynamoDB is never called in local mode
    def _boom() -> list[Any]:
        raise AssertionError("DynamoDB should not be called in local --specs-dir mode")

    monkeypatch.setattr(server.dynamodb_client, "load_full_catalog", _boom)

    old_store = server._local_store
    old_specs = server._local_specs_dir
    old_catalog = list(server._catalog)
    try:
        summary = server.configure_local_catalog(db_path=db_path, specs_dir=specs_dir)
        assert summary["endpoint_count"] == 2
        assert summary["ingested"] == {"thermostat": 2}

        # Verify persisted in SQLiteCatalogStore
        check_store = SQLiteCatalogStore(db_path)
        try:
            assert len(check_store.load_all()) == 2
        finally:
            check_store.close()

        # Verify MCP tools work offline
        health_res = await server.health()
        assert health_res["status"] == "ok"
        assert health_res["endpoint_count"] == 2

        search_res = await server.search_catalog("thermostat temperature")
        assert search_res["status"] == "ok"
        assert search_res["count"] >= 1
        paths = {ep["path"] for ep in search_res["endpoints"]}
        assert "/thermostat/status" in paths

        # reload_catalog refreshes from local specs_dir without DynamoDB
        reload_res = await server.reload_catalog()
        assert reload_res["status"] == "ok"
        assert reload_res["endpoint_count"] == 2
        assert reload_res["api_count"] == 1
    finally:
        if server._local_store is not None:
            server._local_store.close()
        server._local_store = old_store
        server._local_specs_dir = old_specs
        server._catalog = old_catalog


def test_cli_main_local_specs_dir_stdio_and_uds(
    server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "thermostat.json").write_text(
        json.dumps(SAMPLE_OPENAPI_SPEC), encoding="utf-8"
    )
    db_path = tmp_path / "cli_catalog.db"
    sock_path = tmp_path / "toolweave.sock"

    def _boom() -> list[Any]:
        raise AssertionError("DynamoDB must not be called when --specs-dir is passed")

    monkeypatch.setattr(server.dynamodb_client, "load_full_catalog", _boom)

    ran_transports: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        server.mcp, "run", lambda *, transport: ran_transports.append(("mcp", transport))
    )

    old_store = server._local_store
    old_specs = server._local_specs_dir
    old_catalog = list(server._catalog)
    try:
        rc = server.main(
            [
                "--specs-dir",
                str(specs_dir),
                "--db",
                str(db_path),
                "--transport",
                "stdio",
            ]
        )
        assert rc == 0
        assert ran_transports == [("mcp", "stdio")]
        assert len(server._catalog) == 2

        import uvicorn

        uvicorn_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kwargs: uvicorn_calls.append(kwargs)
        )
        rc2 = server.main(
            [
                "--specs-dir",
                str(specs_dir),
                "--db",
                str(db_path),
                "--transport",
                "uds",
                "--socket",
                str(sock_path),
            ]
        )
        assert rc2 == 0
        assert uvicorn_calls == [{"uds": str(sock_path)}]
    finally:
        if server._local_store is not None:
            server._local_store.close()
        server._local_store = old_store
        server._local_specs_dir = old_specs
        server._catalog = old_catalog
