# Security policy

## Supported versions

Security fixes are applied on the default development branch (`main`). Tagged
releases will receive backports when they exist; today, track `main`.

| Version | Supported |
|---------|-----------|
| `main`  | Yes       |
| `< 0.1`  | No        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Send a private report with:

- A description of the issue and affected components (for example `/mcp`,
  `post_tool`, spec ingestion, or the SAM template).
- Steps to reproduce, including minimal request or configuration if applicable.
- Impact you believe it has (confidentiality, integrity, availability).

Use GitHub **Security Advisories** (*Report a vulnerability* on the repository
security tab) when available, or contact the repository owner through their
preferred private channel if you already collaborate with them.

We aim to acknowledge reports within a few business days. We will coordinate
fix timing and disclosure with you.

## Scope

This policy covers the ToolWeave application code, SAM template, and
documentation in this repository. It does not cover third-party services you
deploy alongside ToolWeave (Bedrock, target APIs, CipherWeave, DataDictionary,
or mcp-observatory) except where ToolWeave integrates with them.

## Safe harbor

We welcome good-faith research that follows responsible disclosure and avoids
privacy violations, destruction of data, or service disruption.
