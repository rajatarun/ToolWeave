"""Bedrock Converse settings shared by the planner and the enricher.

Claude 3 Haiku is retired; its model ID is listed in ``RETIRED_MODEL_IDS``.
On ``bedrock-runtime``, Claude Haiku 4.5 is not available as on-demand
throughput under the bare foundation-model ID. Callers use the US
cross-region inference profile. From us-east-1 that profile routes to
us-east-1, us-east-2, and us-west-2, so IAM has to allow ``InvokeModel``
on the inference-profile ARN and on the foundation model in each of
those regions.

Haiku 4.5 accepts ``temperature`` or ``topP``, and rejects a request
that sets both. These calls send temperature only.
"""

from __future__ import annotations

HAIKU_US_INFERENCE_PROFILE_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

RETIRED_MODEL_IDS = frozenset({
    "anthropic.claude-3-haiku-20240307-v1:0",
})

# Destination regions for the US geo profile when the caller is in us-east-1.
US_PROFILE_DESTINATION_REGIONS = ("us-east-1", "us-east-2", "us-west-2")


def converse_inference_config(max_tokens: int, *, temperature: float = 0.0) -> dict[str, float | int]:
    """Inference config for Claude Haiku 4.5 on the Converse API.

    ``topP`` is omitted on purpose. Setting it together with temperature
    is a ValidationException on this model.
    """
    return {"maxTokens": max_tokens, "temperature": temperature}
