from __future__ import annotations

import re
from typing import Any

from .models import EndpointEntry


def search(
    query: str,
    catalog: list[EndpointEntry],
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Keyword search over the in-memory endpoint catalog.

    Scores each endpoint by how many query tokens appear in its method,
    path, summary, description, and tags. Returns top_k results sorted by
    score descending, each as a plain dict suitable for JSON serialisation.
    """
    if not catalog or not query.strip():
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    scored: list[tuple[float, EndpointEntry]] = []
    for entry in catalog:
        score = _score(tokens, entry)
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        {
            "operation_id": e.operation_id,
            "method": e.method,
            "path": e.path,
            "summary": e.summary,
            "api_title": e.api_title,
            "score": round(s, 3),
        }
        for s, e in scored[:top_k]
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Lower-case and split on non-alphanumeric characters."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _score(tokens: list[str], entry: EndpointEntry) -> float:
    """Return a relevance score for *entry* against the query *tokens*."""
    # Build a bag-of-words corpus from all searchable fields
    corpus_parts = [
        entry.method.lower(),
        entry.path.lower(),
        entry.summary.lower(),
        entry.description.lower(),
        entry.api_title.lower(),
        " ".join(entry.tags).lower(),
    ]
    # Also tokenise the operation_id as individual words
    if entry.operation_id:
        corpus_parts.append(_unsplit_camel(entry.operation_id).lower())

    corpus_tokens = set()
    for part in corpus_parts:
        corpus_tokens.update(_tokenize(part))

    # Exact token hits
    hits = sum(1 for t in tokens if t in corpus_tokens)

    # Partial / prefix hits (lower weight)
    partial = sum(
        0.3
        for t in tokens
        if t not in corpus_tokens and any(c.startswith(t) or t in c for c in corpus_tokens)
    )

    # HTTP method match bonus (e.g. user says "get" or "post")
    method_tokens = {"get", "post", "put", "patch", "delete"}
    method_hit = any(t in method_tokens and t == entry.method.lower() for t in tokens)

    return hits + partial + (0.5 if method_hit else 0)


def _unsplit_camel(name: str) -> str:
    """Convert camelCase / PascalCase to space-separated words."""
    # Insert space before uppercase letters
    s = re.sub(r"([A-Z])", r" \1", name)
    # Insert space before digit-to-letter or letter-to-digit transitions
    s = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", s)
    return s
