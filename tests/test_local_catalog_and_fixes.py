"""Tests for ToolWeave standalone SQLite catalog, LAN allowlist, and bug fixes."""

from __future__ import annotations

import inspect
from pathlib import Path

from toolweave import data_dictionary_client, swagger_parser
from toolweave.local_catalog import LanAllowlistGuard, SQLiteCatalogStore


def test_no_hardcoded_bearer_123_credentials() -> None:
    dd_src = inspect.getsource(data_dictionary_client)
    sp_src = inspect.getsource(swagger_parser)
    assert "Bearer 123" not in dd_src
    assert "Bearer 123" not in sp_src


def test_sqlite_catalog_ingests_bundled_swagger_specs_and_searches(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    specs_dir = repo_root / "src" / "Swagger"
    db_path = tmp_path / "catalog.sqlite3"

    store1 = SQLiteCatalogStore(db_path)
    counts = store1.ingest_directory(specs_dir)
    assert counts, "Expected at least one bundled spec in src/Swagger"
    total = sum(counts.values())
    assert total > 0
    store1.close()

    # Re-open from disk to prove restart persistence.
    store2 = SQLiteCatalogStore(db_path)
    loaded = store2.load_all()
    assert len(loaded) == total
    hits = store2.search("device")
    assert len(hits) > 0
    store2.close()


def test_lan_allowlist_guard_allows_explicit_lan_and_blocks_metadata() -> None:
    guard = LanAllowlistGuard(["192.168.1.50", "homeassistant.local"])
    ok, reason = guard.is_allowed("http://192.168.1.50:8123/api/states")
    assert ok is True
    assert reason == "allowlisted_host"

    ok2, _ = guard.is_allowed("http://homeassistant.local:8123/api/services")
    assert ok2 is True

    # AWS IMDS and non-allowlisted LAN hosts must be refused.
    blocked_imds, _ = guard.is_allowed("http://169.254.169.254/latest/meta-data/")
    assert blocked_imds is False
    blocked_other, _ = guard.is_allowed("http://192.168.1.99/admin")
    assert blocked_other is False
