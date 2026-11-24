"""Local multi-step OpenAPI tool pipeline executor for ToolWeave.

Chains cataloged OpenAPI endpoints with:
- ``$steps.<step_id>.<dot.path>`` template resolution from prior step outputs
- Argument validation against catalog entry JSON Schema / OpenAPI parameter definitions
- Per-step ``LanAllowlistGuard.check_url(target_url)`` egress enforcement
- Side-effect-free ``dry_run=True`` planning with zero HTTP calls
"""

from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .executor import _build_url
from .local_catalog import LanAllowlistGuard, SQLiteCatalogStore
from .models import EndpointEntry
from .plan_guard import _BAD_SEGMENT, _header_allowed

_STEP_REF_FULL = re.compile(r"^\$steps\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)$")
_STEP_REF_INLINE = re.compile(r"\$steps\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_.-]+)")
_PATH_PARAM_RE = re.compile(r"\{([^{}/]+)\}")


class TemplateResolutionError(ValueError):
    """Raised when a ``$steps.<step_id>.<dot.path>`` reference cannot be resolved."""


class SchemaValidationError(ValueError):
    """Raised when resolved step arguments fail catalog schema validation."""


@dataclass
class PipelineStep:
    """Single step in a local multi-step ToolWeave pipeline."""

    step_id: str
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    max_retries: int = 0
    backoff_ms: int = 0
    retry_on_exceptions: tuple[type[BaseException], ...] = (
        TimeoutError,
        ConnectionError,
        RuntimeError,
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PipelineStep:
        step_id = str(raw.get("step_id") or "").strip()
        tool_id = str(raw.get("tool_id") or "").strip()
        if not step_id:
            raise ValueError("PipelineStep requires a non-empty 'step_id'")
        if not tool_id:
            raise ValueError(f"PipelineStep '{step_id}' requires a non-empty 'tool_id'")
        arguments = raw.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise TypeError(f"PipelineStep '{step_id}' arguments must be a JSON object")
        timeout = float(raw.get("timeout_seconds", 10.0))
        max_retries = int(raw.get("max_retries", 0))
        backoff_ms = int(raw.get("backoff_ms", 0))
        if max_retries < 0:
            raise ValueError(f"PipelineStep '{step_id}' max_retries must be >= 0")
        if backoff_ms < 0:
            raise ValueError(f"PipelineStep '{step_id}' backoff_ms must be >= 0")
        return cls(
            step_id=step_id,
            tool_id=tool_id,
            arguments=dict(arguments),
            timeout_seconds=timeout,
            max_retries=max_retries,
            backoff_ms=backoff_ms,
        )


@dataclass
class StepExecutionResult:
    """Structured per-step outcome from ``ToolPipeline.execute``."""

    step_id: str
    tool_id: str
    status: str  # "ok" | "dry_run" | "blocked" | "error"
    method: str = ""
    url: str = ""
    query_params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    payload: dict[str, Any] | None = None
    status_code: int = 0
    latency_ms: float = 0.0
    attempts: int = 1
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step_id": self.step_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "method": self.method,
            "url": self.url,
            "query_params": dict(self.query_params),
            "headers": dict(self.headers),
            "payload": self.payload,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "output": self.output,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class PipelineResult:
    """Aggregate result returned by ``ToolPipeline.execute``."""

    status: str  # "completed" | "dry_run" | "blocked" | "error"
    dry_run: bool = False
    steps: list[StepExecutionResult] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "dry_run": self.dry_run,
            "steps": [s.to_dict() for s in self.steps],
            "outputs": dict(self.outputs),
        }
        if self.error is not None:
            data["error"] = self.error
        return data


def _extract_dot_path(data: Any, dot_path: str) -> Any:
    current = data
    for segment in dot_path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                raise TemplateResolutionError(f"missing_field:{segment}")
            current = current[segment]
        elif isinstance(current, list):
            try:
                idx = int(segment)
            except ValueError as exc:
                raise TemplateResolutionError(f"invalid_list_index:{segment}") from exc
            if idx < 0 or idx >= len(current):
                raise TemplateResolutionError(f"list_index_out_of_range:{idx}")
            current = current[idx]
        else:
            raise TemplateResolutionError(f"cannot_traverse:{segment}")
    return current


def resolve_templates(
    value: Any,
    step_outputs: Mapping[str, Any],
    *,
    known_prior_steps: set[str],
    dry_run: bool = False,
) -> Any:
    """Recursively resolve ``$steps.<step_id>.<dot.path>`` templates."""
    if isinstance(value, str):
        full_match = _STEP_REF_FULL.match(value)
        if full_match:
            ref_step, dot_path = full_match.group(1), full_match.group(2)
            if ref_step not in known_prior_steps:
                raise TemplateResolutionError(f"unknown_prior_step:{ref_step}")
            if dry_run and ref_step not in step_outputs:
                return value
            return _extract_dot_path(step_outputs[ref_step], dot_path)

        def _replace(match: re.Match[str]) -> str:
            ref_step, dot_path = match.group(1), match.group(2)
            if ref_step not in known_prior_steps:
                raise TemplateResolutionError(f"unknown_prior_step:{ref_step}")
            if dry_run and ref_step not in step_outputs:
                return match.group(0)
            resolved = _extract_dot_path(step_outputs[ref_step], dot_path)
            return str(resolved)

        return _STEP_REF_INLINE.sub(_replace, value)

    if isinstance(value, list):
        return [
            resolve_templates(
                item,
                step_outputs,
                known_prior_steps=known_prior_steps,
                dry_run=dry_run,
            )
            for item in value
        ]

    if isinstance(value, dict):
        return {
            k: resolve_templates(
                v,
                step_outputs,
                known_prior_steps=known_prior_steps,
                dry_run=dry_run,
            )
            for k, v in value.items()
        }

    return value


def _is_unresolved_dry_run_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(_STEP_REF_INLINE.search(value))


def _matches_openapi_type(value: Any, data_type: str) -> bool:
    t = (data_type or "").strip().lower()
    if not t or t == "any":
        return True
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def _get_nested_arg(args: Mapping[str, Any], dot_name: str) -> tuple[bool, Any]:
    if dot_name in args:
        return True, args[dot_name]
    body = args.get("body")
    if isinstance(body, Mapping) and dot_name in body:
        return True, body[dot_name]

    for root in (args, body) if isinstance(body, Mapping) else (args,):
        current: Any = root
        found = True
        for part in dot_name.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return True, current
    return False, None


def _set_nested_value(target: dict[str, Any], dot_name: str, value: Any) -> None:
    parts = dot_name.split(".")
    curr = target
    for part in parts[:-1]:
        nxt = curr.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            curr[part] = nxt
        curr = nxt
    curr[parts[-1]] = value


def _validate_arguments(
    entry: EndpointEntry,
    arguments: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    """Validate step arguments against the catalog entry's schema / OpenAPI fields."""
    explicit_schema = getattr(entry, "input_schema", None)
    if isinstance(explicit_schema, dict) and explicit_schema:
        try:
            import jsonschema

            # Skip jsonschema type errors only for unresolved dry-run template strings
            if not (
                dry_run
                and any(_is_unresolved_dry_run_placeholder(v) for v in arguments.values())
            ):
                jsonschema.validate(instance=dict(arguments), schema=explicit_schema)
        except ImportError:
            pass
        except Exception as exc:
            raise SchemaValidationError(f"schema_validation_failed:{exc}") from exc

    # Validate declared OpenAPI parameters (path, query, header)
    for param in entry.parameters:
        present = param.name in arguments and arguments[param.name] is not None
        if param.required and not present:
            raise SchemaValidationError(f"missing_required_parameter:{param.name}")
        if present:
            val = arguments[param.name]
            if not (dry_run and _is_unresolved_dry_run_placeholder(val)) and not _matches_openapi_type(
                val, param.data_type
            ):
                raise SchemaValidationError(
                    f"invalid_type_for_{param.name}:expected_{param.data_type}"
                )

    # Validate declared OpenAPI request body fields
    for field_spec in entry.body_fields:
        present, val = _get_nested_arg(arguments, field_spec.name)
        if field_spec.required and (not present or val is None):
            raise SchemaValidationError(f"missing_required_body_field:{field_spec.name}")
        if (
            present
            and val is not None
            and not (dry_run and _is_unresolved_dry_run_placeholder(val))
            and not _matches_openapi_type(val, field_spec.data_type)
        ):
            raise SchemaValidationError(
                f"invalid_type_for_{field_spec.name}:expected_{field_spec.data_type}"
            )


def _default_http_executor(
    method: str,
    url: str,
    *,
    query_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        kwargs: dict[str, Any] = {
            "params": query_params or None,
            "headers": headers or None,
        }
        if payload is not None:
            kwargs["json"] = payload
        resp = client.request(method, url, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return {"status_code": resp.status_code, "body": body}


class ToolPipeline:
    """Multi-step local OpenAPI tool pipeline with schema validation and allowlist checks."""

    def __init__(
        self,
        catalog: SQLiteCatalogStore,
        *,
        allowlist: LanAllowlistGuard | None = None,
        http_executor: Callable[..., Any] | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.allowlist = allowlist
        self._http_executor = http_executor or _default_http_executor
        self._telemetry_sink = telemetry_sink
        self._sleep_fn = sleep_fn or time.sleep

    def _emit_telemetry(
        self,
        step_result: StepExecutionResult,
        *,
        trace_id: str,
        telemetry_status: str | None = None,
    ) -> None:
        if self._telemetry_sink is None:
            return
        if telemetry_status is None:
            if step_result.status == "ok":
                telemetry_status = "ok"
            elif step_result.status == "blocked":
                telemetry_status = "aborted"
            else:
                telemetry_status = "error"
        event: dict[str, Any] = {
            "schema": "weave-telemetry-v1",
            "event_id": str(uuid.uuid4()),
            "source_repo": "ToolWeave",
            "event_type": "tool_call",
            "status": telemetry_status,
            "latency_ms": max(0.0, float(step_result.latency_ms)),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "trace_id": trace_id,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "attributes": {
                "step_id": step_result.step_id,
                "tool_id": step_result.tool_id,
                "method": step_result.method,
                "url": step_result.url,
                "status_code": step_result.status_code,
                "attempts": step_result.attempts,
                "step_status": step_result.status,
            },
        }
        if step_result.error is not None:
            event["error_message"] = step_result.error
        self._telemetry_sink(event)

    def _lookup_tool(self, tool_id: str, entries: list[EndpointEntry]) -> EndpointEntry | None:
        target = tool_id.strip()
        for entry in entries:
            if entry.operation_id and entry.operation_id == target:
                return entry
            if f"{entry.api_id}:{entry.method}:{entry.path}" == target:
                return entry
            if f"{entry.method}:{entry.path}" == target:
                return entry
            if f"{entry.method} {entry.path}" == target:
                return entry
            if entry.operation_id and f"{entry.api_id}.{entry.operation_id}" == target:
                return entry
        return None

    def _check_allowlist(self, url: str) -> tuple[bool, str]:
        if self.allowlist is None:
            return True, "no_allowlist_configured"
        if hasattr(self.allowlist, "check_url"):
            return self.allowlist.check_url(url)
        return self.allowlist.is_allowed(url)

    def _build_request_parts(
        self,
        entry: EndpointEntry,
        arguments: Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> tuple[str, dict[str, Any], dict[str, str], dict[str, Any] | None]:
        path_param_names = set(_PATH_PARAM_RE.findall(entry.path))
        param_locations = {p.name: p.location for p in entry.parameters}

        resolved_path = entry.path
        for name in path_param_names:
            if name not in arguments or arguments[name] is None:
                raise SchemaValidationError(f"missing_required_path_parameter:{name}")
            raw_val = str(arguments[name])
            if not (dry_run and _is_unresolved_dry_run_placeholder(raw_val)):
                decoded = urllib.parse.unquote(urllib.parse.unquote(raw_val))
                if not decoded or _BAD_SEGMENT.search(decoded):
                    raise SchemaValidationError(f"unsafe_path_parameter:{name}")
                encoded = urllib.parse.quote(raw_val, safe="")
            else:
                encoded = raw_val
            resolved_path = resolved_path.replace(f"{{{name}}}", encoded)

        query_params: dict[str, Any] = {}
        headers: dict[str, str] = {}
        consumed: set[str] = set(path_param_names)

        for name, loc in param_locations.items():
            if name not in arguments or arguments[name] is None:
                continue
            if loc == "query":
                query_params[name] = arguments[name]
                consumed.add(name)
            elif loc == "header":
                if not _header_allowed(name):
                    raise SchemaValidationError(f"forbidden_header:{name}")
                headers[name] = str(arguments[name])
                consumed.add(name)

        payload: dict[str, Any] | None = None
        if entry.method.upper() not in ("GET", "HEAD"):
            if isinstance(arguments.get("body"), dict):
                payload = dict(arguments["body"])
                consumed.add("body")
            else:
                body_dict: dict[str, Any] = {}
                if entry.body_fields:
                    for bf in entry.body_fields:
                        present, val = _get_nested_arg(arguments, bf.name)
                        if present:
                            _set_nested_value(body_dict, bf.name, val)
                            consumed.add(bf.name.split(".", 1)[0])
                for k, v in arguments.items():
                    if k not in consumed:
                        body_dict[k] = v
                if body_dict:
                    payload = body_dict

        target_url = _build_url(entry.base_url, resolved_path)
        return target_url, query_params, headers, payload

    def execute(
        self,
        steps: Sequence[PipelineStep | Mapping[str, Any]],
        *,
        dry_run: bool = False,
        trace_id: str | None = None,
    ) -> PipelineResult:
        """Execute (or dry-run validate) a multi-step tool pipeline."""
        run_trace_id = trace_id or str(uuid.uuid4())
        entries = self.catalog.load_all()
        normalized_steps: list[PipelineStep] = []
        for raw in steps:
            if isinstance(raw, PipelineStep):
                normalized_steps.append(raw)
            else:
                try:
                    normalized_steps.append(PipelineStep.from_dict(raw))
                except (ValueError, TypeError) as exc:
                    return PipelineResult(status="error", dry_run=dry_run, error=str(exc))

        step_results: list[StepExecutionResult] = []
        outputs: dict[str, Any] = {}
        known_prior_steps: set[str] = set()

        for step in normalized_steps:
            entry = self._lookup_tool(step.tool_id, entries)
            if entry is None:
                err = f"unknown_tool_id:{step.tool_id}"
                res = StepExecutionResult(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    status="error",
                    attempts=0,
                    error=err,
                )
                step_results.append(res)
                if not dry_run:
                    self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status="error")
                return PipelineResult(
                    status="error",
                    dry_run=dry_run,
                    steps=step_results,
                    outputs=outputs,
                    error=err,
                )

            try:
                resolved_args = resolve_templates(
                    step.arguments,
                    outputs,
                    known_prior_steps=known_prior_steps,
                    dry_run=dry_run,
                )
                _validate_arguments(entry, resolved_args, dry_run=dry_run)
                target_url, query_params, headers, payload = self._build_request_parts(
                    entry, resolved_args, dry_run=dry_run
                )
            except (TemplateResolutionError, SchemaValidationError) as exc:
                err = str(exc)
                res = StepExecutionResult(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    status="error",
                    method=entry.method.upper(),
                    attempts=0,
                    error=err,
                )
                step_results.append(res)
                if not dry_run:
                    self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status="error")
                return PipelineResult(
                    status="error",
                    dry_run=dry_run,
                    steps=step_results,
                    outputs=outputs,
                    error=err,
                )

            allowed, allow_reason = self._check_allowlist(target_url)
            if not allowed:
                err = f"allowlist_blocked:{allow_reason}"
                res = StepExecutionResult(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    status="blocked",
                    method=entry.method.upper(),
                    url=target_url,
                    query_params=query_params,
                    headers=headers,
                    payload=payload,
                    attempts=0,
                    error=err,
                )
                step_results.append(res)
                if not dry_run:
                    self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status="aborted")
                return PipelineResult(
                    status="blocked",
                    dry_run=dry_run,
                    steps=step_results,
                    outputs=outputs,
                    error=err,
                )

            if dry_run:
                step_results.append(
                    StepExecutionResult(
                        step_id=step.step_id,
                        tool_id=step.tool_id,
                        status="dry_run",
                        method=entry.method.upper(),
                        url=target_url,
                        query_params=query_params,
                        headers=headers,
                        payload=payload,
                        latency_ms=0.0,
                        attempts=0,
                    )
                )
                known_prior_steps.add(step.step_id)
                continue

            max_retries = max(0, int(step.max_retries))
            backoff_ms = max(0, int(step.backoff_ms))
            retry_types = step.retry_on_exceptions or ()
            catch_types: tuple[type[BaseException], ...] = (
                httpx.HTTPError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                *retry_types,
            )

            started = time.monotonic()
            attempts = 0
            raw_resp: Any = None
            last_exc: BaseException | None = None
            for attempt in range(1, max_retries + 2):
                attempts = attempt
                try:
                    raw_resp = self._http_executor(
                        entry.method.upper(),
                        target_url,
                        query_params=query_params,
                        headers=headers,
                        payload=payload,
                        timeout=step.timeout_seconds,
                    )
                    last_exc = None
                    break
                except catch_types as exc:
                    last_exc = exc
                    should_retry = (
                        bool(retry_types)
                        and isinstance(exc, retry_types)
                        and attempt <= max_retries
                    )
                    if should_retry:
                        if backoff_ms > 0:
                            delay_seconds = (backoff_ms * (2 ** (attempt - 1))) / 1000.0
                            self._sleep_fn(delay_seconds)
                        continue
                    break

            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            if last_exc is not None:
                err = f"http_execution_error:{last_exc}"
                res = StepExecutionResult(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    status="error",
                    method=entry.method.upper(),
                    url=target_url,
                    query_params=query_params,
                    headers=headers,
                    payload=payload,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    error=err,
                )
                step_results.append(res)
                t_status = "timeout" if isinstance(last_exc, TimeoutError) else "error"
                self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status=t_status)
                return PipelineResult(
                    status="error",
                    dry_run=False,
                    steps=step_results,
                    outputs=outputs,
                    error=err,
                )

            status_code = 200
            step_output = raw_resp
            if isinstance(raw_resp, Mapping) and "status_code" in raw_resp:
                status_code = int(raw_resp["status_code"])
                if "body" in raw_resp:
                    step_output = raw_resp["body"]
                elif "output" in raw_resp:
                    step_output = raw_resp["output"]

            if status_code >= 400:
                err = f"http_status_error:{status_code}"
                res = StepExecutionResult(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    status="error",
                    method=entry.method.upper(),
                    url=target_url,
                    query_params=query_params,
                    headers=headers,
                    payload=payload,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    output=step_output,
                    error=err,
                )
                step_results.append(res)
                self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status="error")
                return PipelineResult(
                    status="error",
                    dry_run=False,
                    steps=step_results,
                    outputs=outputs,
                    error=err,
                )

            outputs[step.step_id] = step_output
            known_prior_steps.add(step.step_id)
            res = StepExecutionResult(
                step_id=step.step_id,
                tool_id=step.tool_id,
                status="ok",
                method=entry.method.upper(),
                url=target_url,
                query_params=query_params,
                headers=headers,
                payload=payload,
                status_code=status_code,
                latency_ms=latency_ms,
                attempts=attempts,
                output=step_output,
            )
            step_results.append(res)
            self._emit_telemetry(res, trace_id=run_trace_id, telemetry_status="ok")

        return PipelineResult(
            status="dry_run" if dry_run else "completed",
            dry_run=dry_run,
            steps=step_results,
            outputs=outputs,
        )
