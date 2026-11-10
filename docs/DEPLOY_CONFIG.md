# Deployment configuration (GitHub Actions)

ToolWeave deploys from `.github/workflows/deploy.yml` on pushes to `main` (not on pull requests). The workflow no longer embeds account-specific ARNs, stack names, or service URLs in the repository. Configure the following **before the next deploy** after merging a cleanup that removed hardcoded values.

> **Git history** may still contain older literals. Rotating or scrubbing history is out of scope for that change; this document lists what must exist in GitHub **now**.

## Repository secrets

| Name | Purpose | Example placeholder |
|------|---------|---------------------|
| `AWS_DEPLOY_ROLE_ARN` | IAM role ARN for OIDC (`configure-aws-credentials` `role-to-assume`) | `arn:aws:iam::123456789012:role/example-github-actions-sam-deployer` |
| `OBSERVATORY_SECRET_KEY` | HMAC key for mcp-observatory proposal/commit tokens (SAM parameter `ObservatorySecretKey`) | *(strong random string; never commit)* |

## Repository variables

| Name | Purpose | Example placeholder |
|------|---------|---------------------|
| `AWS_REGION` | AWS region for SAM deploy, CloudFormation lookups, and spec publish | `us-east-1` |
| `SHARED_STACK_NAME` | CloudFormation stack that exports VPC, subnet, and `OBSERVATORY_METRICS` table (SAM `SharedStackName`) | `example-shared-stack` |
| `DATA_DICTIONARY_URL` | DataDictionary MCP Lambda Function URL (SAM `DataDictionaryUrl`) | `https://example.lambda-url.us-east-1.on.aws/` |
| `STACK_NAME` | ToolWeave SAM stack name (optional; default `toolweave`) | `toolweave` |
| `MCP_AUTH_TOKEN_SECRET_NAME` | Secrets Manager secret **name** for legacy bearer token (optional; default `toolweave/mcp-auth-token`) | `toolweave/mcp-auth-token` |
| `CIPHERWEAVE_STACK_NAME` | Stack that outputs CipherWeave JWKS URL (optional; default `cipherweave-prod`) | `cipherweave-prod` |
| `ACCEPT_LEGACY_TOKEN` | Allow static bearer while migrating (optional; default `true`) | `true` |
| `TOOL_SCOPE_MODE` | `enforce` or `audit` for MCP scope checks (optional; default `enforce`) | `enforce` |
| `ALERT_EMAIL` | Ops alarm email subscription (optional; empty = no subscriber) | `you@example.com` |

## Local SAM deploy

Copy `.env.example` to `.env` and set values for your account. Override template parameters explicitly:

```bash
sam deploy --parameter-overrides \
  SharedStackName=example-shared-stack \
  DataDictionaryUrl=https://example.lambda-url.us-east-1.on.aws/ \
  McpAuthTokenSecretName=toolweave/mcp-auth-token
```

See `samconfig.toml` for default stack name and region.

## IAM trust

The deploy role (`AWS_DEPLOY_ROLE_ARN`) must trust GitHub OIDC for this repository and allow SAM/CloudFormation deploy permissions used by the workflow. Exact policy is account-specific and is not stored in this repo.
