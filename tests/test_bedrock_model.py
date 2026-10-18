"""The deployed Converse model is Claude Haiku 4.5, not retired Claude 3 Haiku.

The US inference profile is required: bedrock-runtime does not serve this
model as on-demand throughput under the bare foundation-model ID. From
us-east-1 the profile routes to us-east-1, us-east-2, and us-west-2, so
the role must be able to invoke the profile and the foundation model in
those regions.
"""

from __future__ import annotations

from pathlib import Path

from toolweave.bedrock import (
    HAIKU_US_INFERENCE_PROFILE_ID,
    RETIRED_MODEL_IDS,
    US_PROFILE_DESTINATION_REGIONS,
    converse_inference_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = HAIKU_US_INFERENCE_PROFILE_ID


def _read(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_profile_id_is_the_us_cross_region_inference_profile():
    assert PROFILE == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert PROFILE.startswith("us.")
    assert not PROFILE.startswith("anthropic.")
    assert US_PROFILE_DESTINATION_REGIONS == ("us-east-1", "us-east-2", "us-west-2")


def test_template_defaults_are_the_inference_profile_and_not_a_retired_model():
    text = _read("template.yaml")
    defaults = [
        line.split("Default:", 1)[1].strip().strip('"').strip("'")
        for line in text.splitlines()
        if line.strip().startswith("Default:")
        and "haiku" in line.lower()
    ]
    assert defaults == [PROFILE, PROFILE]
    for retired in RETIRED_MODEL_IDS:
        assert retired not in text


def test_code_defaults_follow_the_template():
    agent = _read("src", "toolweave", "agent.py")
    enricher = _read("src", "toolweave", "endpoint_enricher.py")
    assert "HAIKU_US_INFERENCE_PROFILE_ID" in agent
    assert "HAIKU_US_INFERENCE_PROFILE_ID" in enricher
    for retired in RETIRED_MODEL_IDS:
        assert retired not in agent
        assert retired not in enricher


def test_deploy_passes_the_profile_and_not_a_retired_model():
    """An omitted override keeps the previous stack value, including a retired ID."""
    workflow = _read(".github", "workflows", "deploy.yml")
    samconfig = _read("samconfig.toml")
    for text in (workflow, samconfig):
        assert f"BedrockModelId={PROFILE}" in text
        assert f"EnricherModelId={PROFILE}" in text
        for retired in RETIRED_MODEL_IDS:
            assert retired not in text


def test_iam_allows_the_inference_profile_and_destination_foundation_models():
    text = _read("template.yaml")
    assert "bedrock:InvokeModel" in text
    assert "bedrock:InvokeModelWithResponseStream" in text
    # Empty account segment: foundation-model ARNs in every region, including
    # the US profile destinations.
    assert 'arn:aws:bedrock:*::foundation-model/*' in text
    # Account-scoped inference profile, any region.
    assert "arn:aws:bedrock:*:${AWS::AccountId}:inference-profile/*" in text


def test_converse_requests_set_temperature_and_not_top_p():
    """Haiku 4.5 rejects a Converse call that sets both temperature and topP."""
    for max_tokens in (2048, 4096):
        config = converse_inference_config(max_tokens)
        assert config == {"maxTokens": max_tokens, "temperature": 0.0}
        assert "topP" not in config
        assert "top_p" not in config

    agent = _read("src", "toolweave", "agent.py")
    enricher = _read("src", "toolweave", "endpoint_enricher.py")
    assert "converse_inference_config(2048)" in agent
    assert "converse_inference_config(4096)" in enricher
    for source in (agent, enricher):
        assert "topP" not in source
        assert "top_p" not in source


def test_docs_and_env_example_do_not_name_the_retired_model():
    paths = (
        ("README.md",),
        (".env.example",),
        ("docs", "architecture.md"),
        ("docs", "claude.md"),
    )
    for parts in paths:
        text = _read(*parts)
        assert PROFILE in text
        for retired in RETIRED_MODEL_IDS:
            assert retired not in text
