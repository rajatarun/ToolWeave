from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Catalog — built from OpenAPI/Swagger specs stored in DynamoDB
# ---------------------------------------------------------------------------


class EndpointParameter(BaseModel):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    data_type: str
    description: str = ""


class RequestBodyField(BaseModel):
    """Represents one leaf field in a request body schema (dot-notation for nested)."""

    name: str  # e.g. "address.street"
    required: bool
    data_type: str
    description: str = ""


class EndpointEntry(BaseModel):
    path: str  # "/orders/{orderId}"
    method: str  # "GET", "POST", … (uppercased)
    operation_id: str = ""
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[EndpointParameter] = Field(default_factory=list)
    body_fields: list[RequestBodyField] = Field(default_factory=list)
    content_type: str = "application/json"
    base_url: str = ""
    api_id: str = ""
    api_title: str = ""
    # --- Enrichment fields (populated by endpoint_enricher via Bedrock) ---
    agent_hint: str = ""              # when to use this vs similar endpoints
    example_prompts: list[str] = Field(default_factory=list)   # sample NL queries
    parameter_notes: dict[str, str] = Field(default_factory=dict)  # name → format hint
    response_hint: str = ""           # key fields in the response
    idempotent: Optional[bool] = None  # safe to retry?


# ---------------------------------------------------------------------------
# DataDictionary metadata
# ---------------------------------------------------------------------------


class DataElementMeta(BaseModel):
    dataElement: str
    meaning: str
    dataType: str
    examples: list[str] = Field(default_factory=list)
    constraints: str = ""
    relatedElements: list[str] = Field(default_factory=list)
    status: str = "active"


# ---------------------------------------------------------------------------
# Pre-tool response
# ---------------------------------------------------------------------------


class FieldMapping(BaseModel):
    field_name: str
    location: str  # "path" | "query" | "body" | "header"
    extracted_value: Any = None
    value_present: bool = False
    metadata: Optional[DataElementMeta] = None


class PreToolResponse(BaseModel):
    session_id: str
    status: Literal["ready", "needs_input", "no_match", "error"]
    # Set when status == "needs_input"
    question: Optional[str] = None
    # Set when status == "error"
    error: Optional[str] = None

    # API call plan — set when status == "ready"
    prompt: str = ""
    matched_endpoint: str = ""  # "POST /orders"
    operation_id: str = ""
    execution_type: Literal["immediate", "proposal", ""] = ""
    method: str = ""
    path: str = ""  # fully resolved, e.g. "/orders/ORD-123"
    base_url: str = ""
    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[dict[str, Any]] = None
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Post-tool responses
# ---------------------------------------------------------------------------


class ImmediateExecutionResult(BaseModel):
    execution_type: Literal["immediate"] = "immediate"
    method: str
    url: str
    status_code: int
    response_body: Any = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: Optional[str] = None


class ProposalResult(BaseModel):
    execution_type: Literal["proposal"] = "proposal"
    method: str
    url: str
    body: Optional[dict[str, Any]] = None
    status: Literal["allowed", "blocked"]
    proposal_id: str
    commit_token: Optional[str] = None
    composite_score: float = 0.0
    signals: dict[str, Any] = Field(default_factory=dict)
    next_step: str = ""


class CommitResult(BaseModel):
    status: Literal["committed", "blocked", "error"]
    commit_id: Optional[str] = None
    proposal_id: str
    reason: Optional[str] = None
    method: Optional[str] = None
    url: Optional[str] = None
    response_body: Any = None
