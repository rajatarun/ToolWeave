#!/usr/bin/env python3
"""Publish the OpenAPI specs in src/Swagger/ to ToolWeave's ApiSpecsBucket.

ToolWeave serves whatever is in that bucket: an upload triggers
`SwaggerProcessorFunction`, which parses the spec and replaces that API's rows
in DynamoDB. Until now nothing in this repository put anything there -- the
bucket was populated by hand, so `src/Swagger/` was a sample the deployment did
not read and the repository had no record of what ToolWeave actually served.

Two things this has to get right, and one it deliberately does not do.

**The server variable.** Both sibling specs declare

    servers:
      - url: "{apiBaseUrl}"
        variables:
          apiBaseUrl:
            default: https://example.execute-api.us-east-1.amazonaws.com/prod

because they instruct callers to resolve the host from a CloudFormation stack
output rather than hardcode it. Uploading that verbatim publishes endpoints
whose base URL is either the literal `{apiBaseUrl}` or a placeholder host that
resolves to nothing -- an API the agent plans calls against and can never
reach. So each spec is published with its variable substituted from the real
stack output, and a sibling whose stack cannot be resolved is **skipped, with
its name**: one API fewer is a smaller failure than an API pointing at an
address that does not answer.

**The stale API.** Removing a spec from this repository does not remove it from
S3, and nothing removes its rows from DynamoDB either -- the processor listens
for `Object Created` only. So the prune below deletes bucket objects under the
managed prefix that this repository no longer declares. It is narrow on
purpose: it only ever deletes `.yaml`/`.yml`/`.json` keys directly under the
prefix, and it refuses to run at all when no local specs were found, so an
unreadable source directory cannot read as "delete everything".

What it does not do is guess. It never invents a stack name or a URL, and
`--dry-run` prints the plan without touching anything.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Iterable

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from toolweave.swagger_parser import _oas3_base_url

SPEC_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "Swagger"
SPEC_SUFFIXES = (".yaml", ".yml", ".json")

# local filename -> (CloudFormation stack, output key holding the base URL)
#
# The output key is each sibling's own; they do not agree on a name by
# accident, so it is read from their templates rather than assumed.
SPEC_SOURCES: dict[str, tuple[str, str]] = {
    "deviceweave.yaml": ("deviceweave", "ApiBaseUrl"),
    "content-orchestrator.yaml": ("tarun-admin-content", "ApiBaseUrl"),
}

class SpecError(RuntimeError):
    pass


def local_specs(spec_dir: pathlib.Path = SPEC_DIR) -> list[pathlib.Path]:
    return sorted(
        p for p in spec_dir.glob("*") if p.is_file() and p.suffix.lower() in SPEC_SUFFIXES
    )


def resolve_base_url(stack: str, output_key: str, region: str, runner=subprocess.run) -> str:
    """Read one stack output. Returns "" when the stack or output is absent."""
    result = runner(
        [
            "aws", "cloudformation", "describe-stacks",
            "--stack-name", stack,
            "--region", region,
            "--query", f"Stacks[0].Outputs[?OutputKey=='{output_key}'].OutputValue",
            "--output", "text",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    # The CLI prints "None" for a query that matched nothing, and an empty
    # string for a stack with no outputs at all. Neither is a URL.
    if value in {"", "None"}:
        return ""
    return value


def substitute_server(text: str, base_url: str) -> str:
    """Rewrite the spec's `servers` to the one literal URL it will be called at.

    Substituting the *variable default* and leaving the template in place would
    also work, but it leaves the published document indistinguishable from an
    unpublished one -- and that distinction is the whole safety property here.
    Both siblings ship a default of

        https://example.execute-api.us-east-1.amazonaws.com/prod

    which is not a template a reader would notice: it resolves, it is a
    syntactically valid URL, and it points at a host that does not exist. A
    spec uploaded verbatim therefore catalogs every endpoint against a dead
    address, the agent plans calls against them, and each one fails with a
    connection error naming a plausible AWS hostname.

    Publishing a literal server makes "was this published properly?" a
    question the processor can answer: a spec that still carries a server
    template did not come through here, and it refuses it.

    The document is re-emitted from its parsed form, so formatting and
    comments are not preserved. It is a deployment artifact read only by the
    parser; the source of truth is the sibling repository.
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise SpecError("spec did not parse to a mapping")
    raw["servers"] = [{"url": base_url, "description": "Resolved at publish time from the stack output"}]
    published = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=10_000)

    # Verify with the same resolver the processor will use, rather than
    # trusting that the edit above did what it says.
    resolved = _oas3_base_url(yaml.safe_load(published))
    if resolved != base_url:
        raise SpecError(
            f"published spec resolves to {resolved!r}, not the {base_url!r} "
            f"that was read from the stack output"
        )
    return published


def plan(region: str, runner=subprocess.run, spec_dir: pathlib.Path = SPEC_DIR):
    """Return (publishable, skipped) without writing anything."""
    specs = local_specs(spec_dir)
    if not specs:
        raise SpecError(
            f"no specs found in {spec_dir} -- refusing to run, because an "
            f"unreadable source directory must not read as 'delete everything'"
        )

    publishable: list[tuple[pathlib.Path, str]] = []
    skipped: list[tuple[str, str]] = []
    for path in specs:
        source = SPEC_SOURCES.get(path.name)
        if source is None:
            skipped.append((path.name, "no stack mapping in SPEC_SOURCES"))
            continue
        stack, output_key = source
        base_url = resolve_base_url(stack, output_key, region, runner=runner)
        if not base_url:
            skipped.append((path.name, f"{stack}.{output_key} did not resolve"))
            continue
        publishable.append((path, base_url))
    return publishable, skipped


def prune_keys(bucket_keys: Iterable[str], local_names: Iterable[str], prefix: str) -> list[str]:
    """Bucket keys under `prefix` that no local spec declares.

    Narrow by construction: a key must sit directly under the prefix and carry
    a spec suffix. A nested key, or anything else someone put in the bucket, is
    left alone rather than swept up.
    """
    local = set(local_names)
    stale = []
    for key in bucket_keys:
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):]
        if not name or "/" in name:
            continue
        if pathlib.Path(name).suffix.lower() not in SPEC_SUFFIXES:
            continue
        if name not in local:
            stale.append(key)
    return sorted(stale)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--prefix", default="", help="key prefix inside the bucket")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        publishable, skipped = plan(args.region)
    except SpecError as exc:
        print(f"::error::publish_specs: {exc}")
        return 1

    for name, why in skipped:
        print(f"::warning::ToolWeave will not serve {name}: {why}. "
              f"Deploy that sibling, then re-run this deploy to add it.")

    if not publishable:
        print("::warning::No API specs could be published. ToolWeave's catalog "
              "will keep whatever it already had; a fresh bucket leaves the MCP "
              "server with no endpoints to plan against at all.")

    for path, base_url in publishable:
        body = substitute_server(path.read_text(), base_url)
        key = f"{args.prefix}{path.name}"
        print(f"publish s3://{args.bucket}/{key}  ->  {base_url}")
        if args.dry_run:
            continue
        subprocess.run(
            ["aws", "s3", "cp", "-", f"s3://{args.bucket}/{key}",
             "--region", args.region, "--content-type", "application/yaml"],
            input=body, text=True, check=True,
        )

    # Prune whatever the repository no longer declares.
    listing = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", args.bucket,
         "--region", args.region, "--query", "Contents[].Key", "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if listing.returncode == 0 and (listing.stdout or "").strip() not in {"", "null"}:
        keys = json.loads(listing.stdout) or []
        # Compared against every *local* spec, not just the ones published on
        # this run. A sibling whose stack failed to resolve is skipped above --
        # pruning its key too would delete a working API from the catalog
        # because one describe-stacks call blipped.
        declared = [p.name for p in local_specs()]
        for key in prune_keys(keys, declared, args.prefix):
            print(f"::notice::pruning s3://{args.bucket}/{key} — no longer declared in src/Swagger/")
            if args.dry_run:
                continue
            subprocess.run(
                ["aws", "s3api", "delete-object", "--bucket", args.bucket,
                 "--key", key, "--region", args.region],
                check=False,
            )

    print(f"published={len(publishable)} skipped={len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
