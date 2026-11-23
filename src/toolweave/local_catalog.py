"""Standalone local SQLite OpenAPI catalog and LAN egress allowlist for ToolWeave.

Allows ToolWeave to ingest OpenAPI 2/3 YAML/JSON specs from a local directory,
store them in WAL SQLite, search endpoints offline, and validate explicit LAN
egress allowlists without requiring AWS S3 or DynamoDB.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .catalog_search import search as search_catalog
from .models import EndpointEntry
from .swagger_parser import load_spec_from_bytes, parse_spec


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteCatalogStore:
    """Thread-safe WAL SQLite catalog store for standalone local/edge ToolWeave."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        path_str = str(db_path)
        if path_str != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path_str, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_catalog (
                    api_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    entry_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (api_id, endpoint_id)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def save_entries(self, api_id: str, entries: list[EndpointEntry]) -> int:
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute("DELETE FROM api_catalog WHERE api_id = ?", (api_id,))
            for entry in entries:
                ep_key = f"{entry.method}:{entry.path}"
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO api_catalog (api_id, endpoint_id, entry_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (api_id, ep_key, entry.model_dump_json(), now),
                )
            self._conn.commit()
        return len(entries)

    def load_all(self) -> list[EndpointEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_json FROM api_catalog ORDER BY api_id, endpoint_id"
            ).fetchall()
        return [EndpointEntry.model_validate(json.loads(r["entry_json"])) for r in rows]

    def ingest_directory(self, specs_dir: str | Path) -> dict[str, int]:
        """Parse all .yaml/.yml/.json OpenAPI specs in ``specs_dir`` into SQLite."""
        root = Path(specs_dir)
        counts: dict[str, int] = {}
        if not root.is_dir():
            return counts
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in (".yaml", ".yml", ".json"):
                continue
            raw = load_spec_from_bytes(path.read_bytes(), path.name)
            if not isinstance(raw, dict) or "paths" not in raw:
                continue
            api_id = path.stem.lower()
            entries, _base_url, _title = parse_spec(raw, api_id=api_id)
            counts[api_id] = self.save_entries(api_id, entries)
        return counts

    def search(self, query: str, *, top_k: int = 10) -> list[EndpointEntry]:
        return search_catalog(query, self.load_all(), top_k=top_k)


class LanAllowlistGuard:
    """Explicit opt-in allowlist for private/LAN hosts (e.g. Home Assistant on a Pi LAN).

    Refuses loopback/link-local/metadata addresses (169.254.169.254, 127.0.0.1) even
    when LAN allowlisting is active, unless explicitly named.
    """

    def __init__(self, allowed_hosts: Iterable[str] = ()) -> None:
        self._hosts = frozenset(h.strip().lower() for h in allowed_hosts if h and h.strip())

    def is_allowed(self, url: str) -> tuple[bool, str]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"unsupported_scheme:{parsed.scheme}"
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False, "missing_host"
        if host in self._hosts:
            return True, "allowlisted_host"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False, f"host_not_allowlisted:{host}"
        if ip.is_link_local or ip.is_loopback or ip.is_multicast:
            return False, f"forbidden_ip:{host}"
        return False, f"ip_not_allowlisted:{host}"

    def check_url(self, url: str) -> tuple[bool, str]:
        """Validate ``url`` against the explicit LAN host allowlist."""
        return self.is_allowed(url)
