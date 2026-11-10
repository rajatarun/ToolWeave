# Contributing to ToolWeave

Thank you for helping improve ToolWeave. This project turns OpenAPI specs into
safe, MCP-accessible API calls with planning, egress controls, and gated writes.

## Before you start

- Read `CLAUDE.md` for invariants that must not break (catalog binding, write
  gating, no invented field values, and related rules).
- Architecture detail lives in `docs/architecture.md`.
- Extension points are described in `docs/EXTENDING.md`.

## Development setup

**Requirements:** Python 3.12+, git.

```bash
git clone <your-fork-url>
cd ToolWeave
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` for local experiments. Replace any example hostnames,
table names, or ARNs with values for **your** environment. Do not commit `.env`.

For unit tests, **no AWS account is required**. Tests use [moto](https://github.com/getmoto/moto)
to mock DynamoDB and S3. If moto complains about missing credentials, set dummy
values:

```bash
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=testing
export AWS_SECRET_ACCESS_KEY=testing
```

Running the full MCP server locally (`toolweave` or SAM local) needs Bedrock
and DynamoDB configuration unless you mock those paths in tests.

## Running checks

These are the same gates CI runs on pull requests:

```bash
python3 -m pytest tests/ -q
```

Optional locally (not currently enforced in CI):

```bash
python3 -m ruff check .
```

## Code style

- Python 3.12+, type hints where the surrounding file uses them.
- [Ruff](https://docs.astral.sh/ruff/) for linting when you run it locally.
- Match existing module layout under `src/toolweave/`.
- **Do not edit vendored trees in place:** `src/cipherweave_verify/`,
  `src/toolweave/bedrock_cost.py`, and `src/toolweave/modelweave_roles.py` sync
  from upstream scripts and checksum files.

## Pull requests

1. Branch from `main` with a descriptive name (for example `feature/plan-guard-tests`).
2. Keep changes focused; mention security-sensitive behavior in the PR description.
3. Ensure `pytest` passes.
4. Update docs when behavior or configuration changes.
5. Do not commit secrets, tokens, or account-specific ARNs in docs or code.

Maintainers merge after review. Deployment to AWS runs automatically on `main`
via `.github/workflows/deploy.yml`; contributors do not need AWS access for
most changes.

## Adding tests

- Place tests under `tests/` with names `test_*.py`.
- Prefer moto-backed tests over live AWS calls.
- For MCP or Lambda import paths, see `tests/test_handlers_import.py`.

## Questions

Open a discussion or issue for design questions. For security issues, see
`SECURITY.md`.
