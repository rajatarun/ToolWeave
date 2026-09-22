"""What ToolWeave serves, and that it can actually be called.

ToolWeave's catalog is built from whatever sits in the ApiSpecsBucket. Nothing
in this repository used to write there, so `src/Swagger/` was a sample the
deployment never read: the file said one thing and the running server served
whatever had been uploaded by hand. These tests hold the two ends together --
the specs this repository declares, the parser that reads them, and the
publisher that puts them in the bucket.
"""
from __future__ import annotations

import io
import pathlib
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import publish_specs

from toolweave.swagger_parser import (
    _oas3_base_url,
    parse_spec,
    server_is_templated,
)

SPEC_DIR = REPO / "src" / "Swagger"
RESOLVED = "https://abc123.execute-api.us-east-1.amazonaws.com/prod"


def spec_paths():
    return publish_specs.local_specs(SPEC_DIR)


# ── which APIs this repository declares ─────────────────────────────────────

def test_the_declared_specs_are_the_two_siblings():
    assert {p.name for p in spec_paths()} == {
        "deviceweave.yaml",
        "content-orchestrator.yaml",
    }


def test_the_content_team_sample_is_gone():
    """It described TeamWeave's API, was never published by any deploy, and
    its endpoints outlived it in DynamoDB because the processor only ever
    listened for Object Created."""
    assert not (SPEC_DIR / "tarun-content-team.yaml").exists()


def test_every_declared_spec_has_a_stack_to_resolve_its_url_from():
    """A spec with no mapping is skipped by the publisher and silently never
    reaches the bucket -- it would look declared and serve nothing."""
    for path in spec_paths():
        assert path.name in publish_specs.SPEC_SOURCES, path.name


def test_no_stack_mapping_names_a_spec_that_is_not_there():
    names = {p.name for p in spec_paths()}
    for name in publish_specs.SPEC_SOURCES:
        assert name in names, f"{name} is mapped to a stack but no such spec exists"


# ── the parser resolves the OAS3 server variable ────────────────────────────

class TestServerVariables:
    """Both siblings declare `url: "{apiBaseUrl}"` with the real host supplied
    per deployment. The parser returned that literal string, so every endpoint
    would have been cataloged with a base URL of `{apiBaseUrl}`."""

    def test_a_server_variable_is_substituted_from_its_default(self):
        raw = {
            "servers": [
                {"url": "{apiBaseUrl}", "variables": {"apiBaseUrl": {"default": RESOLVED}}}
            ]
        }
        assert _oas3_base_url(raw) == RESOLVED

    def test_a_variable_inside_a_larger_url_is_substituted(self):
        raw = {
            "servers": [
                {
                    "url": "https://{host}/v1",
                    "variables": {"host": {"default": "api.example.org"}},
                }
            ]
        }
        assert _oas3_base_url(raw) == "https://api.example.org/v1"

    def test_an_unresolvable_template_is_not_a_url(self):
        """No `variables` block, or no default in it: returning the literal
        would build request targets like `{apiBaseUrl}/admin/articles`."""
        assert _oas3_base_url({"servers": [{"url": "{apiBaseUrl}"}]}) == ""
        assert _oas3_base_url(
            {"servers": [{"url": "{apiBaseUrl}", "variables": {"apiBaseUrl": {}}}]}
        ) == ""

    def test_a_plain_url_is_untouched(self):
        assert _oas3_base_url({"servers": [{"url": RESOLVED}]}) == RESOLVED

    def test_no_servers_block_is_empty(self):
        assert _oas3_base_url({}) == ""


class TestTheProcessorRefusesAnUnpublishedSpec:
    """Driven through the real `_process_file`, not by reading its source.

    A source-level assertion passes on a guard that is present and unreachable,
    which is the failure mode this platform has hit before.
    """

    def _run(self, spec_text):
        from toolweave import swagger_processor

        written = []

        class FakeDdb:
            delete_api_entries = staticmethod(lambda _i: None)
            delete_api_meta = staticmethod(lambda _i: None)
            write_endpoint_batch = staticmethod(lambda *a, **k: written.append("endpoints"))

            @staticmethod
            def write_api_meta(**kwargs):
                written.append(kwargs)

        class FakeS3:
            @staticmethod
            def get_object(Bucket, Key):
                return {"Body": io.BytesIO(spec_text.encode())}

        originals = (
            swagger_processor.dynamodb_client,
            swagger_processor._s3,
            swagger_processor.endpoint_enricher.enrich_endpoints,
        )
        swagger_processor.dynamodb_client = FakeDdb
        swagger_processor._s3 = FakeS3
        swagger_processor.endpoint_enricher.enrich_endpoints = lambda e: e
        try:
            swagger_processor._process_file("b", "spec.yaml")
        finally:
            (
                swagger_processor.dynamodb_client,
                swagger_processor._s3,
                swagger_processor.endpoint_enricher.enrich_endpoints,
            ) = originals
        return written

    @staticmethod
    def _spec(url: str, variables: str = "") -> str:
        return (
            "openapi: 3.0.3\n"
            "info:\n  title: T\n  version: '1'\n"
            + (f"servers:\n  - url: {url}\n{variables}" if url else "")
            + "paths:\n"
            "  /things:\n"
            "    get:\n"
            "      operationId: listThings\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n"
        )

    def test_a_published_spec_is_cataloged(self):
        written = self._run(self._spec(RESOLVED))
        assert written, "a spec with a literal server was not cataloged"

    def test_a_templated_server_is_refused_even_though_it_resolves(self):
        """The placeholder default is a valid URL for a host that does not
        exist, so `if not base_url` alone would have let this through."""
        vars_block = (
            "    variables:\n"
            "      apiBaseUrl:\n"
            "        default: https://example.execute-api.us-east-1.amazonaws.com/prod\n"
        )
        written = self._run(self._spec('"{apiBaseUrl}"', vars_block))
        assert written == [], "an unpublished spec was cataloged against the placeholder host"

    def test_a_spec_with_no_server_at_all_is_refused(self):
        assert self._run(self._spec("")) == []


# ── the real specs, through the real parser ─────────────────────────────────

@pytest.mark.parametrize("path", spec_paths(), ids=lambda p: p.name)
def test_each_published_spec_parses_into_callable_endpoints(path):
    """The substitution and the parse, end to end on the real file.

    A spec that parses to zero endpoints is skipped by the processor with a
    warning -- an API that is declared, published, and serves nothing.
    """
    published = publish_specs.substitute_server(path.read_text(), RESOLVED)
    raw = yaml.safe_load(published)
    entries, base_url, title = parse_spec(raw, api_id="test")

    assert base_url == RESOLVED, f"{path.name} did not resolve to the published URL"
    assert entries, f"{path.name} parsed to no endpoints"
    assert title
    for entry in entries:
        assert entry.path.startswith("/"), entry.path
        assert entry.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


@pytest.mark.parametrize("path", spec_paths(), ids=lambda p: p.name)
def test_the_source_spec_points_nowhere_until_it_is_published(path):
    """Why substitution is mandatory rather than tidy.

    The danger is not an obviously broken template -- it is that the source
    spec *does* resolve, to a placeholder host that looks like a real API
    Gateway URL and answers nothing. Uploading the file verbatim would catalog
    every endpoint against it and nothing would look wrong until a call was
    made.
    """
    raw = yaml.safe_load(path.read_text())
    _entries, base_url, _title = parse_spec(raw, api_id="test")
    assert base_url.startswith("https://example."), (
        f"{path.name} no longer carries a placeholder server. If it now names "
        f"a real host, publishing would still override it -- but a reader "
        f"would reasonably believe the file's URL is the one in use."
    )
    assert server_is_templated(raw), (
        f"{path.name} no longer declares a server variable, so the processor's "
        f"template check can no longer tell a published spec from a raw one"
    )


# ── the publisher ───────────────────────────────────────────────────────────

class TestSubstitution:
    def test_the_servers_block_becomes_one_literal_url(self):
        text = (
            "openapi: 3.0.3\n"
            "servers:\n"
            "  - url: \"{apiBaseUrl}\"\n"
            "    variables:\n"
            "      apiBaseUrl:\n"
            "        default: https://example.execute-api.us-east-1.amazonaws.com/prod\n"
            "components:\n"
            "  schemas:\n"
            "    Thing:\n"
            "      properties:\n"
            "        size:\n"
            "          default: 10\n"
        )
        out = publish_specs.substitute_server(text, RESOLVED)
        raw = yaml.safe_load(out)
        assert raw["servers"] == [
            {"url": RESOLVED, "description": "Resolved at publish time from the stack output"}
        ]
        assert not publish_specs._oas3_base_url(raw).startswith("https://example.")
        assert raw["components"]["schemas"]["Thing"]["properties"]["size"]["default"] == 10, \
            "a schema default outside servers was rewritten"

    def test_the_published_spec_carries_no_server_template(self):
        """The property the processor's refusal depends on."""
        for path in spec_paths():
            out = publish_specs.substitute_server(path.read_text(), RESOLVED)
            assert not server_is_templated(yaml.safe_load(out)), path.name

    def test_the_publisher_verifies_its_own_edit(self):
        """The check exists because the edit above is not self-evidently
        correct: it is verified with the resolver the processor will use,
        rather than trusted. A URL the resolver cannot return is refused
        rather than published."""
        with pytest.raises(publish_specs.SpecError):
            publish_specs.substitute_server(
                "openapi: 3.0.3\nservers:\n  - url: https://x\n", "https://{oops}/prod"
            )

    def test_a_document_that_is_not_a_mapping_is_refused(self):
        with pytest.raises(publish_specs.SpecError):
            publish_specs.substitute_server("- just\n- a list\n", RESOLVED)


class TestPlan:
    def _runner(self, outputs):
        def run(argv, **kwargs):
            class R:
                returncode = 0
                stdout = ""
            stack = argv[argv.index("--stack-name") + 1]
            R.stdout = outputs.get(stack, "None")
            return R
        return run

    def test_a_sibling_whose_stack_is_missing_is_skipped_not_guessed(self):
        runner = self._runner({"deviceweave": RESOLVED})
        publishable, skipped = publish_specs.plan("us-east-1", runner=runner, spec_dir=SPEC_DIR)
        assert [p.name for p, _ in publishable] == ["deviceweave.yaml"]
        assert [n for n, _ in skipped] == ["content-orchestrator.yaml"]

    def test_the_skip_reason_names_the_stack_and_output(self):
        runner = self._runner({})
        _publishable, skipped = publish_specs.plan("us-east-1", runner=runner, spec_dir=SPEC_DIR)
        reasons = dict(skipped)
        assert "tarun-admin-content.ApiBaseUrl" in reasons["content-orchestrator.yaml"]
        assert "deviceweave.ApiBaseUrl" in reasons["deviceweave.yaml"]

    def test_an_empty_spec_directory_refuses_to_run(self, tmp_path):
        """Otherwise an unreadable source directory reads as 'delete
        everything' and the prune empties the bucket."""
        with pytest.raises(publish_specs.SpecError):
            publish_specs.plan("us-east-1", runner=self._runner({}), spec_dir=tmp_path)

    def test_the_cli_prints_None_for_a_missing_output(self):
        """`aws ... --output text` prints the string "None", not empty, when
        the query matches nothing -- treating that as a URL would publish a
        spec whose server is literally None."""
        runner = self._runner({"deviceweave": "None"})
        publishable, _skipped = publish_specs.plan("us-east-1", runner=runner, spec_dir=SPEC_DIR)
        assert publishable == []


class TestPrune:
    PREFIX = ""

    def test_a_withdrawn_spec_is_pruned(self):
        stale = publish_specs.prune_keys(
            ["deviceweave.yaml", "tarun-content-team.yaml"],
            ["deviceweave.yaml"],
            self.PREFIX,
        )
        assert stale == ["tarun-content-team.yaml"]

    def test_a_declared_spec_is_kept(self):
        assert publish_specs.prune_keys(
            ["deviceweave.yaml"], ["deviceweave.yaml"], self.PREFIX
        ) == []

    def test_nested_keys_are_left_alone(self):
        """Narrow on purpose: the prune owns the managed prefix, not the
        bucket."""
        assert publish_specs.prune_keys(
            ["archive/old.yaml"], ["deviceweave.yaml"], self.PREFIX
        ) == []

    def test_non_spec_objects_are_left_alone(self):
        assert publish_specs.prune_keys(
            ["notes.txt", "README.md"], ["deviceweave.yaml"], self.PREFIX
        ) == []

    def test_a_prefix_scopes_the_prune(self):
        assert publish_specs.prune_keys(
            ["specs/stale.yaml", "elsewhere/stale.yaml"],
            ["deviceweave.yaml"],
            "specs/",
        ) == ["specs/stale.yaml"]


# ── withdrawing a spec must also empty the catalog ──────────────────────────

def test_removing_a_spec_from_the_bucket_forgets_its_endpoints():
    from toolweave import swagger_processor

    forgotten = []

    class FakeDdb:
        @staticmethod
        def delete_api_entries(api_id):
            forgotten.append(("entries", api_id))

        @staticmethod
        def delete_api_meta(api_id):
            forgotten.append(("meta", api_id))

    original = swagger_processor.dynamodb_client
    swagger_processor.dynamodb_client = FakeDdb
    try:
        result = swagger_processor.lambda_handler(
            {
                "source": "aws.s3",
                "detail-type": "Object Deleted",
                "detail": {
                    "bucket": {"name": "b"},
                    "object": {"key": "tarun-content-team.yaml"},
                },
            },
            None,
        )
    finally:
        swagger_processor.dynamodb_client = original

    assert result == {"processed": 1, "errors": 0}
    kinds = [k for k, _ in forgotten]
    assert "entries" in kinds, "endpoints were left in the catalog"
    assert "meta" in kinds, "the API metadata row was left behind"
    ids = {i for _, i in forgotten}
    assert len(ids) == 1, ids


def test_a_deletion_does_not_go_down_the_create_path():
    """`_process_file` would fetch an object that no longer exists."""
    from toolweave import swagger_processor

    def explode(*_a, **_k):
        raise AssertionError("deletion took the create path")

    original = swagger_processor._process_file
    swagger_processor._process_file = explode
    ddb_original = swagger_processor.dynamodb_client

    class FakeDdb:
        delete_api_entries = staticmethod(lambda _i: None)
        delete_api_meta = staticmethod(lambda _i: None)

    swagger_processor.dynamodb_client = FakeDdb
    try:
        swagger_processor.lambda_handler(
            {
                "source": "aws.s3",
                "detail-type": "Object Deleted",
                "detail": {"bucket": {"name": "b"}, "object": {"key": "x.yaml"}},
            },
            None,
        )
    finally:
        swagger_processor._process_file = original
        swagger_processor.dynamodb_client = ddb_original


def test_the_event_rule_subscribes_to_deletions():
    """The handler above is unreachable unless EventBridge forwards the event.

    Parsed, not grepped. A substring search over the template also matches the
    comment that explains *why* the rule subscribes -- so it passed on a
    template where the detail-type had been removed and only the prose about
    it remained.
    """

    class CfnLoader(yaml.SafeLoader):
        pass

    # Every CloudFormation short-form tag collapses to a marker: this test
    # reads the detail-type list, and !Sub in the same EventPattern must not
    # stop the document parsing.
    CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: f"<{suffix}>")

    doc = yaml.load((REPO / "template.yaml").read_text(), Loader=CfnLoader)
    pattern = doc["Resources"]["SwaggerProcessorEventRule"]["Properties"]["EventPattern"]
    assert set(pattern["detail-type"]) == {"Object Created", "Object Deleted"}
