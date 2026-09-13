# Vendored contracts

**Canonical home: [`rajatarun/mcp-observatory`](https://github.com/rajatarun/mcp-observatory) → `contracts/`.**

The files in this directory are **verbatim copies** and are not edited here.
Fix problems upstream in mcp-observatory and re-vendor; a local edit silently
forks the interface the shared table depends on.

| File | Purpose |
|------|---------|
| `observatory_metrics_item.json` | Item contract (v1.0.0) for the shared `OBSERVATORY_METRICS` DynamoDB table provisioned by the `tarun-teamweave-shared` stack. |
| `conformance.py` | Dependency-free checker (`load_contract`, `check_item`, `readers_for`) run by `tests/test_shared_table_contract.py`. |

## Why it is vendored rather than imported

The shared table has writers in more than one language and several readers that
build dashboards from it, none of which can see each other's code. The item
shape is therefore a cross-repository interface. Each consumer keeps a copy
next to a conformance test so its writer is checked against the same file its
siblings check against, with no runtime dependency and no network access.

Vendored copies must carry the same `version` string as upstream (`1.0.0`).

## What ToolWeave asserts against it

`tests/test_shared_table_contract.py` drives the real
`toolweave.observatory.DynamoDBSpanExporter` and `_write_invocation_metric`,
captures the emitted item, and asserts `check_item(item) == []` plus the
lower-case `pk`/`sk` regression guard.

It also asserts `readers_for(item["pk"]) == []` — the current, deliberately
recorded truth that ToolWeave's `WRAPPER#` / `INVOCATION#` namespaces have no
reader. That assertion documents a known platform gap; it is not an approval of
it, and it is expected to change when the portfolio settles on one namespace
scheme.
