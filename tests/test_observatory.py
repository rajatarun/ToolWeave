"""
Tests for toolweave.observatory's mcp-observatory 0.3.0 integration.

toolweave.observatory builds its proposer/verifier/token-manager triple (now
via mcp_observatory.aws.build_gate) at *import time*, so the fail-closed
secret behaviour can only be observed by controlling the environment before
the module is imported. Every test that cares about that reloads the module
under a monkeypatched environment and clears it from sys.modules afterwards
so later tests (and other test files) import a clean copy.
"""

from __future__ import annotations

import importlib
import sys

import pytest

MODULE_NAME = "toolweave.observatory"


def _reload_observatory():
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OBSERVATORY_SECRET_KEY", raising=False)
    monkeypatch.delenv("OBSERVATORY_BLOCK_THRESHOLD", raising=False)
    monkeypatch.delenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.delenv("MCP_OBSERVATORY_COMMIT_SECRET", raising=False)
    yield
    sys.modules.pop(MODULE_NAME, None)


# ---------------------------------------------------------------------------
# Fail-closed secret (mcp-observatory>=0.3.0)
# ---------------------------------------------------------------------------


def test_import_fails_closed_without_secret_or_dev_flag():
    """No more hardcoded 'change-me-in-production' fallback: an unset
    OBSERVATORY_SECRET_KEY must raise at construction time, not silently
    sign tokens with a public default.
    """
    from mcp_observatory.utils.secrets import InsecureDefaultSecretError

    with pytest.raises(InsecureDefaultSecretError):
        _reload_observatory()


def test_import_succeeds_with_dev_flag_for_local_and_test_use(monkeypatch):
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    module = _reload_observatory()
    assert module._proposer is not None
    assert module._verifier is not None
    assert module._token_manager.secret == b"dev-commit-secret"


def test_import_succeeds_with_real_secret_set(monkeypatch):
    monkeypatch.setenv("OBSERVATORY_SECRET_KEY", "a-real-strong-secret-value")
    module = _reload_observatory()
    assert module._token_manager.secret == b"a-real-strong-secret-value"


# ---------------------------------------------------------------------------
# build_gate wiring (mcp_observatory.aws) reads ToolWeave's existing env vars
# ---------------------------------------------------------------------------


def test_build_gate_uses_toolweaves_existing_env_var_names(monkeypatch):
    """build_gate must read OBSERVATORY_SECRET_KEY / OBSERVATORY_BLOCK_THRESHOLD
    (ToolWeave's already-deployed env var names), not the library's own
    MCP_OBSERVATORY_* defaults, so the swap needed no template.yaml renames.
    """
    monkeypatch.setenv("OBSERVATORY_SECRET_KEY", "a-real-strong-secret-value")
    monkeypatch.setenv("OBSERVATORY_BLOCK_THRESHOLD", "0.9")
    module = _reload_observatory()
    assert module._proposer.config.block_threshold == 0.9


def test_proposer_and_verifier_share_storage_and_token_manager(monkeypatch):
    monkeypatch.setenv("OBSERVATORY_SECRET_KEY", "a-real-strong-secret-value")
    module = _reload_observatory()
    assert module._proposer.token_manager is module._token_manager
    assert module._verifier.token_manager is module._token_manager
    assert module._proposer.storage is module._verifier.storage


# ---------------------------------------------------------------------------
# REVIEW verdict handling (documents a pre-existing gap, see tests/README.md
# equivalent note in DeviceWeave's test_observatory_wrapper.py)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "toolweave.agent.run_agent() only special-cases "
        "result.decision.action == 'block'; a 'review' verdict from the "
        "InvocationWrapperAPI/WrapperPolicy gate (e.g. cost or latency budget "
        "exceeded) falls through the same 'return result.output' path as an "
        "'allow' verdict, so REVIEW is silently treated as ALLOW. The model-"
        "level wrapper call inside _run_agent_inner() is even less checked: "
        "its decision.action is only logged, never inspected, for any verdict "
        "including 'block'. This documents the expected behaviour once "
        "run_agent() is wired to surface a non-allow verdict instead of "
        "returning the wrapped call's output unchanged."
    ),
)
def test_run_agent_surfaces_review_verdict_instead_of_ignoring_it(monkeypatch):
    monkeypatch.setenv("OBSERVATORY_SECRET_KEY", "a-real-strong-secret-value")
    import asyncio

    agent = importlib.import_module("toolweave.agent")
    from toolweave.models import PreToolResponse

    class _Decision:
        action = "review"
        reason = "cost_budget_exceeded"

    class _Span:
        cost_usd = 0.31
        hallucination_risk_level = "low"
        composite_risk_level = "low"

    class _FakeAgentWrapper:
        async def invoke(self, **kwargs):
            return type(
                "WrapperResult",
                (),
                {
                    "output": PreToolResponse(session_id=kwargs["session_id"], status="ready"),
                    "decision": _Decision(),
                    "span": _Span(),
                },
            )()

    monkeypatch.setattr(agent._obs, "get_agent_wrapper", lambda: _FakeAgentWrapper())

    result = asyncio.get_event_loop().run_until_complete(
        agent.run_agent(
            user_message="hello",
            session_id="sess-review-test",
            catalog=[],
            dd_context="",
        )
    )

    # Desired future behaviour: a 'review' verdict must be visible to the
    # caller instead of being indistinguishable from a clean 'allow'.
    assert result.status != "ready"
