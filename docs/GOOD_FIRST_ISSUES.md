# Good first issues (candidates)

These are scoped tasks suitable for new contributors. A maintainer can copy each
block into a GitHub issue.

---

## 1. Offline example for `catalog_search`

**Files:** `src/toolweave/catalog_search.py`, new `tests/test_catalog_search.py`

**Task:** Build a small list of `EndpointEntry` fixtures in the test file and
assert that `search("list devices")` ranks a `GET /devices` operation above
unrelated paths.

**Acceptance:** New test file passes; no AWS calls; documents expected scoring in
a one-line comment.

---

## 2. Document local-only egress override

**Files:** `docs/design/security-notes.md`, `src/toolweave/plan_guard.py`

**Task:** Add a short section explaining when to set
`TOOLWEAVE_ALLOW_PRIVATE_EGRESS=true` for local development and what risks it
re-enables.

**Acceptance:** Doc matches `plan_guard.resolve_egress` behavior; no endorsement
for production use.

---

## 3. Sanitize `.env.example` placeholders

**Files:** `.env.example`, `CONTRIBUTING.md`

**Task:** Replace account-specific bucket names and Lambda URLs with obvious
placeholders (`your-account`, `your-api-id`) while keeping variable names
accurate.

**Acceptance:** Example file contains no real account IDs; README/CONTRIBUTING
mention copying and substituting values.

---

## 4. Swagger parser golden test for minimal OpenAPI 3

**Files:** `examples/minimal-openapi.yaml`, `tests/test_swagger_parser_minimal.py`

**Task:** Parse the example YAML with `swagger_parser.parse_spec` and assert
operation count, `base_url`, and one parameter name.

**Acceptance:** Test runs offline; example referenced from `examples/README.md`.

---

## 5. CI workflow: add `ruff check` gate

**Files:** `.github/workflows/deploy.yml`

**Task:** Add a lint job running `python3 -m ruff check .` on pull requests, or
narrow to `src/toolweave` if the repo intentionally allows style drift elsewhere.

**Acceptance:** Job passes on `main`; document in `CONTRIBUTING.md`.

---

## 6. `health` tool response schema test

**Files:** `tests/test_mcp_auth.py` or new `tests/test_health_tool.py`

**Task:** Assert the `health` MCP tool returns stable keys (status, endpoint
count or equivalent per current implementation).

**Acceptance:** Test uses existing test harness patterns; no live AWS.

---

## 7. Publish specs dry-run flag

**Files:** `scripts/publish_specs.py`, `tests/test_published_specs.py`

**Task:** Add `--dry-run` that prints which specs would upload and which stack
outputs would be read, without calling AWS.

**Acceptance:** Unit test mocks boto3; help text in `--help`.

---

## 8. Architecture diagram link in README

**Files:** `README.md`, `docs/assets/`

**Task:** Ensure README quickstart links to C4/context diagrams under
`docs/assets/` with alt text for accessibility.

**Acceptance:** Links resolve in GitHub render; no binary changes required.

---

## 9. DataDictionary client timeout configuration

**Files:** `src/toolweave/data_dictionary_client.py`

**Task:** Make HTTP timeout configurable via env var (default unchanged); add
test with httpx mock.

**Acceptance:** Backward compatible default; documented in `docs/OPERATIONS.md`.

---

## 10. Remove stale README note about `Bearer 123` on executor

**Files:** `README.md` (or `docs/OPERATIONS.md` if split)

**Task:** Align docs with `api_credentials.py`: outbound auth comes from per-API
secrets, not a global placeholder on the executor path.

**Acceptance:** No false claim in user-facing docs; cite `api_credentials.py`.
