from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

from .models import ImmediateExecutionResult

_EXTERNAL_API_AUTH_HEADER = {"Authorization": "Bearer 123"}


async def execute_get(
    base_url: str,
    path: str,
    query_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> ImmediateExecutionResult:
    url = _build_url(base_url, path)
    return await _request("GET", url, query_params=query_params, headers=headers, timeout=timeout)


async def execute_write(
    method: str,
    base_url: str,
    path: str,
    query_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> ImmediateExecutionResult:
    url = _build_url(base_url, path)
    return await _request(
        method.upper(),
        url,
        query_params=query_params,
        headers=headers,
        body=body,
        timeout=timeout,
    )


async def _request(
    method: str,
    url: str,
    query_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> ImmediateExecutionResult:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            merged_headers = {
                **_EXTERNAL_API_AUTH_HEADER,
                **(headers or {}),
            }
            kwargs: dict[str, Any] = {
                "params": query_params or {},
                "headers": merged_headers,
            }
            if body is not None:
                kwargs["json"] = body

            resp = await client.request(method, url, **kwargs)
            elapsed_ms = (time.monotonic() - start) * 1000

            try:
                response_body = resp.json()
            except Exception:
                response_body = resp.text

            return ImmediateExecutionResult(
                method=method,
                url=url,
                status_code=resp.status_code,
                response_body=response_body,
                response_headers=dict(resp.headers),
                elapsed_ms=round(elapsed_ms, 2),
            )

    except httpx.TimeoutException as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return ImmediateExecutionResult(
            method=method,
            url=url,
            status_code=0,
            elapsed_ms=round(elapsed_ms, 2),
            error=f"Request timed out after {timeout}s: {exc}",
        )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return ImmediateExecutionResult(
            method=method,
            url=url,
            status_code=0,
            elapsed_ms=round(elapsed_ms, 2),
            error=str(exc),
        )


def _build_url(base_url: str, path: str) -> str:
    if not base_url:
        return path
    base = base_url.rstrip("/")
    p = path if path.startswith("/") else f"/{path}"
    return base + p
