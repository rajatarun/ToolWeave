"""Deterministic offline tool record/replay sandbox for ToolWeave.

Wraps sync or async tool executors (and ``ToolPipeline`` HTTP executors) to:
- ``record`` mode: redact sensitive arguments via :class:`ToolPolicyGuard` and
  persist JSONL fixtures (``tool_name``, ``args_hash``, ``redacted_args``,
  ``response``, ``error``).
- ``replay`` mode: deterministically return recorded responses matching
  ``(tool_name, args_hash)`` completely offline, raising :class:`ReplayMissError`
  on unexpected calls.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from toolweave.policy_guard import ToolPolicyGuard


class ReplayMissError(LookupError):
    """Raised in replay mode when no recorded fixture matches ``(tool_name, args_hash)``."""


def _canonical_args_hash(redacted_args: dict[str, Any]) -> str:
    canonical = json.dumps(
        redacted_args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ToolReplayHarness:
    """Record/replay harness for deterministic offline tool and pipeline testing."""

    def __init__(
        self,
        fixture_path: str | Path,
        mode: str = "replay",
        policy_guard: ToolPolicyGuard | None = None,
    ) -> None:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"record", "replay"}:
            raise ValueError(f"Unsupported replay harness mode: {mode!r}; expected 'record' or 'replay'")

        self.fixture_path = Path(fixture_path)
        self.mode = normalized_mode
        self.policy_guard = policy_guard if policy_guard is not None else ToolPolicyGuard()
        self._fixtures: dict[tuple[str, str], deque[dict[str, Any]]] = {}

        if self.mode == "replay":
            self._load_fixtures()

    def _load_fixtures(self) -> None:
        self._fixtures.clear()
        if not self.fixture_path.exists():
            return
        with self.fixture_path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {self.fixture_path}:{line_no}: {exc}") from exc
                key = (str(entry["tool_name"]), str(entry["args_hash"]))
                self._fixtures.setdefault(key, deque()).append(entry)

    def _append_fixture(self, entry: dict[str, Any]) -> None:
        self.fixture_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fixture_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str) + "\n")

    def _prepare_key(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
    ) -> tuple[str, dict[str, Any], str]:
        normalized_tool = str(tool_name or "").strip()
        if not normalized_tool:
            raise ValueError("tool_name must be a non-empty string")
        redacted_args = self.policy_guard.sanitize_args(args)
        args_hash = _canonical_args_hash(redacted_args)
        return normalized_tool, redacted_args, args_hash

    def _replay_lookup(self, tool_name: str, args_hash: str) -> Any:
        queue = self._fixtures.get((tool_name, args_hash))
        if not queue:
            raise ReplayMissError(
                f"No recorded fixture for tool={tool_name!r} args_hash={args_hash}"
            )
        entry = queue.popleft()
        if entry.get("error") is not None:
            err = entry["error"]
            err_type = err.get("type", "RuntimeError") if isinstance(err, dict) else "RuntimeError"
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Replayed tool error ({err_type}): {err_msg}")
        return copy.deepcopy(entry.get("response"))

    def execute(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        executor: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Any:
        """Execute ``tool_name`` synchronously under record or replay mode."""
        normalized_tool, redacted_args, args_hash = self._prepare_key(tool_name, args)

        if self.mode == "replay":
            return self._replay_lookup(normalized_tool, args_hash)

        if executor is None:
            raise ValueError("executor is required in 'record' mode")

        raw_args = dict(args) if args is not None else {}
        try:
            response = executor(raw_args)
        except Exception as exc:
            self._append_fixture(
                {
                    "tool_name": normalized_tool,
                    "args_hash": args_hash,
                    "redacted_args": redacted_args,
                    "response": None,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            raise

        self._append_fixture(
            {
                "tool_name": normalized_tool,
                "args_hash": args_hash,
                "redacted_args": redacted_args,
                "response": response,
                "error": None,
            }
        )
        return response

    async def aexecute(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        executor: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
    ) -> Any:
        """Execute ``tool_name`` asynchronously under record or replay mode."""
        normalized_tool, redacted_args, args_hash = self._prepare_key(tool_name, args)

        if self.mode == "replay":
            return self._replay_lookup(normalized_tool, args_hash)

        if executor is None:
            raise ValueError("executor is required in 'record' mode")

        raw_args = dict(args) if args is not None else {}
        try:
            result = executor(raw_args)
            response = await result if inspect.isawaitable(result) else result
        except Exception as exc:
            self._append_fixture(
                {
                    "tool_name": normalized_tool,
                    "args_hash": args_hash,
                    "redacted_args": redacted_args,
                    "response": None,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            raise

        self._append_fixture(
            {
                "tool_name": normalized_tool,
                "args_hash": args_hash,
                "redacted_args": redacted_args,
                "response": response,
                "error": None,
            }
        )
        return response

    def wrap(
        self,
        tool_name: str,
        executor: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> Callable[[Mapping[str, Any]], Any]:
        """Return a wrapped sync or async callable bound to ``tool_name``."""
        if executor is not None and inspect.iscoroutinefunction(executor):

            async def _async_wrapped(args: Mapping[str, Any] | None = None) -> Any:
                return await self.aexecute(tool_name, args, executor)

            return _async_wrapped

        def _sync_wrapped(args: Mapping[str, Any] | None = None) -> Any:
            return self.execute(tool_name, args, executor)

        return _sync_wrapped

    def make_http_executor(
        self,
        http_executor: Callable[..., dict[str, Any]] | None = None,
    ) -> Callable[..., dict[str, Any]]:
        """Build a ``ToolPipeline``-compatible ``http_executor`` backed by this harness."""

        def _harness_http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            tool_key = f"http:{method.upper()}:{url}"
            call_args = {"method": method.upper(), "url": url, **kwargs}

            def _invoke(_raw: Mapping[str, Any]) -> dict[str, Any]:
                if http_executor is None:
                    raise ValueError("http_executor is required in 'record' mode")
                return http_executor(method, url, **kwargs)

            return self.execute(tool_key, call_args, _invoke if self.mode == "record" else None)

        return _harness_http
