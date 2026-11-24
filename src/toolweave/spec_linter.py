"""Offline OpenAPI 3.x specification linter for ToolWeave.

Validates local OpenAPI specs before ingestion into the ToolWeave catalog:
- OpenAPI 3.x version header and non-empty ``paths``
- Required and unique ``operationId`` per HTTP operation
- Parameter and ``requestBody`` schema presence
- Server URL safety (HTTPS-only, rejecting non-HTTPS and wildcard URLs)
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
_WILDCARD_HOSTS = frozenset({"*", "0.0.0.0", "::", "[::]"})


def load_openapi_spec(spec_path: str | Path) -> dict[str, Any]:
    """Load an OpenAPI specification from a ``.json``, ``.yaml``, or ``.yml`` file."""
    path = Path(spec_path)
    raw_text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        parsed = json.loads(raw_text)
    elif suffix in (".yaml", ".yml"):
        try:
            parsed = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in '{path}': {exc}") from exc
    else:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            try:
                parsed = yaml.safe_load(raw_text)
            except yaml.YAMLError as exc:
                raise ValueError(f"Invalid JSON/YAML in '{path}': {exc}") from exc

    if not isinstance(parsed, dict):
        raise TypeError(f"OpenAPI spec at '{path}' must be a mapping/object")
    return parsed


def _check_parameters(
    parameters: Any,
    base_path: str,
    issues: list[dict[str, str]],
) -> None:
    if not isinstance(parameters, list):
        issues.append(
            {
                "severity": "error",
                "path": base_path,
                "message": "parameters must be a list",
            }
        )
        return

    for idx, param in enumerate(parameters):
        param_path = f"{base_path}[{idx}]"
        if not isinstance(param, dict):
            issues.append(
                {
                    "severity": "error",
                    "path": param_path,
                    "message": "parameter entry must be an object",
                }
            )
            continue
        if "$ref" in param:
            continue
        schema = param.get("schema")
        content = param.get("content")
        has_schema = isinstance(schema, dict) and bool(schema)
        has_content = isinstance(content, dict) and bool(content)
        if not has_schema and not has_content:
            param_name = param.get("name") or f"index {idx}"
            issues.append(
                {
                    "severity": "error",
                    "path": f"{param_path}.schema",
                    "message": f"Parameter '{param_name}' is missing a non-empty 'schema' definition",
                }
            )


def _check_request_body(
    request_body: Any,
    base_path: str,
    issues: list[dict[str, str]],
) -> None:
    if not isinstance(request_body, dict):
        issues.append(
            {
                "severity": "error",
                "path": base_path,
                "message": "requestBody must be an object",
            }
        )
        return
    if "$ref" in request_body:
        return

    content = request_body.get("content")
    if not isinstance(content, dict) or not content:
        issues.append(
            {
                "severity": "error",
                "path": f"{base_path}.content",
                "message": "requestBody is missing a non-empty 'content' mapping",
            }
        )
        return

    for media_type, media_obj in content.items():
        media_path = f"{base_path}.content.{media_type}"
        if not isinstance(media_obj, dict):
            issues.append(
                {
                    "severity": "error",
                    "path": media_path,
                    "message": f"requestBody media type '{media_type}' must be an object",
                }
            )
            continue
        schema = media_obj.get("schema")
        if not isinstance(schema, dict) or not schema:
            issues.append(
                {
                    "severity": "error",
                    "path": f"{media_path}.schema",
                    "message": f"requestBody media type '{media_type}' is missing a non-empty 'schema'",
                }
            )


def _check_servers(spec: dict[str, Any], issues: list[dict[str, str]]) -> None:
    servers = spec.get("servers")
    if servers is None:
        issues.append(
            {
                "severity": "warning",
                "path": "servers",
                "message": "No 'servers' block declared in OpenAPI spec",
            }
        )
        return
    if not isinstance(servers, list):
        issues.append(
            {
                "severity": "error",
                "path": "servers",
                "message": "'servers' must be a list of server objects",
            }
        )
        return
    if not servers:
        issues.append(
            {
                "severity": "warning",
                "path": "servers",
                "message": "'servers' list is empty",
            }
        )
        return

    for idx, server in enumerate(servers):
        url_path = f"servers[{idx}].url"
        if not isinstance(server, dict):
            issues.append(
                {
                    "severity": "error",
                    "path": f"servers[{idx}]",
                    "message": "server entry must be an object",
                }
            )
            continue
        url = server.get("url")
        if not isinstance(url, str) or not url.strip():
            issues.append(
                {
                    "severity": "error",
                    "path": url_path,
                    "message": "server 'url' must be a non-empty string",
                }
            )
            continue

        trimmed = url.strip()
        parsed = urllib.parse.urlparse(trimmed)
        hostname = (parsed.hostname or "").strip().lower()
        if "*" in trimmed or hostname in _WILDCARD_HOSTS:
            issues.append(
                {
                    "severity": "error",
                    "path": url_path,
                    "message": f"Unsafe wildcard server URL: '{trimmed}'",
                }
            )
        if parsed.scheme.lower() != "https":
            issues.append(
                {
                    "severity": "error",
                    "path": url_path,
                    "message": f"Unsafe non-HTTPS server URL: '{trimmed}'",
                }
            )
        elif not parsed.netloc:
            issues.append(
                {
                    "severity": "error",
                    "path": url_path,
                    "message": f"Server URL is missing host: '{trimmed}'",
                }
            )


def lint_openapi_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Lint an OpenAPI 3.x specification dict offline.

    Returns:
        ``{"ok": bool, "error_count": int, "warning_count": int, "issues": [...]}``
        where each issue has ``{"severity": "error"|"warning", "path": str, "message": str}``.
    """
    issues: list[dict[str, str]] = []

    if not isinstance(spec, dict):
        issues.append(
            {
                "severity": "error",
                "path": "$",
                "message": "OpenAPI spec must be a JSON/YAML object",
            }
        )
        return {
            "ok": False,
            "error_count": 1,
            "warning_count": 0,
            "issues": issues,
        }

    version = spec.get("openapi")
    if not isinstance(version, str) or not version.strip().startswith("3."):
        issues.append(
            {
                "severity": "error",
                "path": "openapi",
                "message": f"Expected OpenAPI 3.x version string, got {version!r}",
            }
        )

    info = spec.get("info")
    if not isinstance(info, dict):
        issues.append(
            {
                "severity": "warning",
                "path": "info",
                "message": "Missing 'info' metadata object",
            }
        )
    else:
        if not info.get("title"):
            issues.append(
                {
                    "severity": "warning",
                    "path": "info.title",
                    "message": "Missing 'info.title'",
                }
            )
        if not info.get("version"):
            issues.append(
                {
                    "severity": "warning",
                    "path": "info.version",
                    "message": "Missing 'info.version'",
                }
            )

    _check_servers(spec, issues)

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        issues.append(
            {
                "severity": "error",
                "path": "paths",
                "message": "OpenAPI spec must define a non-empty 'paths' object",
            }
        )
    else:
        seen_operation_ids: dict[str, str] = {}
        for path_key, path_item in paths.items():
            p_path = f"paths.{path_key}"
            if not isinstance(path_key, str) or not path_key.startswith("/"):
                issues.append(
                    {
                        "severity": "error",
                        "path": p_path,
                        "message": f"Path key {path_key!r} must start with '/'",
                    }
                )
            if not isinstance(path_item, dict):
                issues.append(
                    {
                        "severity": "error",
                        "path": p_path,
                        "message": "Path item must be an object",
                    }
                )
                continue

            if "parameters" in path_item:
                _check_parameters(path_item["parameters"], f"{p_path}.parameters", issues)

            found_methods = 0
            for method_key, operation in path_item.items():
                if not isinstance(method_key, str) or method_key.lower() not in _HTTP_METHODS:
                    continue
                found_methods += 1
                op_path = f"{p_path}.{method_key.lower()}"
                if not isinstance(operation, dict):
                    issues.append(
                        {
                            "severity": "error",
                            "path": op_path,
                            "message": "Operation definition must be an object",
                        }
                    )
                    continue

                op_id = operation.get("operationId")
                if not isinstance(op_id, str) or not op_id.strip():
                    issues.append(
                        {
                            "severity": "error",
                            "path": f"{op_path}.operationId",
                            "message": "Operation is missing a non-empty 'operationId'",
                        }
                    )
                else:
                    clean_id = op_id.strip()
                    if clean_id in seen_operation_ids:
                        issues.append(
                            {
                                "severity": "error",
                                "path": f"{op_path}.operationId",
                                "message": (
                                    f"Duplicate operationId '{clean_id}' "
                                    f"(already defined at {seen_operation_ids[clean_id]})"
                                ),
                            }
                        )
                    else:
                        seen_operation_ids[clean_id] = op_path

                if "parameters" in operation:
                    _check_parameters(
                        operation["parameters"], f"{op_path}.parameters", issues
                    )
                if "requestBody" in operation:
                    _check_request_body(
                        operation["requestBody"], f"{op_path}.requestBody", issues
                    )

            if found_methods == 0:
                issues.append(
                    {
                        "severity": "warning",
                        "path": p_path,
                        "message": "Path item defines no HTTP operations",
                    }
                )

    error_count = sum(1 for item in issues if item["severity"] == "error")
    warning_count = sum(1 for item in issues if item["severity"] == "warning")
    return {
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
