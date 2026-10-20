"""ToolWeave takes its models from ModelWeave's role pins.

The agent and the endpoint enricher hardcoded
anthropic.claude-3-haiku-20240307-v1:0 -- a model ModelWeave lists as
retired -- and never read ModelWeave. Both now resolve the "reasoning" role
through the vendored modelweave_roles module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from toolweave import endpoint_enricher, modelweave_roles
from toolweave.models import EndpointEntry

ROOT = Path(__file__).resolve().parents[1]
RETIRED = "anthropic.claude-3-haiku-20240307-v1:0"
PINNED = "us.amazon.nova-pro-v1:0"


def test_vendored_module_is_byte_for_byte_the_pinned_modelweave_commit():
    manifest = json.loads((ROOT / "MODELWEAVE_VENDORED.json").read_text())
    data = (ROOT / manifest["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["sha256"], (
        "copy client/modelweave_roles.py from ModelWeave and update MODELWEAVE_VENDORED.json"
    )
    commit = manifest["source"].split("@")[1].split()[0]
    assert len(commit) == 40


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class Ssm:
    def __init__(self, pins):
        self.pins = pins

    def get_parameter(self, Name):
        if Name not in self.pins:
            raise ClientError("ParameterNotFound", Name)
        return {"Parameter": {"Value": json.dumps({"model_id": self.pins[Name]})}}


class Bedrock:
    def __init__(self, failing=None):
        self.failing, self.models = failing or {}, []

    def converse(self, modelId, **kwargs):
        self.models.append(modelId)
        if modelId in self.failing:
            raise self.failing[modelId]
        return {"output": {"message": {"content": [{"text": '{"agent_hint": "x"}'}]}}}


@pytest.fixture
def pins(monkeypatch):
    def install(values):
        resolver = modelweave_roles.RoleResolver(Ssm(values), enabled=True)
        monkeypatch.setattr(modelweave_roles, "_default", resolver)
        return resolver
    yield install
    modelweave_roles.reset_default_resolver()


def _enrich(bedrock, monkeypatch):
    monkeypatch.setattr(endpoint_enricher, "_client", lambda: bedrock)
    entry = EndpointEntry(path="/orders", method="GET", operation_id="listOrders")
    return endpoint_enricher._enrich_one(entry, "Orders", ["listOrders"])


def test_the_enricher_uses_the_reasoning_pin(pins, monkeypatch):
    pins({"/modelweave/roles/reasoning": PINNED})
    bedrock = Bedrock()
    assert _enrich(bedrock, monkeypatch) == {"agent_hint": "x"}
    assert bedrock.models == [PINNED]


def test_without_a_pin_the_fallback_is_not_the_retired_model(pins, monkeypatch):
    pins({})
    bedrock = Bedrock()
    _enrich(bedrock, monkeypatch)
    assert bedrock.models == ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert RETIRED not in bedrock.models


def test_a_retired_pin_recovers_to_the_fallback(pins, monkeypatch):
    pins({"/modelweave/roles/reasoning": RETIRED})
    bedrock = Bedrock({RETIRED: ClientError(
        "ValidationException", "This model version has reached the end of its life.")})
    _enrich(bedrock, monkeypatch)
    assert bedrock.models == [RETIRED, "us.anthropic.claude-haiku-4-5-20251001-v1:0"]


def test_the_agent_no_longer_chooses_its_own_model():
    source = (ROOT / "src" / "toolweave" / "agent.py").read_text()
    assert '"modelId": BEDROCK_MODEL_ID' not in source
    assert "modelweave_roles.converse(" in source


def test_template_grants_the_pin_read_and_defaults_off_the_retired_model():
    template = (ROOT / "template.yaml").read_text()
    assert RETIRED not in template
    assert "parameter/modelweave/roles/*" in template
    grant = template.split("parameter/modelweave/roles/*")[0].rsplit("- Effect: Allow", 1)[1]
    assert "ssm:GetParameter" in grant and "PutParameter" not in grant
