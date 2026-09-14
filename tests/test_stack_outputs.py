"""Every table this service writes to must be discoverable from the stack.

Two of the three DynamoDB tables were unpublished. A harness that wanted to
assert what a proposal recorded, or what metadata an ingested spec produced,
had to derive the name from the ``${AWS::StackName}-Suffix`` convention. That
works until the convention changes, and then it fails in the worst way
available: reading a table that does not exist is a ResourceNotFoundException,
but reading the *wrong* table succeeds and returns nothing, which a test reads
as "the write did not happen".

So this asserts that every DynamoDB table and every S3 bucket in the template
has an Output naming it. It needs no AWS.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "template.yaml"


def _template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _logical_ids_of_type(resource_type: str) -> set:
    """Logical ids of every resource of one CloudFormation type.

    Matched on the two-space-indented logical id followed by its Type: line,
    rather than with a YAML loader -- the template is full of !Sub and !GetAtt
    tags a safe loader rejects, and a permissive loader that maps unknown tags
    to None silently reports !Sub-valued fields as absent.
    """
    return set(re.findall(
        rf"^  ([A-Za-z][A-Za-z0-9]*):\n    Type: {re.escape(resource_type)}\s*$",
        _template(), re.M,
    ))


def _outputs_block() -> str:
    parts = _template().split("\nOutputs:\n", 1)
    assert len(parts) == 2, "template.yaml has no top-level Outputs: block"
    return parts[1]


def _referenced_in_outputs() -> set:
    """Logical ids any Output resolves, via !Ref or !GetAtt."""
    block = _outputs_block()
    return (set(re.findall(r"!Ref\s+([A-Za-z][A-Za-z0-9]*)", block))
            | set(re.findall(r"!GetAtt\s+([A-Za-z][A-Za-z0-9]*)\.", block)))


def test_resources_were_actually_parsed():
    """Guard the guard: an empty parse would make the checks below vacuous."""
    tables = _logical_ids_of_type("AWS::DynamoDB::Table")
    assert len(tables) >= 3, f"parsed {sorted(tables)} -- the resource regex has gone stale"


def test_every_dynamodb_table_is_published_as_an_output():
    tables = _logical_ids_of_type("AWS::DynamoDB::Table")
    unpublished = sorted(tables - _referenced_in_outputs())
    assert not unpublished, (
        f"these tables have no Output naming them: {unpublished}. A harness "
        f"cannot discover them, and deriving the name from the stack-name "
        f"convention fails silently -- the wrong table reads as an empty one."
    )


def test_every_s3_bucket_is_published_as_an_output():
    buckets = _logical_ids_of_type("AWS::S3::Bucket")
    unpublished = sorted(buckets - _referenced_in_outputs())
    assert not unpublished, (
        f"these buckets have no Output naming them: {unpublished}"
    )
