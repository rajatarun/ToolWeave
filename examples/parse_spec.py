#!/usr/bin/env python3
"""Parse a local OpenAPI file the same way ToolWeave ingestion does (offline).

Usage:
    python3 examples/parse_spec.py
    python3 examples/parse_spec.py path/to/spec.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running without install when executed from repo root
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from toolweave.swagger_parser import load_spec_from_bytes, parse_spec  # noqa: E402


def main() -> int:
    spec_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("minimal-openapi.yaml")
    api_id = "example00001"
    raw = load_spec_from_bytes(spec_path.read_bytes(), spec_path.name)
    entries, base_url, title = parse_spec(raw, api_id=api_id)
    print(f"API title: {title}")
    print(f"Base URL:  {base_url}")
    print(f"Endpoints: {len(entries)}")
    for entry in entries:
        print(f"  {entry.method.upper():6} {entry.path:30}  op={entry.operation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
